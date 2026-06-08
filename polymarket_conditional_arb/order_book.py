from __future__ import annotations

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
        return book.get("asks") or book.get("sell") or []
    return book.get("bids") or book.get("buy") or []


def _price_size(level: Any) -> tuple[float | None, float | None]:
    if isinstance(level, dict):
        price = _as_float(level.get("price"))
        size = _as_float(level.get("size") or level.get("quantity") or level.get("amount"))
        return price, size
    if isinstance(level, (list, tuple)) and len(level) >= 2:
        return _as_float(level[0]), _as_float(level[1])
    return None, None


def normalize_book_side(
    book: dict[str, Any],
    *,
    token_id: str,
    side: BookSideName,
    source: str = "rest_book",
    updated_at: datetime | None = None,
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
    )


def asks_from_book(
    book: dict[str, Any],
    *,
    token_id: str,
    source: str = "rest_book",
    updated_at: datetime | None = None,
) -> OrderBookSide:
    return normalize_book_side(
        book,
        token_id=token_id,
        side="ask",
        source=source,
        updated_at=updated_at,
    )


def bids_from_book(
    book: dict[str, Any],
    *,
    token_id: str,
    source: str = "rest_book",
    updated_at: datetime | None = None,
) -> OrderBookSide:
    return normalize_book_side(
        book,
        token_id=token_id,
        side="bid",
        source=source,
        updated_at=updated_at,
    )


def is_crossed_book(book: dict[str, Any]) -> bool:
    bids = normalize_book_side(book, token_id=str(book.get("asset_id") or ""), side="bid")
    asks = normalize_book_side(book, token_id=str(book.get("asset_id") or ""), side="ask")
    return (
        bids.best_price is not None
        and asks.best_price is not None
        and bids.best_price > asks.best_price
    )

