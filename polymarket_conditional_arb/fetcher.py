from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from typing import Any

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


def _emit_ask_book_progress(
    on_progress: Callable[[dict[str, Any]], None] | None,
    *,
    total_tokens: int,
    completed_tokens: int,
    received_books: int,
    failed_tokens: int,
    current_batch_number: int | None = None,
    total_batches: int | None = None,
    current_batch_start_token: int | None = None,
    current_batch_end_token: int | None = None,
    current_batch_status: str | None = None,
    current_batch_started_at_utc: str | None = None,
) -> None:
    if on_progress is None:
        return
    progress = {
        "total_tokens": total_tokens,
        "completed_tokens": completed_tokens,
        "remaining_tokens": max(0, total_tokens - completed_tokens),
        "received_books": received_books,
        "failed_tokens": failed_tokens,
    }
    optional = {
        "current_batch_number": current_batch_number,
        "total_batches": total_batches,
        "current_batch_start_token": current_batch_start_token,
        "current_batch_end_token": current_batch_end_token,
        "current_batch_status": current_batch_status,
        "current_batch_started_at_utc": current_batch_started_at_utc,
    }
    progress.update({key: value for key, value in optional.items() if value is not None})
    on_progress(progress)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


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

    def fetch_active_events(
        self,
        *,
        on_page: Callable[[int, int, int], None] | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
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
            next_total = len(events) + len(batch)
            if on_page is not None:
                on_page(offset, len(batch), next_total)
            if not batch:
                break
            events.extend(batch)
            if should_continue is not None and not should_continue():
                raise InterruptedError("active event fetch stopped")
            if len(batch) < limit:
                break
            offset += limit
        return events

    def fetch_active_events_slice(
        self,
        *,
        limit: int,
        order: str | None = None,
        ascending: bool | None = None,
        on_page: Callable[[int, int, int], None] | None = None,
        should_continue: Callable[[], bool] | None = None,
    ) -> list[dict[str, Any]]:
        page_limit = max(1, int(limit))
        params: dict[str, Any] = {"closed": "false", "limit": page_limit}
        if order:
            params["order"] = order
        if ascending is not None:
            params["ascending"] = "true" if ascending else "false"
        batch = network.get_json_with_retries(
            self.session,
            self.gamma_events_url,
            params=params,
            timeout=30,
        )
        if not isinstance(batch, list):
            raise ValueError(f"unexpected Gamma events response: {type(batch).__name__}")
        if on_page is not None:
            on_page(0, len(batch), len(batch))
        if should_continue is not None and not should_continue():
            raise InterruptedError("active event fetch stopped")
        return batch

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

        books: dict[str, dict[str, Any]] = {}
        response_is_full_length = len(data) == len(token_ids)
        for index, raw_book in enumerate(data):
            if not isinstance(raw_book, dict):
                raise ValueError("batch order book response contains non-object book")
            token_id = _book_token_id(raw_book)
            if token_id is None:
                if not response_is_full_length:
                    raise ValueError("partial batch order book response contains row without token id")
                token_id = token_ids[index]
            books[str(token_id)] = raw_book
        return books

    def fetch_ask_books(
        self,
        token_ids: Iterable[str],
        *,
        on_progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        unique_token_ids = list(dict.fromkeys(str(token_id) for token_id in token_ids if token_id))
        ask_books: dict[str, Any] = {}
        completed_tokens = 0
        failed_tokens = 0
        total_tokens = len(unique_token_ids)
        total_batches = (total_tokens + self.batch_book_limit - 1) // self.batch_book_limit
        for batch_index, chunk in enumerate(_chunked(unique_token_ids, self.batch_book_limit), start=1):
            batch_start_token = completed_tokens + 1
            batch_end_token = completed_tokens + len(chunk)
            batch_started_at_utc = _utc_iso_now()
            _emit_ask_book_progress(
                on_progress,
                total_tokens=total_tokens,
                completed_tokens=completed_tokens,
                received_books=len(ask_books),
                failed_tokens=failed_tokens,
                current_batch_number=batch_index,
                total_batches=total_batches,
                current_batch_start_token=batch_start_token,
                current_batch_end_token=batch_end_token,
                current_batch_status="in_flight",
                current_batch_started_at_utc=batch_started_at_utc,
            )
            received_at = datetime.now(timezone.utc)
            batch_exc: Exception | None = None
            fallback_tokens: list[str] = []
            try:
                raw_books = self.fetch_order_books_batch(chunk)
                for token_id in chunk:
                    raw_book = raw_books.get(token_id)
                    if raw_book is None:
                        fallback_tokens.append(token_id)
                        continue
                    try:
                        ask_books[token_id] = asks_from_book(
                            raw_book,
                            token_id=token_id,
                            source="rest_books_batch",
                            updated_at=received_at,
                        )
                    except Exception:
                        fallback_tokens.append(token_id)
            except Exception as exc:
                batch_exc = exc
                fallback_tokens = list(chunk)

            if fallback_tokens:
                fallback_failures: dict[str, Exception] = {}
                for token_id in fallback_tokens:
                    try:
                        raw_book = self.fetch_order_book(token_id)
                    except Exception as exc:
                        fallback_failures[token_id] = exc
                        continue
                    ask_books[token_id] = asks_from_book(
                        raw_book,
                        token_id=token_id,
                        source="rest_book_fallback",
                        updated_at=datetime.now(timezone.utc),
                    )
                failed_tokens += len(fallback_failures)
                completed_tokens += len(chunk)
                _emit_ask_book_progress(
                    on_progress,
                    total_tokens=total_tokens,
                    completed_tokens=completed_tokens,
                    received_books=len(ask_books),
                    failed_tokens=failed_tokens,
                    current_batch_number=batch_index,
                    total_batches=total_batches,
                    current_batch_start_token=batch_start_token,
                    current_batch_end_token=batch_end_token,
                    current_batch_status="complete",
                    current_batch_started_at_utc=batch_started_at_utc,
                )
                if batch_exc is not None and len(fallback_failures) == len(chunk):
                    raise RuntimeError(
                        f"failed to fetch order books for all {len(chunk)} tokens after batch failure"
                    ) from batch_exc
                continue
            completed_tokens += len(chunk)
            _emit_ask_book_progress(
                on_progress,
                total_tokens=total_tokens,
                completed_tokens=completed_tokens,
                received_books=len(ask_books),
                failed_tokens=failed_tokens,
                current_batch_number=batch_index,
                total_batches=total_batches,
                current_batch_start_token=batch_start_token,
                current_batch_end_token=batch_end_token,
                current_batch_status="complete",
                current_batch_started_at_utc=batch_started_at_utc,
            )
        return ask_books
