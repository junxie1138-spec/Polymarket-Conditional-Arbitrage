from __future__ import annotations

import json

from weather_arb_live.ws_stream import (
    BestBidAskCache,
    unique_market_condition_ids,
    unique_market_token_ids,
)


def test_best_bid_ask_cache_updates_from_market_messages():
    cache = BestBidAskCache()

    assert (
        cache.apply_message(
            {
                "event_type": "book",
                "asset_id": "yes-token",
                "bids": [{"price": "0.40"}, {"price": "0.39"}],
                "asks": [{"price": "0.44"}, {"price": "0.45"}],
            },
            now=100.0,
        )
        == 1
    )
    assert cache.midpoint("yes-token", max_age_seconds=10, now=105.0) == 0.42

    assert (
        cache.apply_message(
            {
                "event_type": "price_change",
                "market": "0xabc",
                "price_changes": [
                    {
                        "asset_id": "yes-token",
                        "best_bid": "0.41",
                        "best_ask": "0.43",
                    }
                ],
            },
            now=106.0,
        )
        == 1
    )
    assert cache.midpoint("yes-token", max_age_seconds=10, now=107.0) == 0.42
    assert cache.midpoint("yes-token", max_age_seconds=0.5, now=107.0) is None


def test_market_subscription_helpers_dedupe_and_cap_ids():
    markets = [
        {
            "conditionId": "0xabc",
            "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        },
        {
            "conditionId": "0xdef",
            "clobTokenIds": ["yes-token", "other-no-token"],
        },
    ]

    assert unique_market_token_ids(markets, max_tokens=3) == [
        "yes-token",
        "no-token",
        "other-no-token",
    ]
    assert unique_market_condition_ids(markets) == ["0xabc", "0xdef"]
