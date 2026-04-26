from __future__ import annotations

import os
from pathlib import Path

from weather_arb_live import config


def test_default_clob_host_uses_production_books(monkeypatch):
    monkeypatch.delenv("POLYMARKET_CLOB_HOST", raising=False)

    assert config.default_clob_host() == "https://clob.polymarket.com"
    assert config.clob_host() == "https://clob.polymarket.com"


def test_clob_host_env_override_still_allows_v2_test_host(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob-v2.polymarket.com")

    assert config.clob_host() == "https://clob-v2.polymarket.com"


def test_load_dotenv_sets_values_without_overriding_existing_env(monkeypatch):
    env_path = Path("data/test_config_dotenv.env")
    env_path.parent.mkdir(exist_ok=True)
    try:
        env_path.write_text(
            "\n".join(
                [
                    "# comment",
                    "MAX_POSITION_USD=2.50",
                    'POLYMARKET_CLOB_HOST="https://example.invalid"',
                    "export LIVE_MARKET_LIMIT=10",
                ]
            ),
            encoding="utf-8",
        )
        monkeypatch.delenv("MAX_POSITION_USD", raising=False)
        monkeypatch.delenv("LIVE_MARKET_LIMIT", raising=False)
        monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://already-set.invalid")

        config.load_dotenv(env_path)

        assert os.getenv("MAX_POSITION_USD") == "2.50"
        assert os.getenv("LIVE_MARKET_LIMIT") == "10"
        assert os.getenv("POLYMARKET_CLOB_HOST") == "https://already-set.invalid"
    finally:
        env_path.unlink(missing_ok=True)


def test_max_position_usd_uses_env_cap(monkeypatch):
    monkeypatch.setenv("MAX_POSITION_USD", "2.50")

    assert config.max_position_usd() == 2.5


def test_max_position_usd_rejects_non_positive_env(monkeypatch):
    monkeypatch.setenv("MAX_POSITION_USD", "0")

    try:
        config.max_position_usd()
    except ValueError as exc:
        assert "MAX_POSITION_USD" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def test_event_snapshot_interval_uses_minutes_with_one_minimum(monkeypatch):
    monkeypatch.setenv("EVENT_SNAPSHOT_INTERVAL_MINUTES", "0.5")

    assert config.event_snapshot_interval_seconds() == 60.0
