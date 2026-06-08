from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from . import config, network
from .arb_models import BinaryMarket, as_bool
from .order_book import asks_from_book


def _chunked(values: list[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _book_token_id(book: dict[str, Any]) -> str | None:
    for key in ("asset_id", "assetId", "token_id", "tokenId"):
        value = book.get(key)
        if value not in (None, ""):
            return str(value)
    return None


class GammaClobClient:
    def __init__(
        self,
        *,
        session=None,
        clob_host: str | None = None,
        gamma_events_url: str = config.GAMMA_EVENTS_URL,
        batch_book_limit: int = config.CLOB_BATCH_BOOK_LIMIT,
    ):
        self.session = session or network.get_session()
        self.clob_host = (clob_host or config.clob_host()).rstrip("/")
        self.gamma_events_url = gamma_events_url
        self.batch_book_limit = max(1, min(config.CLOB_BATCH_BOOK_LIMIT, batch_book_limit))

    def fetch_active_events(self) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        offset = 0
        limit = 100
        while True:
            batch = network.get_json_with_retries(
                self.session,
                self.gamma_events_url,
                params={"closed": "false", "limit": limit, "offset": offset},
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
    def flatten_event_markets(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        markets: list[dict[str, Any]] = []
        seen: set[str] = set()
        for event in events:
            event_id = event.get("id")
            tags = [tag.get("slug") for tag in (event.get("tags") or []) if isinstance(tag, dict)]
            event_neg_risk = as_bool(event.get("negRisk") or event.get("neg_risk"), default=False)
            for market in event.get("markets", []) or []:
                if not isinstance(market, dict):
                    continue
                row = dict(market)
                row["_event_id"] = event_id
                row["_event_title"] = event.get("title")
                row["_event_slug"] = event.get("slug")
                row["_event_endDate"] = event.get("endDate")
                row["_event_tags"] = tags
                row["_event_neg_risk"] = event_neg_risk
                key = str(row.get("id") or row.get("conditionId") or id(row))
                if key in seen:
                    continue
                seen.add(key)
                markets.append(row)
        return markets

    @staticmethod
    def tradable_binary_markets(raw_markets: Iterable[dict[str, Any]]) -> list[BinaryMarket]:
        markets: list[BinaryMarket] = []
        seen_tokens: set[str] = set()
        for row in raw_markets:
            market = BinaryMarket.from_gamma_market(row)
            if market is None or not market.is_tradable:
                continue
            if market.yes_token_id in seen_tokens or market.no_token_id in seen_tokens:
                continue
            seen_tokens.add(market.yes_token_id)
            seen_tokens.add(market.no_token_id)
            markets.append(market)
        return markets

    def fetch_tradable_markets(self, *, limit: int | None = None) -> list[BinaryMarket]:
        raw_markets = self.flatten_event_markets(self.fetch_active_events())
        markets = self.tradable_binary_markets(raw_markets)
        if limit is not None:
            return markets[:limit]
        return markets

    def fetch_order_book(self, token_id: str) -> dict[str, Any]:
        data = network.get_json_with_retries(
            self.session,
            f"{self.clob_host}/book",
            params={"token_id": token_id},
            timeout=20,
        )
        if isinstance(data, dict):
            return data
        raise ValueError(f"unexpected order book response for token_id={token_id}")

    def fetch_order_books_batch(self, token_ids: list[str]) -> dict[str, dict[str, Any]]:
        payload = [{"token_id": token_id} for token_id in token_ids]
        data = network.post_json_with_retries(
            self.session,
            f"{self.clob_host}/books",
            json_body=payload,
            timeout=30,
        )
        if not isinstance(data, list):
            raise ValueError(f"unexpected batch order book response: {type(data).__name__}")
        if len(data) != len(token_ids):
            raise ValueError(
                f"batch order book response length {len(data)} does not match request {len(token_ids)}"
            )

        books: dict[str, dict[str, Any]] = {}
        for requested_token_id, raw_book in zip(token_ids, data):
            if not isinstance(raw_book, dict):
                raise ValueError("batch order book response contains non-object book")
            token_id = _book_token_id(raw_book) or requested_token_id
            books[str(token_id)] = raw_book
        return books

    def fetch_ask_books(self, token_ids: Iterable[str]) -> dict[str, Any]:
        unique_token_ids = list(dict.fromkeys(str(token_id) for token_id in token_ids if token_id))
        ask_books: dict[str, Any] = {}
        for chunk in _chunked(unique_token_ids, self.batch_book_limit):
            received_at = datetime.now(timezone.utc)
            try:
                raw_books = self.fetch_order_books_batch(chunk)
                for token_id in chunk:
                    raw_book = raw_books[token_id]
                    ask_books[token_id] = asks_from_book(
                        raw_book,
                        token_id=token_id,
                        source="rest_books_batch",
                        updated_at=received_at,
                    )
            except Exception:
                for token_id in chunk:
                    raw_book = self.fetch_order_book(token_id)
                    ask_books[token_id] = asks_from_book(
                        raw_book,
                        token_id=token_id,
                        source="rest_book_fallback",
                        updated_at=datetime.now(timezone.utc),
                    )
        return ask_books
