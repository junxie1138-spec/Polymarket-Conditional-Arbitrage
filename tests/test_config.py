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
