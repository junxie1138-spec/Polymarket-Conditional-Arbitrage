from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, network
from .ledger import PositionLedger
from .live_fetcher import LiveFetcher
from .order_placer import OrderPlacer
from .strategy import token_ids_from_market


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ExchangeExposure:
    source: str
    token_id: str
    condition_id: str | None = None
    size: float | None = None
    price: float | None = None
    side: str | None = None
    title: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ReconciliationResult:
    active_markets: int
    exchange_exposures: int
    matched_local: int
    missing_local: int
    added_guards: int
    user_address: str


def _first_str(row: dict[str, Any], keys: Iterable[str]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        return str(value)
    return None


def _first_float(row: dict[str, Any], keys: Iterable[str]) -> float | None:
    for key in keys:
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def _compact_exchange_row(row: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "id",
        "order_id",
        "orderID",
        "hash",
        "market",
        "conditionId",
        "condition_id",
        "asset",
        "asset_id",
        "token_id",
        "tokenId",
        "side",
        "outcome",
        "price",
        "avgPrice",
        "curPrice",
        "cashPnl",
        "percentPnl",
        "size",
        "original_size",
        "remaining_size",
        "title",
    )
    return {key: row[key] for key in keep if key in row}


def exposure_from_open_order(row: dict[str, Any]) -> ExchangeExposure | None:
    token_id = _first_str(row, ("asset_id", "asset", "token_id", "tokenId"))
    if not token_id:
        return None
    return ExchangeExposure(
        source="open_order",
        token_id=token_id,
        condition_id=_first_str(row, ("market", "conditionId", "condition_id")),
        size=_first_float(row, ("remaining_size", "size", "original_size")),
        price=_first_float(row, ("price",)),
        side=_first_str(row, ("side",)),
        title=_first_str(row, ("title",)),
        raw=_compact_exchange_row(row),
    )


def exposure_from_position(row: dict[str, Any]) -> ExchangeExposure | None:
    token_id = _first_str(row, ("asset", "asset_id", "token_id", "tokenId"))
    if not token_id:
        return None
    return ExchangeExposure(
        source="position",
        token_id=token_id,
        condition_id=_first_str(row, ("conditionId", "condition_id", "market")),
        size=_first_float(row, ("size",)),
        price=_first_float(row, ("avgPrice", "price", "curPrice")),
        side=_first_str(row, ("outcome", "side")),
        title=_first_str(row, ("title",)),
        raw=_compact_exchange_row(row),
    )


def _market_key(market: dict) -> str:
    return str(market.get("id") or market.get("conditionId") or "")


def _market_condition_id(market: dict) -> str:
    return str(market.get("conditionId") or "")


def _normalized_outcome_side(value: Any) -> str | None:
    side = str(value or "").strip().upper()
    return side if side in {"YES", "NO"} else None


def _outcome_side_for_exposure(exposure: ExchangeExposure, market: dict | None) -> str | None:
    side = _normalized_outcome_side(exposure.side)
    if side is not None:
        return side
    if not market:
        return None
    token_ids = token_ids_from_market(market)
    if token_ids and exposure.token_id == token_ids[0]:
        return "YES"
    if len(token_ids) > 1 and exposure.token_id == token_ids[1]:
        return "NO"
    return None


def _token_market_indexes(markets: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    by_token: dict[str, dict] = {}
    by_condition: dict[str, dict] = {}
    for market in markets:
        condition_id = _market_condition_id(market)
        if condition_id:
            by_condition[condition_id] = market
        for token_id in token_ids_from_market(market):
            by_token[token_id] = market
    return by_token, by_condition


def _row_matches_exposure(row: dict[str, Any], exposure: ExchangeExposure, market: dict | None) -> bool:
    row_token_id = str(row.get("token_id") or "")
    row_condition_id = str(row.get("condition_id") or "")
    row_market_id = str(row.get("market_id") or "")

    if row_token_id and row_token_id == exposure.token_id:
        return True
    if exposure.condition_id and row_condition_id == exposure.condition_id:
        return True
    if exposure.condition_id and row_market_id == exposure.condition_id:
        return True
    if not market:
        return False

    market_key = _market_key(market)
    condition_id = _market_condition_id(market)
    if row_market_id and row_market_id == market_key:
        return True
    if row_condition_id and condition_id and row_condition_id == condition_id:
        return True
    return bool(row_token_id and row_token_id in set(token_ids_from_market(market)))


def _find_matching_exposure(
    row: dict[str, Any],
    exposures: list[ExchangeExposure],
    by_token: dict[str, dict],
    by_condition: dict[str, dict],
) -> ExchangeExposure | None:
    for exposure in exposures:
        market = by_token.get(exposure.token_id)
        if market is None and exposure.condition_id:
            market = by_condition.get(exposure.condition_id)
        if _row_matches_exposure(row, exposure, market):
            return exposure
    return None


def _ledger_has_exchange_guard(
    ledger: PositionLedger,
    *,
    market: dict | None,
    exposure: ExchangeExposure,
) -> bool:
    for key, row in ledger.positions.items():
        if bool(row.get("dry_run")):
            continue
        row_market_id = str(row.get("market_id") or "")
        row_condition_id = str(row.get("condition_id") or "")
        row_token_id = str(row.get("token_id") or "")
        if str(key) == exposure.token_id or row_token_id == exposure.token_id:
            return True
        if exposure.condition_id and str(key) == exposure.condition_id:
            return True
        if exposure.condition_id and row_condition_id == exposure.condition_id:
            return True
        if exposure.condition_id and row_market_id == exposure.condition_id:
            return True
        if not market:
            continue
        market_key = _market_key(market)
        condition_id = _market_condition_id(market)
        if str(key) == market_key or row_market_id == market_key:
            return True
        if condition_id and (str(key) == condition_id or row_condition_id == condition_id):
            return True
    return False


class Reconciler:
    def __init__(
        self,
        *,
        fetcher: LiveFetcher,
        order_placer: OrderPlacer,
        ledger: PositionLedger,
        session=None,
        logger_: logging.Logger | None = None,
    ):
        self.fetcher = fetcher
        self.order_placer = order_placer
        self.ledger = ledger
        self.session = session or network.get_session()
        self.logger = logger_ or logger

    def reconcile(
        self,
        *,
        market_limit: int | None = None,
        active_markets: list[dict] | None = None,
        reason: str = "startup",
    ) -> ReconciliationResult:
        user_address, address_source = self._resolve_user_address()
        markets = (
            active_markets
            if active_markets is not None
            else self.fetcher.fetch_active_markets(limit=market_limit)
        )
        by_token, by_condition = _token_market_indexes(markets)

        open_orders = self.order_placer.fetch_open_orders()
        positions = self._fetch_user_positions(user_address)
        exposures = self._exchange_exposures(open_orders, positions, by_token, by_condition)
        now = datetime.now(timezone.utc)

        matched_local = 0
        missing_local = 0
        for key, row in self.ledger.positions.items():
            if bool(row.get("dry_run")):
                continue
            exposure = _find_matching_exposure(row, exposures, by_token, by_condition)
            if exposure is None:
                missing_local += 1
                row["reconciliation"] = {
                    "status": "missing_exchange_match",
                    "requires_manual_review": True,
                    "reconciled_at": now.isoformat(),
                }
                self.logger.warning(
                    "reconcile_missing_exchange_match ledger_key=%s token_id=%s",
                    key,
                    row.get("token_id"),
                )
                continue
            matched_local += 1
            row["reconciliation"] = {
                "status": f"matched_{exposure.source}",
                "requires_manual_review": False,
                "reconciled_at": now.isoformat(),
                "exchange": exposure.raw,
            }

        added_guards = 0
        for exposure in exposures:
            market = by_token.get(exposure.token_id)
            if market is None and exposure.condition_id:
                market = by_condition.get(exposure.condition_id)
            if _ledger_has_exchange_guard(self.ledger, market=market, exposure=exposure):
                continue
            self._record_exchange_guard(exposure=exposure, market=market, reconciled_at=now)
            added_guards += 1

        self.ledger.save()
        self.logger.info(
            "reconcile_complete reason=%s user_address=%s source=%s active_markets=%s "
            "exchange_exposures=%s matched_local=%s missing_local=%s added_guards=%s",
            reason,
            user_address,
            address_source,
            len(markets),
            len(exposures),
            matched_local,
            missing_local,
            added_guards,
        )
        return ReconciliationResult(
            active_markets=len(markets),
            exchange_exposures=len(exposures),
            matched_local=matched_local,
            missing_local=missing_local,
            added_guards=added_guards,
            user_address=user_address,
        )

    def _resolve_user_address(self) -> tuple[str, str]:
        for name in (
            "POLYMARKET_RECONCILE_USER_ADDRESS",
            "POLYMARKET_FUNDER_ADDRESS",
            "POLYMARKET_PROXY_ADDRESS",
            "POLYMARKET_WALLET_ADDRESS",
        ):
            value = os.getenv(name)
            if value:
                return value.strip(), name
        return self.order_placer.get_client_address(), "client_wallet"

    def _fetch_user_positions(self, user_address: str) -> list[dict[str, Any]]:
        positions: list[dict[str, Any]] = []
        limit = 500
        offset = 0
        while True:
            batch = network.get_json_with_retries(
                self.session,
                f"{config.DATA_API_BASE_URL}/positions",
                params={
                    "user": user_address,
                    "sizeThreshold": 0,
                    "limit": limit,
                    "offset": offset,
                },
                timeout=30,
            )
            if not isinstance(batch, list):
                raise ValueError(f"unexpected positions response: {type(batch).__name__}")
            dict_batch = [row for row in batch if isinstance(row, dict)]
            positions.extend(dict_batch)
            if len(batch) < limit:
                break
            offset += limit
        return positions

    @staticmethod
    def _exchange_exposures(
        open_orders: list[dict[str, Any]],
        positions: list[dict[str, Any]],
        by_token: dict[str, dict],
        by_condition: dict[str, dict],
    ) -> list[ExchangeExposure]:
        exposures: list[ExchangeExposure] = []
        for row in open_orders:
            exposure = exposure_from_open_order(row)
            if exposure is not None:
                exposures.append(exposure)
        for row in positions:
            exposure = exposure_from_position(row)
            if exposure is not None:
                exposures.append(exposure)

        weather_exposures: list[ExchangeExposure] = []
        for exposure in exposures:
            if exposure.token_id in by_token:
                weather_exposures.append(exposure)
                continue
            if exposure.condition_id and exposure.condition_id in by_condition:
                weather_exposures.append(exposure)
        return weather_exposures

    def _record_exchange_guard(
        self,
        *,
        exposure: ExchangeExposure,
        market: dict | None,
        reconciled_at: datetime,
    ) -> None:
        market_id = _market_key(market) if market else exposure.condition_id or exposure.token_id
        condition_id = _market_condition_id(market) if market else exposure.condition_id
        row = {
            "market_id": market_id,
            "condition_id": condition_id,
            "token_id": exposure.token_id,
            "side": _outcome_side_for_exposure(exposure, market),
            "question": (market or {}).get("question") or exposure.title or "",
            "city": None,
            "target_date": None,
            "market_price": exposure.price,
            "entry_price": exposure.price,
            "shares": exposure.size,
            "position_usd": None if exposure.price is None or exposure.size is None else exposure.price * exposure.size,
            "forecast_prob": None,
            "edge": None,
            "lead_days": None,
            "entry_time": reconciled_at.isoformat(),
            "dry_run": False,
            "order_response": {
                "posted": "reconciled",
                "reason": f"exchange_{exposure.source}",
                "reconciled_at": reconciled_at.isoformat(),
                "exchange": exposure.raw,
            },
            "reconciliation": {
                "status": f"added_guard_from_{exposure.source}",
                "requires_manual_review": False,
                "reconciled_at": reconciled_at.isoformat(),
            },
        }
        self.ledger.positions[market_id] = row
        self.logger.warning(
            "reconcile_added_guard market_id=%s condition_id=%s token_id=%s source=%s",
            market_id,
            condition_id,
            exposure.token_id,
            exposure.source,
        )
