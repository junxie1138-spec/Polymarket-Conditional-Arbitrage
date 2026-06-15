from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from .arb_models import BookLevel, BookSideName, OrderBookSide, as_float
from .order_book import asks_from_book, bids_from_book

MarketWsSide = Literal["BUY", "SELL", "bid", "ask"]


DEFAULT_MARKET_WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
DEFAULT_MARKET_WS_MAX_MESSAGE_SIZE_BYTES = 8_388_608
DEFAULT_SKIPPED_SNAPSHOT_WARNING_INTERVAL_SECONDS = 60.0
DEFAULT_RECENT_PUBLIC_EVIDENCE_LIMIT = 50


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _token_id_from_payload(payload: Mapping[str, Any]) -> str | None:
    for key in ("asset_id", "assetId", "token_id", "tokenId"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _side_name(value: Any) -> BookSideName | None:
    normalized = str(value or "").strip().upper()
    if normalized == "BUY":
        return "bid"
    if normalized == "SELL":
        return "ask"
    if normalized == "BID":
        return "bid"
    if normalized == "ASK":
        return "ask"
    return None


def _token_ids_from_payload(payload: Mapping[str, Any]) -> list[str]:
    token_id = _token_id_from_payload(payload)
    if token_id is not None:
        return [token_id]
    for key in ("asset_ids", "assetIds", "token_ids", "tokenIds"):
        value = payload.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if item not in (None, "")]
    return []


def _market_key_from_payload(payload: Mapping[str, Any]) -> str | None:
    for key in ("market_id", "marketId", "market", "condition_id", "conditionId"):
        value = payload.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def _sorted_levels(side: BookSideName, levels_by_price: Mapping[float, float]) -> tuple[BookLevel, ...]:
    levels = [
        BookLevel(price=price, size=size)
        for price, size in levels_by_price.items()
        if 0.0 < price < 1.0 and size > 0.0
    ]
    levels.sort(key=lambda level: level.price, reverse=(side == "bid"))
    return tuple(levels)


