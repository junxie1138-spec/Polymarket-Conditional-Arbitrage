from __future__ import annotations

import pytest

from polymarket_conditional_arb import config


def test_load_scan_config_rejects_blank_clob_host(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", " ")
    monkeypatch.delenv("COND_ARB_MARKET_WS_ENDPOINT", raising=False)

    with pytest.raises(ValueError, match="POLYMARKET_CLOB_HOST must be a non-empty"):
        config.load_scan_config()


def test_load_scan_config_rejects_non_http_clob_host(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "wss://clob.example")
    monkeypatch.delenv("COND_ARB_MARKET_WS_ENDPOINT", raising=False)

    with pytest.raises(ValueError, match="POLYMARKET_CLOB_HOST must use http/https scheme"):
        config.load_scan_config()


def test_load_scan_config_rejects_non_ws_market_endpoint(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "https://ws.example/ws/market")

    with pytest.raises(ValueError, match="COND_ARB_MARKET_WS_ENDPOINT must use ws/wss scheme"):
        config.load_scan_config()


def test_load_scan_config_normalizes_valid_clob_host(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example/")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")

    loaded = config.load_scan_config()

    assert loaded.clob_host == "https://clob.example"
    assert loaded.market_ws_endpoint == "wss://ws.example/ws/market"


def test_load_scan_config_reads_fast_start_settings(monkeypatch, tmp_path):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")
    monkeypatch.setenv("COND_ARB_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("COND_ARB_FAST_START_ENABLED", "false")
    monkeypatch.setenv("COND_ARB_FAST_START_EVENT_LIMIT", "7")
    monkeypatch.setenv("COND_ARB_FAST_START_TOKEN_LIMIT", "13")
    monkeypatch.setenv("COND_ARB_UNIVERSE_CACHE_MAX_AGE_SECONDS", "42")

    loaded = config.load_scan_config()

    assert loaded.fast_start_enabled is False
    assert loaded.fast_start_event_limit == 7
    assert loaded.fast_start_token_limit == 13
    assert loaded.universe_cache_max_age_seconds == 42
    assert loaded.market_universe_cache_path == tmp_path / "data" / "market_universe_cache.json"
