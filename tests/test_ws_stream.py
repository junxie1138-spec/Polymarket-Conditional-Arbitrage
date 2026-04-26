from __future__ import annotations

import json
import logging
from pathlib import Path

from weather_arb_live.event_log import LiveEventLog
from weather_arb_live.ws_stream import (
    BestBidAskCache,
    PolymarketMarketStream,
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


def test_market_stream_records_market_resolved_event():
    event_path = Path("data/test_ws_market_events.jsonl")
    market_path = Path("data/test_ws_market_snapshots.jsonl")
    forecast_path = Path("data/test_ws_forecast_snapshots.jsonl")
    for path in (event_path, market_path, forecast_path):
        path.unlink(missing_ok=True)
    event_log = LiveEventLog(
        event_path=event_path,
        market_snapshot_path=market_path,
        forecast_snapshot_path=forecast_path,
    )
    stream = PolymarketMarketStream(
        cache=BestBidAskCache(),
        base_url="wss://example.invalid/ws",
        logger_=logging.getLogger("test_market_stream_events"),
        event_log=event_log,
    )

    try:
        stream._handle_message(
            {
                "event_type": "market_resolved",
                "market": "0xabc",
                "payout": "1",
                "timestamp": "2026-04-26T12:00:00Z",
            }
        )

        rows = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]
        assert rows[0]["event_type"] == "market_resolved"
        assert rows[0]["market_id"] == "0xabc"
        assert rows[0]["final_resolved_payout"] == 1.0
    finally:
        for path in (event_path, market_path, forecast_path):
            path.unlink(missing_ok=True)