class MarketDataCache:
    """Normalized in-memory orderbook cache keyed by Polymarket token id."""

    def __init__(
        self,
        *,
        skipped_snapshot_warning_interval_seconds: float = DEFAULT_SKIPPED_SNAPSHOT_WARNING_INTERVAL_SECONDS,
        recent_public_evidence_limit: int = DEFAULT_RECENT_PUBLIC_EVIDENCE_LIMIT,
    ) -> None:
        self._books: dict[tuple[str, BookSideName], OrderBookSide] = {}
        self._ready_tokens: set[str] = set()
        self._snapshot_generations: dict[str, int] = {}
        self._recent_price_changes: dict[str, list[dict[str, Any]]] = {}
        self._recent_trade_prints: dict[str, list[dict[str, Any]]] = {}
        self._recent_tick_size_changes: dict[str, list[dict[str, Any]]] = {}
        self._recent_best_bid_asks: dict[str, list[dict[str, Any]]] = {}
        self._recent_market_resolved_by_token: dict[str, list[dict[str, Any]]] = {}
        self._recent_market_resolved_by_market: dict[str, list[dict[str, Any]]] = {}
        self._recent_public_evidence_limit = max(1, int(recent_public_evidence_limit))
        self._skipped_snapshot_warning_interval_seconds = max(
            0.0,
            float(skipped_snapshot_warning_interval_seconds),
        )
        self._last_skipped_snapshot_warning_at: float | None = None
        self._suppressed_skipped_snapshot_warnings = 0
        self._lock = threading.RLock()

    def _append_recent_locked(
        self,
        target: dict[str, list[dict[str, Any]]],
        token_id: str,
        record: Mapping[str, Any],
    ) -> None:
        rows = target.setdefault(str(token_id), [])
        rows.append(dict(record))
        excess = len(rows) - self._recent_public_evidence_limit
        if excess > 0:
            del rows[:excess]

    def _mark_snapshot_ready_locked(self, token_id: str) -> None:
        normalized_token_id = str(token_id)
        self._ready_tokens.add(normalized_token_id)
        self._snapshot_generations[normalized_token_id] = self._snapshot_generations.get(normalized_token_id, 0) + 1

    def _mark_snapshot_not_ready_locked(self, token_id: str) -> None:
        normalized_token_id = str(token_id)
        self._ready_tokens.discard(normalized_token_id)
        self._snapshot_generations[normalized_token_id] = self._snapshot_generations.get(normalized_token_id, 0) + 1

    def is_snapshot_ready(self, token_id: str) -> bool:
        with self._lock:
            return str(token_id) in self._ready_tokens

    def snapshot_generation(self, token_id: str) -> int:
        with self._lock:
            return self._snapshot_generations.get(str(token_id), 0)

    def set_book_side(self, book: OrderBookSide) -> None:
        with self._lock:
            self._books[(book.token_id, book.side)] = book
            self._mark_snapshot_ready_locked(book.token_id)

    def seed_ask_books(self, books_by_token: Mapping[str, OrderBookSide]) -> set[str]:
        updated: set[str] = set()
        with self._lock:
            for token_id, book in books_by_token.items():
                if book.side != "ask":
                    continue
                normalized_token_id = str(token_id)
                self._books[(normalized_token_id, "ask")] = book
                self._mark_snapshot_ready_locked(normalized_token_id)
                updated.add(normalized_token_id)
        return updated

    def remove_tokens(self, token_ids: Iterable[str]) -> None:
        token_set = {str(token_id) for token_id in token_ids}
        with self._lock:
            for key in list(self._books):
                if key[0] in token_set:
                    self._books.pop(key, None)
            for token_id in token_set:
                self._ready_tokens.discard(token_id)
                self._snapshot_generations.pop(token_id, None)
                self._recent_price_changes.pop(token_id, None)
                self._recent_trade_prints.pop(token_id, None)
                self._recent_tick_size_changes.pop(token_id, None)
                self._recent_best_bid_asks.pop(token_id, None)
                self._recent_market_resolved_by_token.pop(token_id, None)

    def market_resolution_snapshot(
        self,
        market_ids: Iterable[str] | None = None,
    ) -> dict[str, list[dict[str, Any]]]:
        allowed = {str(market_id) for market_id in market_ids} if market_ids is not None else None
        with self._lock:
            source = self._recent_market_resolved_by_market
            if allowed is None:
                return {market_id: [dict(row) for row in rows] for market_id, rows in source.items()}
            return {
                market_id: [dict(row) for row in source.get(market_id, [])]
                for market_id in sorted(allowed)
                if market_id in source
            }

    def mark_tokens_stale(self, token_ids: Iterable[str], *, stale_at: datetime) -> None:
        token_set = {str(token_id) for token_id in token_ids}
        timestamp = _ensure_aware(stale_at)
        with self._lock:
            for key, book in list(self._books.items()):
                if key[0] in token_set:
                    self._books[key] = OrderBookSide(
                        token_id=book.token_id,
                        side=book.side,
                        levels=book.levels,
                        source=f"{book.source}_stale",
                        updated_at=timestamp,
                        source_revision=book.source_revision,
                        source_hash=book.source_hash,
                    )
            for token_id in token_set:
                self._mark_snapshot_not_ready_locked(token_id)

    def book_side(self, token_id: str, side: BookSideName) -> OrderBookSide | None:
        with self._lock:
            return self._books.get((str(token_id), side))

    def ask_books_snapshot(self, token_ids: Iterable[str] | None = None) -> dict[str, OrderBookSide]:
        allowed = {str(token_id) for token_id in token_ids} if token_ids is not None else None
        with self._lock:
            return {
                token_id: book
                for (token_id, side), book in self._books.items()
                if side == "ask" and (allowed is None or token_id in allowed)
            }

    def public_evidence_snapshot(
        self,
        token_ids: Iterable[str] | None = None,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
    ) -> dict[str, dict[str, Any]]:
        allowed = {str(token_id) for token_id in token_ids} if token_ids is not None else None
        since_aware = _ensure_aware(since) if since is not None else None
        until_aware = _ensure_aware(until) if until is not None else None

        def within_window(row: Mapping[str, Any]) -> bool:
            observed_at = row.get("observed_at")
            if isinstance(observed_at, datetime):
                observed = _ensure_aware(observed_at)
                if since_aware is not None and observed < since_aware:
                    return False
                if until_aware is not None and observed > until_aware:
                    return False
            return True

        def filtered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
            return [dict(row) for row in rows if within_window(row)]

        with self._lock:
            token_set = (
                set(allowed)
                if allowed is not None
                else {
                    *(token_id for token_id, _side in self._books),
                    *self._recent_price_changes,
                    *self._recent_trade_prints,
                    *self._recent_tick_size_changes,
                    *self._recent_best_bid_asks,
                }
            )
            snapshot: dict[str, dict[str, Any]] = {}
            for token_id in sorted(token_set):
                books: dict[str, dict[str, Any]] = {}
                for side in ("bid", "ask"):
                    book = self._books.get((token_id, side))
                    if book is None:
                        continue
                    books[side] = {
                        "source": book.source,
                        "updated_at": book.updated_at,
                        "source_revision": book.source_revision,
                        "source_hash": book.source_hash,
                        "best_price": book.best_price,
                        "available_size": book.available_size,
                    }
                snapshot[token_id] = {
                    "token_id": token_id,
                    "snapshot_ready": token_id in self._ready_tokens,
                    "snapshot_generation": self._snapshot_generations.get(token_id, 0),
                    "books": books,
                    "recent_price_changes": filtered(self._recent_price_changes.get(token_id, [])),
                    "recent_trade_prints": filtered(self._recent_trade_prints.get(token_id, [])),
                    "recent_tick_size_changes": filtered(self._recent_tick_size_changes.get(token_id, [])),
                    "recent_best_bid_asks": filtered(self._recent_best_bid_asks.get(token_id, [])),
                    "recent_market_resolved": filtered(self._recent_market_resolved_by_token.get(token_id, [])),
                }
            return snapshot

    def apply_message(
        self,
        raw_message: str | bytes | Mapping[str, Any] | list[Any],
        *,
        received_at: datetime | None = None,
        logger: logging.Logger | None = None,
    ) -> set[str]:
        timestamp = _ensure_aware(received_at or _utc_now())
        try:
            payload = self._decode_message(raw_message)
        except ValueError as exc:
            if logger is not None:
                logger.warning("market_ws_malformed_message error=%s", exc)
            return set()
        return self.apply_payload(payload, received_at=timestamp, logger=logger)

    def apply_payload(
        self,
        payload: Mapping[str, Any] | list[Any],
        *,
        received_at: datetime | None = None,
        logger: logging.Logger | None = None,
    ) -> set[str]:
        timestamp = _ensure_aware(received_at or _utc_now())
        if isinstance(payload, list):
            updated: set[str] = set()
            for item in payload:
                if isinstance(item, Mapping):
                    updated.update(self.apply_payload(item, received_at=timestamp, logger=logger))
            return updated

        event_type = str(payload.get("event_type") or payload.get("type") or "").strip().lower()
        if event_type == "book":
            return self._apply_book_snapshot(payload, received_at=timestamp, logger=logger)
        if event_type == "price_change":
            return self._apply_price_change(payload, received_at=timestamp, logger=logger)
        if event_type == "last_trade_price":
            return self._apply_last_trade_price(payload, received_at=timestamp, logger=logger)
        if event_type == "tick_size_change":
            return self._apply_tick_size_change(payload, received_at=timestamp, logger=logger)
        if event_type == "best_bid_ask":
            return self._apply_best_bid_ask(payload, received_at=timestamp, logger=logger)
        if event_type == "market_resolved":
            return self._apply_market_resolved(payload, received_at=timestamp, logger=logger)
        return set()

    @staticmethod
    def _decode_message(raw_message: str | bytes | Mapping[str, Any] | list[Any]) -> Mapping[str, Any] | list[Any]:
        if isinstance(raw_message, Mapping) or isinstance(raw_message, list):
            return raw_message
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8", errors="replace")
        message = raw_message.strip()
        if message in {"", "PING", "PONG"}:
            return {}
        try:
            decoded = json.loads(message)
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
        if not isinstance(decoded, (Mapping, list)):
            raise ValueError(f"unexpected JSON message type {type(decoded).__name__}")
        return decoded

    def _apply_book_snapshot(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        logger: logging.Logger | None,
    ) -> set[str]:
        token_id = _token_id_from_payload(payload)
        if not token_id:
            if logger is not None:
                logger.warning("market_ws_book_missing_token")
            return set()
        raw_book = dict(payload)
        bid_book = bids_from_book(raw_book, token_id=token_id, source="ws_book", updated_at=received_at)
        ask_book = asks_from_book(raw_book, token_id=token_id, source="ws_book", updated_at=received_at)
        with self._lock:
            self._books[(token_id, "bid")] = bid_book
            self._books[(token_id, "ask")] = ask_book
            self._mark_snapshot_ready_locked(token_id)
        return {token_id}

    def _apply_price_change(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        logger: logging.Logger | None,
    ) -> set[str]:
        changes = payload.get("price_changes")
        if not isinstance(changes, list):
            if logger is not None:
                logger.warning("market_ws_price_change_missing_changes")
            return set()

        updated: set[str] = set()
        skipped_without_snapshot = 0
        with self._lock:
            for change in changes:
                if not isinstance(change, Mapping):
                    continue
                token_id = _token_id_from_payload(change)
                side = _side_name(change.get("side"))
                price = as_float(change.get("price"))
                size = as_float(change.get("size"))
                if token_id is None or side is None or price is None or size is None:
                    continue
                if not 0.0 < price < 1.0:
                    continue

                current = self._books.get((token_id, side))
                if current is None or token_id not in self._ready_tokens:
                    skipped_without_snapshot += 1
                    continue
                levels_by_price = {
                    level.price: level.size
                    for level in current.levels
                }
                old_size = levels_by_price.get(price, 0.0)
                revision = _source_revision(payload, change) or current.source_revision
                source_hash = _source_hash(payload, change) or current.source_hash
                self._append_recent_locked(
                    self._recent_price_changes,
                    token_id,
                    {
                        "event_type": "price_change",
                        "token_id": token_id,
                        "side": side,
                        "price": price,
                        "old_size": old_size,
                        "new_size": max(0.0, size),
                        "delta_size": max(0.0, size) - old_size,
                        "source_revision": revision,
                        "source_hash": source_hash,
                        "observed_at": received_at,
                    },
                )
                if size <= 0.0:
                    levels_by_price.pop(price, None)
                else:
                    levels_by_price[price] = size
                self._books[(token_id, side)] = OrderBookSide(
                    token_id=token_id,
                    side=side,
                    levels=_sorted_levels(side, levels_by_price),
                    source="ws_price_change",
                    updated_at=received_at,
                    source_revision=revision,
                    source_hash=source_hash,
                )
                updated.add(token_id)
        self._log_skipped_snapshot_warning(skipped_without_snapshot, logger)
        return updated

    def _apply_last_trade_price(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        logger: logging.Logger | None,
    ) -> set[str]:
        raw_trades = payload.get("trades") or payload.get("last_trades") or payload.get("lastTradePrices")
        trades = raw_trades if isinstance(raw_trades, list) else [payload]
        updated: set[str] = set()
        with self._lock:
            for trade in trades:
                if not isinstance(trade, Mapping):
                    continue
                token_id = _token_id_from_payload(trade) or _token_id_from_payload(payload)
                price = as_float(trade.get("price") or trade.get("last_trade_price") or trade.get("lastTradePrice"))
                size = as_float(
                    trade.get("size")
                    or trade.get("quantity")
                    or trade.get("amount")
                    or trade.get("last_trade_size")
                    or trade.get("lastTradeSize")
                )
                if token_id is None or price is None:
                    continue
                side = str(trade.get("side") or payload.get("side") or "").strip().upper() or None
                fee_bps = as_float(
                    trade.get("fee_rate_bps")
                    or trade.get("feeRateBps")
                    or trade.get("fee")
                    or payload.get("fee_rate_bps")
                    or payload.get("feeRateBps")
                )
                self._append_recent_locked(
                    self._recent_trade_prints,
                    token_id,
                    {
                        "event_type": "last_trade_price",
                        "token_id": token_id,
                        "side": side,
                        "price": price,
                        "size": size,
                        "fee_bps": fee_bps,
                        "source_revision": _source_revision(payload, trade),
                        "source_hash": _source_hash(payload, trade),
                        "observed_at": received_at,
                    },
                )
                updated.add(token_id)
        if not updated and logger is not None:
            logger.warning("market_ws_last_trade_price_missing_token_or_price")
        return updated

    def _apply_tick_size_change(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        logger: logging.Logger | None,
    ) -> set[str]:
        token_ids = _token_ids_from_payload(payload)
        new_tick_size = as_float(
            payload.get("tick_size")
            or payload.get("tickSize")
            or payload.get("new_tick_size")
            or payload.get("newTickSize")
        )
        old_tick_size = as_float(payload.get("old_tick_size") or payload.get("oldTickSize"))
        if not token_ids:
            if logger is not None:
                logger.warning("market_ws_tick_size_change_missing_token")
            return set()
        with self._lock:
            for token_id in token_ids:
                self._append_recent_locked(
                    self._recent_tick_size_changes,
                    token_id,
                    {
                        "event_type": "tick_size_change",
                        "token_id": token_id,
                        "old_tick_size": old_tick_size,
                        "new_tick_size": new_tick_size,
                        "source_revision": _source_revision(payload),
                        "source_hash": _source_hash(payload),
                        "observed_at": received_at,
                    },
                )
        return set(token_ids)

    def _apply_best_bid_ask(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        logger: logging.Logger | None,
    ) -> set[str]:
        token_id = _token_id_from_payload(payload)
        if token_id is None:
            if logger is not None:
                logger.warning("market_ws_best_bid_ask_missing_token")
            return set()
        best_bid = as_float(payload.get("bid") or payload.get("best_bid") or payload.get("bestBid"))
        best_ask = as_float(payload.get("ask") or payload.get("best_ask") or payload.get("bestAsk"))
        with self._lock:
            self._append_recent_locked(
                self._recent_best_bid_asks,
                token_id,
                {
                    "event_type": "best_bid_ask",
                    "token_id": token_id,
                    "best_bid": best_bid,
                    "best_ask": best_ask,
                    "source_revision": _source_revision(payload),
                    "source_hash": _source_hash(payload),
                    "observed_at": received_at,
                },
            )
        return {token_id}

    def _apply_market_resolved(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime,
        logger: logging.Logger | None,
    ) -> set[str]:
        market_key = _market_key_from_payload(payload)
        winning_asset_id = (
            payload.get("winning_asset_id")
            or payload.get("winningAssetId")
            or payload.get("winner_token_id")
            or payload.get("winnerTokenId")
        )
        winning_outcome = payload.get("winning_outcome") or payload.get("winningOutcome")
        token_ids = _token_ids_from_payload(payload)
        if winning_asset_id not in (None, ""):
            token_ids.append(str(winning_asset_id))
        normalized_token_ids = sorted(dict.fromkeys(str(token_id) for token_id in token_ids if token_id))
        if market_key is None and not normalized_token_ids:
            if logger is not None:
                logger.warning("market_ws_market_resolved_missing_market_and_token")
            return set()
        row = {
            "event_type": "market_resolved",
            "market_id": market_key,
            "token_ids": normalized_token_ids,
            "winning_asset_id": str(winning_asset_id) if winning_asset_id not in (None, "") else None,
            "winning_outcome": str(winning_outcome) if winning_outcome not in (None, "") else None,
            "source_revision": _source_revision(payload),
            "source_hash": _source_hash(payload),
            "observed_at": received_at,
        }
        with self._lock:
            if market_key is not None:
                rows = self._recent_market_resolved_by_market.setdefault(str(market_key), [])
                rows.append(dict(row))
                excess = len(rows) - self._recent_public_evidence_limit
                if excess > 0:
                    del rows[:excess]
            for token_id in normalized_token_ids:
                self._append_recent_locked(self._recent_market_resolved_by_token, token_id, row)
        return set(normalized_token_ids)

    def _log_skipped_snapshot_warning(
        self,
        skipped_without_snapshot: int,
        logger: logging.Logger | None,
    ) -> None:
        if skipped_without_snapshot <= 0 or logger is None:
            return

        now = time.monotonic()
        with self._lock:
            if (
                self._last_skipped_snapshot_warning_at is not None
                and now - self._last_skipped_snapshot_warning_at
                < self._skipped_snapshot_warning_interval_seconds
            ):
                self._suppressed_skipped_snapshot_warnings += skipped_without_snapshot
                return

            suppressed_since_last = self._suppressed_skipped_snapshot_warnings
            self._suppressed_skipped_snapshot_warnings = 0
            self._last_skipped_snapshot_warning_at = now

        logger.warning(
            "market_ws_price_change_without_ready_snapshot skipped=%s suppressed_since_last=%s",
            skipped_without_snapshot,
            suppressed_since_last,
        )


