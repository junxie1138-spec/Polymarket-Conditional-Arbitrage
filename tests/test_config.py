from __future__ import annotations

from weather_arb_live import config


def test_default_clob_host_uses_production_books(monkeypatch):
    monkeypatch.delenv("POLYMARKET_CLOB_HOST", raising=False)

    assert config.default_clob_host() == "https://clob.polymarket.com"
    assert config.clob_host() == "https://clob.polymarket.com"


def test_clob_host_env_override_still_allows_v2_test_host(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob-v2.polymarket.com")

    assert config.clob_host() == "https://clob-v2.polymarket.com"
