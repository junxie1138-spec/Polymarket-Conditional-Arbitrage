import json
from pathlib import Path

from weather_arb_live import dashboard


def _patch_dashboard_paths(monkeypatch) -> tuple[Path, Path, list[Path]]:
    data_dir = Path("data")
    log_dir = Path("logs")
    data_dir.mkdir(exist_ok=True)
    log_dir.mkdir(exist_ok=True)

    weather_cache_path = data_dir / "test_dashboard_weather_cache.json"
    residuals_path = data_dir / "test_dashboard_empirical_residuals.json"
    sigma_path = data_dir / "test_dashboard_sigma_cache.json"
    calibration_path = data_dir / "test_dashboard_calibration_table.json"
    patched_positions_path = data_dir / "test_dashboard_live_positions.json"
    patched_log_path = log_dir / "test_dashboard_live_bot.log"

    monkeypatch.setattr(dashboard.config, "DATA_DIR", data_dir)
    monkeypatch.setattr(dashboard.config, "LOG_DIR", log_dir)
    monkeypatch.setattr(dashboard.config, "POSITIONS_PATH", patched_positions_path)
    monkeypatch.setattr(dashboard.config, "WEATHER_CACHE_PATH", weather_cache_path)
    monkeypatch.setattr(dashboard.config, "RESIDUALS_CACHE_PATH", residuals_path)
    monkeypatch.setattr(dashboard.config, "SIGMA_CACHE_PATH", sigma_path)
    monkeypatch.setattr(dashboard.config, "CALIBRATION_PATH", calibration_path)
    monkeypatch.setattr(dashboard, "LOG_PATH", patched_log_path)
    cleanup_paths = [
        weather_cache_path,
        residuals_path,
        sigma_path,
        calibration_path,
        patched_positions_path,
        patched_log_path,
    ]
    return patched_positions_path, patched_log_path, cleanup_paths


def test_dashboard_state_summarizes_positions_and_hides_secret_values(monkeypatch):
    positions_path, log_path, cleanup_paths = _patch_dashboard_paths(monkeypatch)
    try:
        positions_path.write_text(
            json.dumps(
                {
                    "m1": {
                        "market_id": "m1",
                        "token_id": "yes-token",
                        "side": "YES",
                        "question": "Will NYC hit 80F?",
                        "city": "New York",
                        "target_date": "2026-04-27",
                        "entry_price": 0.3,
                        "shares": 166.67,
                        "position_usd": 50,
                        "forecast_prob": 0.8,
                        "edge": 0.5,
                        "entry_time": "2026-04-25T08:00:00+00:00",
                        "dry_run": True,
                        "order_response": {"dry_run": True},
                    },
                    "m2": {
                        "market_id": "m2",
                        "token_id": "no-token",
                        "side": "NO",
                        "question": "Will Chicago stay below 70F?",
                        "city": "Chicago",
                        "target_date": "2026-04-28",
                        "entry_price": 0.5,
                        "shares": 50,
                        "position_usd": 25,
                        "forecast_prob": 0.7,
                        "edge": 0.2,
                        "entry_time": "2026-04-25T09:00:00+00:00",
                        "dry_run": False,
                        "order_response": {"posted": "unknown"},
                        "reconciliation": {
                            "status": "missing_exchange_match",
                            "requires_manual_review": True,
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        log_path.write_text(
            "\n".join(
                [
                    "2026-04-25 08:00:00,000 INFO weather_arb_live cycle_start at=2026-04-25T08:00:00+00:00",
                    "2026-04-25 08:00:01,000 WARNING weather_arb_live cycle_retry_after seconds=60",
                    "2026-04-25 08:00:02,000 INFO weather_arb_live cycle_end positions=2",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv("DRY_RUN", "false")
        monkeypatch.setenv("POLL_INTERVAL_MINUTES", "10")
        monkeypatch.setenv("POLYMARKET_API_KEY", "secret-api-key")

        state = dashboard.build_dashboard_state(log_limit=10)

        summary = state["positions"]["summary"]
        assert summary["total"] == 2
        assert summary["dry_run"] == 1
        assert summary["live"] == 1
        assert summary["yes_count"] == 1
        assert summary["no_count"] == 1
        assert summary["unknown_posted"] == 1
        assert summary["manual_review"] == 1
        assert summary["total_position_usd"] == 75
        assert state["positions"]["recent"][0]["market_id"] == "m2"
        assert state["logs"]["level_counts"]["WARNING"] == 1
        assert state["logs"]["last_cycle_end"] == "2026-04-25 08:00:02,000"
        assert state["environment"]["live_credentials_ready"] is False
        assert "POLYMARKET_API_SECRET" in state["environment"]["missing_required"]
        assert "secret-api-key" not in json.dumps(state)
        assert any(
            variable["name"] == "POLYMARKET_API_KEY" and variable["present"]
            for variable in state["environment"]["variables"]
        )
    finally:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)


def test_tail_lines_returns_only_requested_recent_lines():
    path = Path("logs/test_dashboard_tail.log")
    path.parent.mkdir(exist_ok=True)
    try:
        path.write_text("\n".join(f"line {index}" for index in range(10)), encoding="utf-8")

        lines, error = dashboard.tail_lines(path, 3)

        assert error is None
        assert lines == ["line 7", "line 8", "line 9"]
    finally:
        path.unlink(missing_ok=True)


def test_parse_log_lines_keeps_unstructured_lines():
    parsed = dashboard.parse_log_lines(
        [
            "2026-04-25 08:00:00,000 INFO weather_arb_live cycle_start at=now",
            "partial line without formatter",
        ]
    )

    assert parsed["entries"][0]["level"] == "INFO"
    assert parsed["last_cycle_start"] == "2026-04-25 08:00:00,000"
    assert parsed["entries"][1]["message"] == "partial line without formatter"