def chunk_asset_ids(asset_ids: Iterable[str], max_assets_per_connection: int) -> list[list[str]]:
    chunk_size = max(1, int(max_assets_per_connection))
    unique_ids = sorted(dict.fromkeys(str(asset_id) for asset_id in asset_ids if asset_id))
    return [unique_ids[index : index + chunk_size] for index in range(0, len(unique_ids), chunk_size)]


def market_subscribe_payload(asset_ids: Iterable[str]) -> dict[str, Any]:
    return {
        "assets_ids": list(asset_ids),
        "type": "market",
        "custom_feature_enabled": True,
    }


def market_subscription_update_payload(asset_ids: Iterable[str], operation: Literal["subscribe", "unsubscribe"]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "assets_ids": list(asset_ids),
        "operation": operation,
    }
    if operation == "subscribe":
        payload["custom_feature_enabled"] = True
    return payload


def _source_revision(*payloads: Mapping[str, Any]) -> str | None:
    for payload in payloads:
        for key in (
            "hash",
            "book_hash",
            "bookHash",
            "update_id",
            "updateId",
            "sequence",
            "sequence_id",
            "sequenceId",
            "timestamp",
            "ts",
        ):
            value = payload.get(key)
            if value not in (None, ""):
                return f"{key}:{value}"
    return None


