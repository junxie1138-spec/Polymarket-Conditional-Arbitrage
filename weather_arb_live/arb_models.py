from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


BookSideName = Literal["bid", "ask"]


def _json_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
    return value if isinstance(value, list) else []


def _as_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return parsed


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return default
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


def _first_present(mapping: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _normalized_outcome_label(value: Any) -> str:
    return str(value or "").strip().upper()


def _token_id_from_token_row(row: dict[str, Any]) -> str | None:
    for key in ("token_id", "tokenId", "clobTokenId", "asset_id", "assetId", "id"):
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def outcome_token_map_from_market(market: dict[str, Any]) -> dict[str, str]:
    token_rows = _json_list(market.get("tokens"))
    mapped: dict[str, str] = {}
    for row in token_rows:
        if not isinstance(row, dict):
            continue
        label = _normalized_outcome_label(
            row.get("outcome")
            or row.get("name")
            or row.get("label")
            or row.get("title")
        )
        token_id = _token_id_from_token_row(row)
        if label and token_id:
            mapped[label] = token_id

    if mapped:
        return mapped

    outcomes = _json_list(market.get("outcomes"))
    token_ids = _json_list(market.get("clobTokenIds"))
    if len(outcomes) != len(token_ids):
        return {}
    for label, token_id in zip(outcomes, token_ids):
        normalized = _normalized_outcome_label(label)
        if normalized and token_id not in (None, ""):
            mapped[normalized] = str(token_id)
    return mapped


@dataclass(frozen=True)
class BinaryMarket:
    market_id: str
    condition_id: str | None
    question: str
    yes_token_id: str
    no_token_id: str
    active: bool = True
    closed: bool = False
    accepting_orders: bool = True
    enable_order_book: bool = True
    neg_risk: bool = False
    tick_size: float | None = None
    min_order_size: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_gamma_market(cls, market: dict[str, Any]) -> "BinaryMarket | None":
        mapped = outcome_token_map_from_market(market)
        yes_token_id = mapped.get("YES")
        no_token_id = mapped.get("NO")
        if not yes_token_id or not no_token_id:
            return None
        if len({yes_token_id, no_token_id}) != 2:
            return None

        market_id = str(market.get("id") or market.get("conditionId") or "").strip()
        if not market_id:
            return None

        tick_size = _as_float(market.get("orderPriceMinTickSize") or market.get("tickSize"))
        min_order_size = _as_float(market.get("orderMinSize") or market.get("minOrderSize"))
        return cls(
            market_id=market_id,
            condition_id=str(market.get("conditionId") or "").strip() or None,
            question=str(market.get("question") or market.get("title") or ""),
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
            active=_as_bool(market.get("active"), default=True),
            closed=_as_bool(market.get("closed"), default=False),
            accepting_orders=_as_bool(
                _first_present(market, "acceptingOrders", "accepting_orders"),
                default=True,
            ),
            enable_order_book=_as_bool(
                _first_present(market, "enableOrderBook", "enable_order_book"),
                default=True,
            ),
            neg_risk=_as_bool(market.get("negRisk") or market.get("neg_risk"), default=False),
            tick_size=tick_size,
            min_order_size=min_order_size,
            metadata={
                key: market.get(key)
                for key in (
                    "slug",
                    "endDate",
                    "_event_id",
                    "_event_title",
                    "_event_endDate",
                    "_event_tags",
                )
                if key in market
            },
        )


@dataclass(frozen=True)
class BookLevel:
    price: float
    size: float

    @property
    def cost(self) -> float:
        return self.price * self.size


@dataclass(frozen=True)
class OrderBookSide:
    token_id: str
    side: BookSideName
    levels: tuple[BookLevel, ...]
    source: str = "rest_book"
    updated_at: datetime | None = None

    @property
    def available_size(self) -> float:
        return sum(level.size for level in self.levels)

    @property
    def best_price(self) -> float | None:
        if not self.levels:
            return None
        return self.levels[0].price

    def cost_to_fill(self, quantity: float) -> float | None:
        remaining = float(quantity)
        if remaining <= 0:
            return 0.0
        cost = 0.0
        for level in self.levels:
            take = min(remaining, level.size)
            cost += take * level.price
            remaining -= take
            if remaining <= 1e-12:
                return cost
        return None

    def vwap_to_fill(self, quantity: float) -> float | None:
        if quantity <= 0:
            return None
        cost = self.cost_to_fill(quantity)
        if cost is None:
            return None
        return cost / quantity


@dataclass(frozen=True)
class ArbOpportunity:
    market: BinaryMarket
    executable_size: float
    yes_vwap: float
    no_vwap: float
    yes_cost: float
    no_cost: float
    gross_cost: float
    estimated_fees: float
    gas_cost: float
    slippage_buffer: float
    net_profit: float
    net_return_bps: float
    source_timestamps: dict[str, str | None]
    detected_at: datetime
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def merge_value(self) -> float:
        return self.executable_size
