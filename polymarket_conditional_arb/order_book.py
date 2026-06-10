from __future__ import annotations

from collections.abc import Iterable as IterableABC
from datetime import datetime
from typing import Any, Iterable

from .arb_models import BookLevel, BookSideName, OrderBookSide


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


def _raw_levels(book: dict[str, Any], side: BookSideName) -> Iterable[Any]:
    if side == "ask":
        for key in ("asks", "sell"):
            if key in book:
                value = book.get(key)
                return value if isinstance(value, IterableABC) and not isinstance(value, (str, bytes)) else []
        return []
    for key in ("bids", "buy"):
        if key in book:
            value = book.get(key)
            return value if isinstance(value, IterableABC) and not isinstance(value, (str, bytes)) else []
    return []


def _first_present(level: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in level:
            return level[key]
    return None


def _price_size(level: Any) -> tuple[float | None, float | None]:
    if isinstance(level, dict):
        price = _as_float(level.get("price"))
        size = _as_float(_first_present(level, "size", "quantity", "amount"))
        return price, size
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _as_float(level[0]), _as_float(level[1])
    return None, None


def _source_revision(book: dict[str, Any]) -> str | None:
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
        value = book.get(key)
        if value not in (None, ""):
            return f"{key}:{value}"
    return None


def normalize_book_side(
    book: dict[str, Any],
    *,
    token_id: str,
    side: BookSideName,
    source: str = "rest_book",
    updated_at: datetime | None = None,
    source_revision: str | None = None,
) -> OrderBookSide:
    levels: list[BookLevel] = []
    for raw_level in _raw_levels(book, side):
        price, size = _price_size(raw_level)
        if price is None or size is None:
            continue
        if not 0.0 < price < 1.0 or size <= 0.0:
            continue
        levels.append(BookLevel(price=price, size=size))

    levels.sort(key=lambda level: level.price, reverse=(side == "bid"))
    return OrderBookSide(
        token_id=str(token_id),
        side=side,
        levels=tuple(levels),
        source=source,
        updated_at=updated_at,
        source_revision=source_revision or _source_revision(book),
    )


def asks_from_book(
    book: dict[str, Any],
    *,
    token_id: str,
    source: str = "rest_book",
    updated_at: datetime | None = None,
    source_revision: str | None = None,
) -> OrderBookSide:
    return normalize_book_side(
        book,
        token_id=token_id,
        side="ask",
        source=source,
        updated_at=updated_at,
        source_revision=source_revision,
    )


def bids_from_book(
    book: dict[str, Any],
    *,
    token_id: str,
    source: str = "rest_book",
    updated_at: datetime | None = None,
    source_revision: str | None = None,
) -> OrderBookSide:
    return normalize_book_side(
        book,
        token_id=token_id,
        side="bid",
        source=source,
        updated_at=updated_at,
        source_revision=source_revision,
    )


def is_crossed_book(book: dict[str, Any]) -> bool:
    bids = normalize_book_side(book, token_id=str(book.get("asset_id") or ""), side="bid")
    asks = normalize_book_side(book, token_id=str(book.get("asset_id") or ""), side="ask")
    return (
        bids.best_price is not None
        and asks.best_price is not None
        and bids.best_price > asks.best_price
    )
