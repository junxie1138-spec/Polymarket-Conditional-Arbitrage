from __future__ import annotations

import pytest

from polymarket_conditional_arb.order_book import asks_from_book, bids_from_book, is_crossed_book


def test_normalized_asks_sort_by_executable_price_and_ignore_invalid_levels():
    book = {
        "asks": [
            {"price": "0.43", "size": "0"},
            {"price": "1.00", "size": "10"},
            {"price": "0.42", "size": "3"},
            {"price": "0.41", "size": "5"},
        ]
    }

    asks = asks_from_book(book, token_id="yes-token")

    assert [level.price for level in asks.levels] == [0.41, 0.42]
    assert asks.available_size == 8
    assert asks.cost_to_fill(8) == pytest.approx(5 * 0.41 + 3 * 0.42)
    assert asks.vwap_to_fill(8) == pytest.approx((5 * 0.41 + 3 * 0.42) / 8)
    assert asks.cost_to_fill(9) is None


def test_normalized_bids_sort_descending_and_crossed_book_is_detected():
    book = {
        "bids": [{"price": "0.50", "size": "10"}, {"price": "0.52", "size": "1"}],
        "asks": [{"price": "0.51", "size": "10"}],
    }

    bids = bids_from_book(book, token_id="yes-token")

    assert [level.price for level in bids.levels] == [0.52, 0.50]
    assert is_crossed_book(book) is True


def test_order_book_side_captures_exchange_source_revision():
    asks = asks_from_book(
        {
            "timestamp": "1717800000000",
            "asks": [{"price": "0.41", "size": "5"}],
        },
        token_id="yes-token",
    )

    assert asks.source_revision == "timestamp:1717800000000"


def test_explicit_empty_level_list_does_not_fall_back_to_alias():
    asks = asks_from_book(
        {
            "asks": [],
            "sell": [{"price": "0.41", "size": "5"}],
        },
        token_id="yes-token",
    )

    assert asks.levels == ()


def test_explicit_zero_size_does_not_fall_back_to_quantity_alias():
    asks = asks_from_book(
        {
            "asks": [{"price": "0.41", "size": 0, "quantity": "5"}],
        },
        token_id="yes-token",
    )

    assert asks.levels == ()
