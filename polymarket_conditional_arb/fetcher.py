from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from typing import Any

from . import config, network
from .arb_models import BinaryMarket, as_bool
from .order_book import asks_from_book

MAX_FAILURE_SAMPLE_TOKENS = 5


def _jsonable_request_meta(meta: Mapping[str, Any]) -> dict[str, Any]:
    rendered: dict[str, Any] = {}
    for key, value in meta.items():
        if isinstance(value, datetime):
            rendered[key] = value.isoformat().replace("+00:00", "Z")
        else:
            rendered[key] = value
    return rendered


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
    failed_token_sample: Iterable[str] | None = None,
    failure_categories: Mapping[str, int] | None = None,
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
        "failed_token_sample": list(failed_token_sample) if failed_token_sample is not None else None,
        "failure_categories": dict(failure_categories) if failure_categories is not None else None,
    }
    progress.update({key: value for key, value in optional.items() if value is not None})
    on_progress(progress)


def _utc_iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _error_category(stage: str, exc: Exception) -> str:
    return f"{stage}:{type(exc).__name__}"


class GammaClobClient:
    def __init__(
        self,
        *,
        session=None,
        clob_host: str | None = None,
        gamma_events_url: str = config.GAMMA_EVENTS_URL,
        gamma_markets_url: str | None = None,
        batch_book_limit: int = config.CLOB_BATCH_BOOK_LIMIT,
    ):
        self.session = session or network.get_session()
        self.clob_host = (clob_host or config.clob_host()).rstrip("/")
        self.gamma_events_url = gamma_events_url
        self.gamma_markets_url = gamma_markets_url or gamma_events_url.replace("/events", "/markets")
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

    def fetch_market_rows_by_ids(
        self,
        market_ids: Iterable[str],
        *,
        request_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, dict[str, Any]]:
        rows_by_id: dict[str, dict[str, Any]] = {}
        for market_id in dict.fromkeys(str(market_id) for market_id in market_ids if market_id):
            request_meta = {
                "endpoint_family": "gamma_markets",
                "endpoint": "/markets",
                "market_id": market_id,
            }
            data = network.get_json_with_retries(
                self.session,
                self.gamma_markets_url,
                params={"id": market_id},
                timeout=30,
                meta=request_meta,
            )
            if request_records is not None:
                request_records.append(_jsonable_request_meta(request_meta))
            rows: list[dict[str, Any]]
            if isinstance(data, list):
                rows = [row for row in data if isinstance(row, dict)]
            elif isinstance(data, dict):
                rows = [data]
            else:
                raise ValueError(f"unexpected Gamma markets response: {type(data).__name__}")
            matched = next(
                (
                    row
                    for row in rows
                    if str(row.get("id") or row.get("conditionId") or "").strip() == market_id
                ),
                rows[0] if rows else None,
            )
            if matched is not None:
                rows_by_id[market_id] = dict(matched)
        return rows_by_id

    def fetch_binary_markets_by_ids(
        self,
        market_ids: Iterable[str],
        *,
        request_records: list[dict[str, Any]] | None = None,
    ) -> dict[str, BinaryMarket]:
        rows_by_id = self.fetch_market_rows_by_ids(market_ids, request_records=request_records)
        markets: dict[str, BinaryMarket] = {}
        for market_id, row in rows_by_id.items():
            market = BinaryMarket.from_gamma_market(row)
            if market is not None:
                markets[market_id] = market
        return markets

    def fetch_order_book(self, token_id: str, *, request_meta: dict[str, Any] | None = None) -> dict[str, Any]:
        data = network.get_json_with_retries(
            self.session,
            f"{self.clob_host}/book",
            params={"token_id": token_id},
            timeout=20,
            meta=request_meta,
        )
        if isinstance(data, dict):
            return data
        raise ValueError(f"unexpected order book response for token_id={token_id}")

    def fetch_order_books_batch(
        self,
        token_ids: list[str],
        *,
        request_meta: dict[str, Any] | None = None,
    ) -> dict[str, dict[str, Any]]:
        payload = [{"token_id": token_id} for token_id in token_ids]
        data = network.post_json_with_retries(
            self.session,
            f"{self.clob_host}/books",
            json_body=payload,
            timeout=30,
            meta=request_meta,
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
        failed_token_sample: list[str] = []
        failure_categories: dict[str, int] = {}
        total_tokens = len(unique_token_ids)
        total_batches = (total_tokens + self.batch_book_limit - 1) // self.batch_book_limit

        def add_failure_category(category: str, count: int = 1) -> None:
            failure_categories[category] = failure_categories.get(category, 0) + max(1, int(count))

        def add_failed_token_sample(token_id: str) -> None:
            if len(failed_token_sample) < MAX_FAILURE_SAMPLE_TOKENS:
                failed_token_sample.append(str(token_id))

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
                failed_token_sample=failed_token_sample,
                failure_categories=failure_categories,
            )
            received_at = datetime.now(timezone.utc)
            batch_exc: Exception | None = None
            fallback_tokens: list[str] = []
            fallback_reasons: dict[str, str] = {}
            try:
                raw_books = self.fetch_order_books_batch(chunk)
                for token_id in chunk:
                    raw_book = raw_books.get(token_id)
                    if raw_book is None:
                        fallback_tokens.append(token_id)
                        fallback_reasons[token_id] = "batch:missing_book"
                        add_failure_category("batch:missing_book")
                        continue
                    try:
                        ask_books[token_id] = asks_from_book(
                            raw_book,
                            token_id=token_id,
                            source="rest_books_batch",
                            updated_at=received_at,
                        )
                    except Exception as exc:
                        fallback_tokens.append(token_id)
                        category = _error_category("batch_parse", exc)
                        fallback_reasons[token_id] = category
                        add_failure_category(category)
            except Exception as exc:
                batch_exc = exc
                fallback_tokens = list(chunk)
                category = _error_category("batch", exc)
                fallback_reasons = {token_id: category for token_id in chunk}
                add_failure_category(category, len(chunk))

            if fallback_tokens:
                fallback_failures: dict[str, Exception] = {}
                for token_id in fallback_tokens:
                    try:
                        raw_book = self.fetch_order_book(token_id)
                    except Exception as exc:
                        fallback_failures[token_id] = exc
                        add_failure_category(_error_category("fallback", exc))
                        add_failed_token_sample(token_id)
                        continue
                    try:
                        ask_books[token_id] = asks_from_book(
                            raw_book,
                            token_id=token_id,
                            source="rest_book_fallback",
                            updated_at=datetime.now(timezone.utc),
                        )
                    except Exception as exc:
                        fallback_failures[token_id] = exc
                        add_failure_category(_error_category("fallback_parse", exc))
                        add_failed_token_sample(token_id)
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
                    failed_token_sample=failed_token_sample,
                    failure_categories=failure_categories,
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
                failed_token_sample=failed_token_sample,
                failure_categories=failure_categories,
            )
        return ask_books

    def fetch_ask_books_with_evidence(
        self,
        token_ids: Iterable[str],
    ) -> dict[str, Any]:
        unique_token_ids = list(dict.fromkeys(str(token_id) for token_id in token_ids if token_id))
        ask_books: dict[str, Any] = {}
        request_records: list[dict[str, Any]] = []
        errors: dict[str, str] = {}
        received_at = datetime.now(timezone.utc)

        for chunk in _chunked(unique_token_ids, self.batch_book_limit):
            batch_meta: dict[str, Any] = {
                "endpoint_family": "clob_books",
                "endpoint": "/books",
                "token_count": len(chunk),
                "documented_limit_bucket": f"batch_size<={self.batch_book_limit}",
            }
            fallback_tokens: list[str] = []
            try:
                raw_books = self.fetch_order_books_batch(chunk, request_meta=batch_meta)
                for token_id in chunk:
                    raw_book = raw_books.get(token_id)
                    if raw_book is None:
                        fallback_tokens.append(token_id)
                        errors[token_id] = "batch:missing_book"
                        continue
                    try:
                        ask_books[token_id] = asks_from_book(
                            raw_book,
                            token_id=token_id,
                            source="rest_books_batch",
                            updated_at=received_at,
                        )
                    except Exception as exc:
                        fallback_tokens.append(token_id)
                        errors[token_id] = _error_category("batch_parse", exc)
            except Exception as exc:
                fallback_tokens = list(chunk)
                errors.update({token_id: _error_category("batch", exc) for token_id in chunk})
            request_records.append(_jsonable_request_meta(batch_meta))

            for token_id in fallback_tokens:
                fallback_meta: dict[str, Any] = {
                    "endpoint_family": "clob_book",
                    "endpoint": "/book",
                    "token_count": 1,
                    "token_id": token_id,
                    "documented_limit_bucket": "single_book",
                }
                try:
                    raw_book = self.fetch_order_book(token_id, request_meta=fallback_meta)
                    ask_books[token_id] = asks_from_book(
                        raw_book,
                        token_id=token_id,
                        source="rest_book_fallback",
                        updated_at=datetime.now(timezone.utc),
                    )
                    errors.pop(token_id, None)
                except Exception as exc:
                    errors[token_id] = _error_category("fallback", exc)
                    fallback_meta.setdefault("error", f"{type(exc).__name__}: {exc}")
                request_records.append(_jsonable_request_meta(fallback_meta))

        return {
            "books": ask_books,
            "request_records": request_records,
            "errors": errors,
            "total_tokens": len(unique_token_ids),
            "received_books": len(ask_books),
            "failed_tokens": len(errors),
        }
