from __future__ import annotations

import json
import zipfile
from pathlib import Path

from weather_arb_live import daily_report


def _append_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload) + "\n")


def test_daily_report_writes_xlsx_and_skips_incomplete_jsonl(monkeypatch):
    event_path = Path("data/test_daily_report_events.jsonl")
    market_path = Path("data/test_daily_report_markets.jsonl")
    forecast_path = Path("data/test_daily_report_forecasts.jsonl")
    output_path = Path("data/test_daily_report.xlsx")
    for path in (event_path, market_path, forecast_path, output_path):
        path.unlink(missing_ok=True)

    monkeypatch.setattr(daily_report.config, "EVENT_LOG_PATH", event_path)
    monkeypatch.setattr(daily_report.config, "MARKET_SNAPSHOT_PATH", market_path)
    monkeypatch.setattr(daily_report.config, "FORECAST_SNAPSHOT_PATH", forecast_path)

    try:
        _append_json(
            event_path,
            {
                "timestamp_utc": "2026-04-26T01:00:00Z",
                "event_type": "signal_generated",
                "market_id": "m1",
                "city": "Austin",
                "target_date": "2026-04-27",
                "bracket": {"low": 80, "high": None, "unit": "F"},
                "side": "YES",
                "model_probability": 0.72,
                "intended_edge": 0.16,
                "best_bid": 0.54,
                "best_ask": 0.58,
                "midpoint": 0.56,
            },
        )
        _append_json(
            event_path,
            {
                "timestamp_utc": "2026-04-26T01:03:00Z",
                "event_type": "order_submitted",
                "market_id": "m1",
                "submitted_limit_price": 0.58,
            },
        )
        _append_json(
            event_path,
            {
                "timestamp_utc": "2026-04-26T01:04:00Z",
                "event_type": "order_filled",
                "market_id": "m1",
                "intended_edge": 0.16,
                "midpoint": 0.56,
                "filled_price": 0.57,
                "fill_quantity": 4,
                "fees": 0.02,
            },
        )
        event_path.write_text(event_path.read_text(encoding="utf-8") + '{"timestamp_utc":', encoding="utf-8")

        _append_json(
            market_path,
            {
                "timestamp_utc": "2026-04-26T01:05:00Z",
                "market_id": "m1",
                "best_bid": 0.55,
                "best_ask": 0.59,
                "midpoint": 0.57,
            },
        )
        _append_json(
            forecast_path,
            {
                "timestamp_utc": "2026-04-26T01:05:00Z",
                "market_id": "m1",
                "model_probability": 0.73,
            },
        )

        output = daily_report.create_daily_report(daily_report.parse_report_date("2026-04-26"), output_path)

        assert output == output_path
        assert output.exists()
        with zipfile.ZipFile(output) as zf:
            names = set(zf.namelist())
            workbook = zf.read("xl/workbook.xml").decode("utf-8")
            summary = zf.read("xl/worksheets/sheet1.xml").decode("utf-8")
            market_quality = zf.read("xl/worksheets/sheet3.xml").decode("utf-8")
        assert "xl/worksheets/sheet7.xml" in names
        assert "Market Quality" in workbook
        assert "Skipped malformed/incomplete lines" in summary
        assert "Austin" in market_quality
    finally:
        for path in (event_path, market_path, forecast_path, output_path):
            path.unlink(missing_ok=True)
