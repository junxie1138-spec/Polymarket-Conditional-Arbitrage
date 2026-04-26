from __future__ import annotations

import json
from pathlib import Path

from weather_arb_live.event_log import LiveEventLog, order_lifecycle_events_from_payload


def _jsonl(path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_event_log_appends_jsonl_and_enriches_known_market_context():
    event_path = Path("data/test_event_log_events.jsonl")
    market_path = Path("data/test_event_log_market.jsonl")
    forecast_path = Path("data/test_event_log_forecast.jsonl")
    for path in (event_path, market_path, forecast_path):
        path.unlink(missing_ok=True)
    log = LiveEventLog(
        event_path=event_path,
        market_snapshot_path=market_path,
        forecast_snapshot_path=forecast_path,
    )

    try:
        log.append_event("bot_started", {"dry_run": True})
        log.remember_market_context(
            {
                "market_id": "m1",
                "condition_id": "c1",
                "token_id": "yes-token",
                "city": "New York",
                "target_date": "2026-04-27",
                "bracket": {"low": 70.0, "high": None, "unit": "F", "metric": "max"},
                "side": "YES",
                "model_probability": 0.8,
                "intended_edge": 0.2,
            }
        )
        log.append_event("order_filled", {"token_id": "yes-token", "filled_price": 0.42, "fill_quantity": 3})
        log.append_market_snapshot({"market_id": "m1", "midpoint": 0.43})
        log.append_forecast_snapshot({"market_id": "m1", "model_probability": 0.81})

        rows = _jsonl(event_path)
        assert [row["event_type"] for row in rows] == ["bot_started", "order_filled"]
        assert rows[1]["city"] == "New York"
        assert rows[1]["bracket"]["low"] == 70.0
        assert rows[1]["filled_price"] == 0.42
        assert rows[1]["realized_pnl"] is None
        assert _jsonl(market_path)[0]["snapshot_type"] == "market"
        assert _jsonl(forecast_path)[0]["snapshot_type"] == "forecast"
    finally:
        for path in (event_path, market_path, forecast_path):
            path.unlink(missing_ok=True)


def test_user_stream_payloads_map_to_required_lifecycle_events():
    events = order_lifecycle_events_from_payload(
        {
            "event_type": "trade",
            "market": "c1",
            "asset_id": "yes-token",
            "side": "BUY",
            "price": "0.42",
            "size": "3",
            "remaining_size": "2",
            "fee": "0.01",
            "timestamp": "2026-04-26T12:00:00Z",
        }
    )

    assert events[0][0] == "order_partially_filled"
    assert events[0][1]["order_side"] == "BUY"
    assert events[0][1]["filled_price"] == 0.42
    assert events[0][1]["fill_quantity"] == 3.0
    assert events[0][1]["fees"] == 0.01

    cancelled = order_lifecycle_events_from_payload(
        {
            "event_type": "cancellation",
            "market": "c1",
            "asset_id": "yes-token",
            "id": "order-1",
            "timestamp": "2026-04-26T12:05:00Z",
        }
    )

    assert cancelled[0][0] == "order_cancelled"
    assert cancelled[0][1]["cancelled_at_utc"] == "2026-04-26T12:05:00Z"