def _source_hash(*payloads: Mapping[str, Any]) -> str | None:
    for payload in payloads:
        for key in ("hash", "book_hash", "bookHash"):
            value = payload.get(key)
            if value not in (None, ""):
                return str(value)
    return None


@dataclass(frozen=True)
class MarketWebSocketSettings:
    endpoint: str = DEFAULT_MARKET_WS_ENDPOINT
    heartbeat_seconds: float = 10.0
    max_assets_per_connection: int = 500
    max_message_size_bytes: int = DEFAULT_MARKET_WS_MAX_MESSAGE_SIZE_BYTES
    reconnect_initial_seconds: float = 1.0
    reconnect_max_seconds: float = 30.0


class _MarketWebSocketWorker:
    def __init__(
        self,
        *,
        settings: MarketWebSocketSettings,
        asset_ids: Iterable[str],
        cache: MarketDataCache,
        logger: logging.Logger,
        connect_factory: Callable[..., Any],
        on_dirty_tokens: Callable[[set[str]], None] | None = None,
        on_connection_lost: Callable[[set[str]], None] | None = None,
        on_connection_error: Callable[[Exception], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.asset_ids = set(str(asset_id) for asset_id in asset_ids if asset_id)
        self.cache = cache
        self.logger = logger
        self.connect_factory = connect_factory
        self.on_dirty_tokens = on_dirty_tokens
        self.on_connection_lost = on_connection_lost
        self.on_connection_error = on_connection_error
        self.on_reconnect = on_reconnect
        self.websocket: Any | None = None
        self._lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    async def run(self) -> None:
        backoff = max(0.01, self.settings.reconnect_initial_seconds)
        while not self._stop_event.is_set():
            connected = False
            heartbeat_task: asyncio.Task[None] | None = None
            try:
                async with self.connect_factory(
                    self.settings.endpoint,
                    max_size=self.settings.max_message_size_bytes,
                ) as websocket:
                    async with self._lock:
                        self.websocket = websocket
                        await websocket.send(json.dumps(market_subscribe_payload(sorted(self.asset_ids))))
                    connected = True
                    self.logger.info("market_ws_connected assets=%s", len(self.asset_ids))
                    heartbeat_task = asyncio.create_task(self._heartbeat_loop(websocket))
                    async for message in websocket:
                        dirty_tokens = self.cache.apply_message(message, logger=self.logger)
                        if dirty_tokens and self.on_dirty_tokens is not None:
                            self.on_dirty_tokens(dirty_tokens)
                    backoff = max(0.01, self.settings.reconnect_initial_seconds)
            except asyncio.CancelledError:
                self._stop_event.set()
                raise
            except Exception as exc:
                self.logger.warning("market_ws_connection_error error=%s", exc)
                if self.on_connection_error is not None:
                    self.on_connection_error(exc)
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await heartbeat_task
                async with self._lock:
                    self.websocket = None
                if connected and self.on_connection_lost is not None:
                    self.on_connection_lost(set(self.asset_ids))

            if not self._stop_event.is_set():
                if self.on_reconnect is not None:
                    self.on_reconnect()
                await asyncio.sleep(backoff)
                backoff = min(self.settings.reconnect_max_seconds, backoff * 2.0)

    async def stop(self) -> None:
        self._stop_event.set()
        async with self._lock:
            websocket = self.websocket
        if websocket is not None:
            close = getattr(websocket, "close", None)
            if close is not None:
                result = close()
                if hasattr(result, "__await__"):
                    await result

    async def update_tokens(self, asset_ids: Iterable[str]) -> None:
        new_ids = set(str(asset_id) for asset_id in asset_ids if asset_id)
        added = sorted(new_ids - self.asset_ids)
        removed = sorted(self.asset_ids - new_ids)
        self.asset_ids = new_ids
        async with self._lock:
            websocket = self.websocket
            if websocket is None:
                return
            if added:
                await websocket.send(json.dumps(market_subscription_update_payload(added, "subscribe")))
            if removed:
                await websocket.send(json.dumps(market_subscription_update_payload(removed, "unsubscribe")))

    async def _heartbeat_loop(self, websocket: Any) -> None:
        while not self._stop_event.is_set():
            await asyncio.sleep(max(0.01, self.settings.heartbeat_seconds))
            await websocket.send("PING")


def _default_connect_factory(endpoint: str, *, max_size: int) -> Any:
    import websockets

    return websockets.connect(endpoint, ping_interval=None, max_size=max_size)


class MarketWebSocketManager:
    def __init__(
        self,
        *,
        settings: MarketWebSocketSettings,
        cache: MarketDataCache,
        logger: logging.Logger | None = None,
        connect_factory: Callable[..., Any] | None = None,
        on_dirty_tokens: Callable[[set[str]], None] | None = None,
        on_connection_lost: Callable[[set[str]], None] | None = None,
        on_connection_error: Callable[[Exception], None] | None = None,
        on_reconnect: Callable[[], None] | None = None,
    ) -> None:
        self.settings = settings
        self.cache = cache
        self.logger = logger or logging.getLogger("polymarket_conditional_arb.market_ws")
        self.connect_factory = connect_factory or _default_connect_factory
        self.on_dirty_tokens = on_dirty_tokens
        self.on_connection_lost = on_connection_lost
        self.on_connection_error = on_connection_error
        self.on_reconnect = on_reconnect
        self._workers: list[_MarketWebSocketWorker] = []
        self._tasks: list[asyncio.Task[None]] = []

    @property
    def connection_count(self) -> int:
        return len(self._workers)

    @property
    def token_chunks(self) -> list[list[str]]:
        return [sorted(worker.asset_ids) for worker in self._workers]

    def _new_worker(self, asset_ids: Iterable[str]) -> _MarketWebSocketWorker:
        return _MarketWebSocketWorker(
            settings=self.settings,
            asset_ids=asset_ids,
            cache=self.cache,
            logger=self.logger,
            connect_factory=self.connect_factory,
            on_dirty_tokens=self.on_dirty_tokens,
            on_connection_lost=self.on_connection_lost,
            on_connection_error=self.on_connection_error,
            on_reconnect=self.on_reconnect,
        )

    async def start(self, asset_ids: Iterable[str]) -> None:
        await self.stop()
        chunks = chunk_asset_ids(asset_ids, self.settings.max_assets_per_connection)
        self._workers = [self._new_worker(chunk) for chunk in chunks]
        self._tasks = [asyncio.create_task(worker.run()) for worker in self._workers]

    async def update_tokens(self, asset_ids: Iterable[str]) -> None:
        chunks = chunk_asset_ids(asset_ids, self.settings.max_assets_per_connection)
        if not chunks:
            await self.stop()
            return
        if not self._workers:
            await self.start(asset_ids)
            return

        shared_workers = min(len(self._workers), len(chunks))
        for index in range(shared_workers):
            await self._workers[index].update_tokens(chunks[index])

        if len(chunks) > len(self._workers):
            new_workers = [self._new_worker(chunk) for chunk in chunks[len(self._workers) :]]
            new_tasks = [asyncio.create_task(worker.run()) for worker in new_workers]
            self._workers.extend(new_workers)
            self._tasks.extend(new_tasks)
            return

        if len(chunks) < len(self._workers):
            extra_workers = self._workers[len(chunks) :]
            extra_tasks = self._tasks[len(chunks) :]
            self._workers = self._workers[: len(chunks)]
            self._tasks = self._tasks[: len(chunks)]
            for worker in extra_workers:
                await worker.stop()
            for task in extra_tasks:
                task.cancel()
            if extra_tasks:
                await asyncio.gather(*extra_tasks, return_exceptions=True)

    async def stop(self) -> None:
        workers = list(self._workers)
        tasks = list(self._tasks)
        for worker in workers:
            await worker.stop()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._workers = []
        self._tasks = []
