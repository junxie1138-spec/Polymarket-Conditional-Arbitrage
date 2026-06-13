from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .arb_models import BinaryMarket

SCHEMA_VERSION = 1


@dataclass(frozen=True)
class MarketUniverseCacheRecord:
    fetched_at: datetime
    gamma_query: Mapping[str, Any]
    events_fetched: int
    raw_markets: int
    markets: tuple[BinaryMarket, ...]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("fetched_at_utc must be a non-empty string")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def binary_market_to_cache_row(market: BinaryMarket) -> dict[str, Any]:
    return {
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "question": market.question,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "active": market.active,
        "closed": market.closed,
        "accepting_orders": market.accepting_orders,
        "enable_order_book": market.enable_order_book,
        "neg_risk": market.neg_risk,
        "tick_size": market.tick_size,
        "min_order_size": market.min_order_size,
        "metadata": dict(market.metadata),
    }


def binary_market_from_cache_row(row: Mapping[str, Any]) -> BinaryMarket:
    metadata = row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    market = BinaryMarket(
        market_id=str(row["market_id"]),
        condition_id=str(row["condition_id"]) if row.get("condition_id") not in (None, "") else None,
        question=str(row.get("question") or ""),
        yes_token_id=str(row["yes_token_id"]),
        no_token_id=str(row["no_token_id"]),
        active=bool(row.get("active", True)),
        closed=bool(row.get("closed", False)),
        accepting_orders=bool(row.get("accepting_orders", True)),
        enable_order_book=bool(row.get("enable_order_book", True)),
        neg_risk=bool(row.get("neg_risk", False)),
        tick_size=float(row["tick_size"]) if row.get("tick_size") not in (None, "") else None,
        min_order_size=float(row["min_order_size"]) if row.get("min_order_size") not in (None, "") else None,
        metadata=dict(metadata),
    )
    if not market.is_tradable:
        raise ValueError(f"cached market is not tradable: {market.market_id}")
    return market


def write_market_universe_cache(
    path: Path,
    *,
    markets: list[BinaryMarket] | tuple[BinaryMarket, ...],
    events_fetched: int,
    raw_markets: int,
    gamma_query: Mapping[str, Any],
    fetched_at: datetime | None = None,
) -> None:
    timestamp = fetched_at or _utc_now()
    payload = {
        "schema_version": SCHEMA_VERSION,
        "fetched_at_utc": _utc_iso(timestamp),
        "gamma_query": dict(gamma_query),
        "counts": {
            "events_fetched": int(events_fetched),
            "raw_markets": int(raw_markets),
            "tradable_markets": len(markets),
            "tokens": sum(2 for _market in markets),
        },
        "markets": [binary_market_to_cache_row(market) for market in markets],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(path.name + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def load_market_universe_cache(
    path: Path,
    *,
    max_age_seconds: int,
    logger: logging.Logger | None = None,
    now: datetime | None = None,
) -> MarketUniverseCacheRecord | None:
    if not path.exists():
        return None

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("cache root must be an object")
        if payload.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version={payload.get('schema_version')!r}")
        fetched_at = _parse_utc(payload.get("fetched_at_utc"))
        age_seconds = (now or _utc_now()).astimezone(timezone.utc).timestamp() - fetched_at.timestamp()
        if age_seconds > max_age_seconds:
            if logger is not None:
                logger.warning(
                    "market_universe_cache_ignored reason=stale path=%s age_seconds=%.1f max_age_seconds=%s",
                    path,
                    age_seconds,
                    max_age_seconds,
                )
            return None

        raw_markets = payload.get("markets")
        if not isinstance(raw_markets, list):
            raise ValueError("markets must be a list")
        markets = tuple(binary_market_from_cache_row(row) for row in raw_markets if isinstance(row, Mapping))
        if len(markets) != len(raw_markets):
            raise ValueError("markets contains non-object rows")

        counts = payload.get("counts")
        if not isinstance(counts, dict):
            counts = {}
        gamma_query = payload.get("gamma_query")
        if not isinstance(gamma_query, dict):
            gamma_query = {}
        return MarketUniverseCacheRecord(
            fetched_at=fetched_at,
            gamma_query=dict(gamma_query),
            events_fetched=int(counts.get("events_fetched", 0)),
            raw_markets=int(counts.get("raw_markets", len(markets))),
            markets=markets,
        )
    except Exception as exc:
        if logger is not None:
            logger.warning("market_universe_cache_ignored reason=invalid path=%s error=%r", path, exc)
        return None
