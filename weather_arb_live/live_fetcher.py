from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable

from . import config, network
from .strategy import yes_token_from_market

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LiveMarketSnapshot:
    market: dict
    token_id: str
    yes_midpoint: float


def _as_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _prices_from_side(side: Iterable) -> list[float]:
    prices: list[float] = []
    for level in side or []:
        if isinstance(level, dict):
            value = level.get("price")
        elif isinstance(level, (list, tuple)) and level:
            value = level[0]
        else:
            value = None
        price = _as_float(value)
        if price is not None:
            prices.append(price)
    return prices


def midpoint_from_book(book: dict) -> float | None:
    bids = _prices_from_side(book.get("bids") or book.get("buy") or [])
    asks = _prices_from_side(book.get("asks") or book.get("sell") or [])
    if not bids or not asks:
        return None
    best_bid = max(bids)
    best_ask = min(asks)
    if best_bid <= 0.0 or best_ask >= 1.0 or best_bid > best_ask:
        return None
    return round((best_bid + best_ask) / 2.0, 10)


class LiveFetcher:
    def __init__(self, *, session=None, clob_host: str | None = None):
        self.session = session or network.get_session()
        self.clob_host = (clob_host or config.clob_host()).rstrip("/")

    def fetch_active_events(self, *, tag_slug: str = "weather") -> list[dict]:
        events: list[dict] = []
        offset = 0
        limit = 100
        while True:
            batch = network.get_json_with_retries(
                self.session,
                config.GAMMA_EVENTS_URL,
                params={
                    "tag_slug": tag_slug,
                    "closed": "false",
                    "limit": limit,
                    "offset": offset,
                },
                timeout=30,
            )
            if not isinstance(batch, list):
                raise ValueError(f"unexpected Gamma events response: {type(batch).__name__}")
            if not batch:
                break
            events.extend(batch)
            if len(batch) < limit:
                break
            offset += limit
        return events

    @staticmethod
    def flatten_event_markets(events: list[dict]) -> list[dict]:
        markets: list[dict] = []
        seen: set[str] = set()
        for event in events:
            for market in event.get("markets", []) or []:
                row = dict(market)
                row["_event_title"] = event.get("title")
                row["_event_id"] = event.get("id")
                row["_event_endDate"] = event.get("endDate")
                row["_event_tags"] = [tag.get("slug") for tag in (event.get("tags") or [])]
                key = str(row.get("id") or row.get("conditionId") or id(row))
                if key in seen:
                    continue
                seen.add(key)
                markets.append(row)
        return markets

    def fetch_active_markets(self, *, limit: int | None = None) -> list[dict]:
        markets = self.flatten_event_markets(self.fetch_active_events(tag_slug="weather"))
        if limit is not None:
            return markets[:limit]
        return markets

    def fetch_order_book(self, token_id: str) -> dict:
        data = network.get_json_with_retries(
            self.session,
            f"{self.clob_host}/book",
            params={"token_id": token_id},
            timeout=20,
        )
        if isinstance(data, dict):
            return data
        raise ValueError(f"unexpected order book response for token_id={token_id}")

    def fetch_midpoint(self, token_id: str) -> float | None:
        return midpoint_from_book(self.fetch_order_book(token_id))

    def fetch_yes_midpoint(self, token_id: str) -> float | None:
        return self.fetch_midpoint(token_id)

    def fetch_snapshots(self, *, limit: int | None = None) -> list[LiveMarketSnapshot]:
        snapshots: list[LiveMarketSnapshot] = []
        for market in self.fetch_active_markets(limit=limit):
            token_id = yes_token_from_market(market)
            if not token_id:
                logger.info("skip market_id=%s reason=missing_yes_token", market.get("id"))
                continue
            try:
                midpoint = self.fetch_yes_midpoint(token_id)
            except Exception as exc:
                logger.exception(
                    "price_error market_id=%s token_id=%s error=%s",
                    market.get("id"),
                    token_id,
                    exc,
                )
                continue
            if midpoint is None:
                logger.info(
                    "skip market_id=%s token_id=%s reason=missing_two_sided_book",
                    market.get("id"),
                    token_id,
                )
                continue
            snapshots.append(LiveMarketSnapshot(market=market, token_id=token_id, yes_midpoint=midpoint))
        return snapshots
