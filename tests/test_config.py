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


def test_load_scan_config_defaults_market_ws_message_limit_above_websockets_default(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")

    loaded = config.load_scan_config()

    assert loaded.market_ws_max_message_size_bytes == config.DEFAULT_MARKET_WS_MAX_MESSAGE_SIZE_BYTES
    assert loaded.market_ws_max_message_size_bytes > config.MIN_MARKET_WS_MAX_MESSAGE_SIZE_BYTES
    assert loaded.rest_book_seed_batch_stall_seconds == config.DEFAULT_REST_BOOK_SEED_BATCH_STALL_SECONDS


@pytest.mark.parametrize("raw_value", ["0", "-1", "4096"])
def test_load_scan_config_clamps_small_market_ws_message_limit_and_reads_stall_seconds(monkeypatch, raw_value):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")
    monkeypatch.setenv("COND_ARB_MARKET_WS_MAX_MESSAGE_SIZE_BYTES", raw_value)
    monkeypatch.setenv("COND_ARB_REST_BOOK_SEED_BATCH_STALL_SECONDS", "123")

    loaded = config.load_scan_config()

    assert loaded.market_ws_max_message_size_bytes == config.MIN_MARKET_WS_MAX_MESSAGE_SIZE_BYTES
    assert loaded.rest_book_seed_batch_stall_seconds == 123.0


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


def test_load_scan_config_reads_conservative_paper_simulation_defaults(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")

    loaded = config.load_scan_config()

    assert loaded.paper_simulation.enabled is True
    assert loaded.paper_simulation.seed == 0
    assert loaded.paper_simulation.latency_ms == 250.0
    assert loaded.paper_simulation.latency_jitter_ms == 50.0
    assert loaded.paper_simulation.latency_mode == "fixed"
    assert loaded.paper_simulation.local_timeout_ms == 0.0
    assert loaded.paper_simulation.telemetry_latency_window == 50
    assert loaded.paper_simulation.latency_jitter_seed_scope == "market_book_stage"
    assert loaded.paper_simulation.signing_latency_ms == 50.0
    assert loaded.paper_simulation.settlement_latency_ms == 1500.0
    assert loaded.paper_simulation.max_fill_price_move_bps == 25.0
    assert loaded.paper_simulation.fill_eligibility_mode == "strict_public_depth"
    assert loaded.paper_simulation.allow_trade_print_fill_support is True
    assert loaded.paper_simulation.allow_deterministic_fill_fallback is False
    assert loaded.paper_simulation.settlement_enabled is True
    assert loaded.paper_simulation.settlement_source == "public_metadata_or_ws"
    assert loaded.paper_simulation.unmatched_open_valuation == "best_bid_midpoint_or_zero"
    assert loaded.paper_simulation.settlement_require_winner is True
    assert loaded.paper_simulation.slippage_mode == "fixed_plus_calibrated"
    assert loaded.paper_simulation.slippage_max_bps == 100.0
    assert loaded.paper_simulation.slippage_lookback_events == 50
    assert loaded.paper_simulation.slippage_combine_mode == "max"
    assert loaded.paper_simulation.step_quantity_shares == 5.0
    assert loaded.paper_simulation.max_step_count == 20
    assert loaded.paper_simulation.grow_step_size_after_success is False
    assert loaded.paper_simulation.merge_cost_per_step is True
    assert loaded.paper_simulation.queue_depth_ratio == 0.75
    assert loaded.paper_simulation.queue_fill_probability == 0.95
    assert loaded.paper_simulation.partial_fill_probability == 0.15
    assert loaded.paper_simulation.partial_fill_min_ratio == 0.50
    assert loaded.paper_simulation.submit_failure_probability == 0.005
    assert loaded.paper_simulation.accept_failure_probability == 0.0025
    assert loaded.paper_simulation.fill_failure_probability == 0.01
    assert loaded.paper_simulation.cancel_failure_probability == 0.0025
    assert loaded.paper_simulation.throttle_max_submissions_per_second == 8
    assert loaded.paper_simulation.throttle_quantity_ratio == 0.50
    assert loaded.paper_simulation.adverse_selection_probability == 0.25
    assert loaded.paper_simulation.adverse_depth_removal_ratio == 0.50
    assert loaded.paper_simulation.adverse_price_move_bps == 10.0
    assert loaded.paper_simulation.is_zero_friction is False


def test_load_scan_config_reads_zero_friction_paper_simulation(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")
    monkeypatch.setenv("COND_ARB_PAPER_SIMULATION_ENABLED", "true")
    monkeypatch.setenv("COND_ARB_PAPER_SIM_SEED", "42")
    for name in (
        "COND_ARB_PAPER_LATENCY_MS",
        "COND_ARB_PAPER_LATENCY_JITTER_MS",
        "COND_ARB_PAPER_SIGNING_LATENCY_MS",
        "COND_ARB_PAPER_SETTLEMENT_LATENCY_MS",
        "COND_ARB_PAPER_MAX_FILL_PRICE_MOVE_BPS",
        "COND_ARB_PAPER_QUEUE_DEPTH_RATIO",
        "COND_ARB_PAPER_QUEUE_FILL_PROBABILITY",
        "COND_ARB_PAPER_PARTIAL_FILL_PROBABILITY",
        "COND_ARB_PAPER_PARTIAL_FILL_MIN_RATIO",
        "COND_ARB_PAPER_SUBMIT_FAILURE_PROBABILITY",
        "COND_ARB_PAPER_ACCEPT_FAILURE_PROBABILITY",
        "COND_ARB_PAPER_FILL_FAILURE_PROBABILITY",
        "COND_ARB_PAPER_CANCEL_FAILURE_PROBABILITY",
        "COND_ARB_PAPER_THROTTLE_QUANTITY_RATIO",
        "COND_ARB_PAPER_ADVERSE_SELECTION_PROBABILITY",
        "COND_ARB_PAPER_ADVERSE_DEPTH_REMOVAL_RATIO",
        "COND_ARB_PAPER_ADVERSE_PRICE_MOVE_BPS",
        "COND_ARB_PAPER_LOCAL_TIMEOUT_MS",
        "COND_ARB_PAPER_SLIPPAGE_MAX_BPS",
    ):
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("COND_ARB_PAPER_THROTTLE_MAX_SUBMISSIONS_PER_SECOND", "0")
    monkeypatch.setenv("COND_ARB_PAPER_SLIPPAGE_MODE", "fixed_only")

    loaded = config.load_scan_config()

    assert loaded.paper_simulation.enabled is True
    assert loaded.paper_simulation.seed == 42
    assert loaded.paper_simulation.is_zero_friction is True


def test_load_scan_config_reads_step_simulation_overrides(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")
    monkeypatch.setenv("COND_ARB_PAPER_STEP_QUANTITY_SHARES", "7.5")
    monkeypatch.setenv("COND_ARB_PAPER_MAX_STEP_COUNT", "11")
    monkeypatch.setenv("COND_ARB_PAPER_GROW_STEP_SIZE_AFTER_SUCCESS", "true")
    monkeypatch.setenv("COND_ARB_PAPER_MERGE_COST_PER_STEP", "false")

    loaded = config.load_scan_config()

    assert loaded.paper_simulation.step_quantity_shares == 7.5
    assert loaded.paper_simulation.max_step_count == 11
    assert loaded.paper_simulation.grow_step_size_after_success is True
    assert loaded.paper_simulation.merge_cost_per_step is False


def test_load_scan_config_rejects_invalid_paper_simulation_probability(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")
    monkeypatch.setenv("COND_ARB_PAPER_FILL_FAILURE_PROBABILITY", "1.5")

    with pytest.raises(ValueError, match="COND_ARB_PAPER_FILL_FAILURE_PROBABILITY must be between 0 and 1"):
        config.load_scan_config()


def test_load_scan_config_rejects_invalid_paper_simulation_choice(monkeypatch):
    monkeypatch.setenv("POLYMARKET_CLOB_HOST", "https://clob.example")
    monkeypatch.setenv("COND_ARB_MARKET_WS_ENDPOINT", "wss://ws.example/ws/market")
    monkeypatch.setenv("COND_ARB_PAPER_LATENCY_MODE", "private_gateway")

    with pytest.raises(ValueError, match="COND_ARB_PAPER_LATENCY_MODE must be one of"):
        config.load_scan_config()
