from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from polymarket_conditional_arb import config
from polymarket_conditional_arb import paper as paper_module
from polymarket_conditional_arb.arb_models import BinaryMarket
from polymarket_conditional_arb.order_book import asks_from_book
from polymarket_conditional_arb.paper import (
    FillTimeBookEvidence,
    PaperPortfolio,
    PaperPortfolioParams,
    evaluate_binary_paper_execution,
    initial_portfolio_state,
)

AS_OF = datetime(2026, 6, 8, 12, tzinfo=timezone.utc)


def raw_binary_market(**overrides):
    row = {
        "id": "m1",
        "conditionId": "c1",
        "question": "Will X happen?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes-token", "no-token"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
    }
    row.update(overrides)
    return row


def market(**overrides) -> BinaryMarket:
    parsed = BinaryMarket.from_gamma_market(raw_binary_market(**overrides))
    assert parsed is not None
    return parsed


def asks(token_id: str, levels):
    return asks_from_book(
        {"asks": [{"price": price, "size": size} for price, size in levels]},
        token_id=token_id,
        updated_at=AS_OF,
    )


def params(**overrides) -> PaperPortfolioParams:
    values = {
        "starting_capital_usd": 1000.0,
        "trade_ceiling_usd": 100.0,
        "slippage_buffer_bps": 0.0,
        "taker_fee_bps": 0.0,
        "tax_bps": 0.0,
        "merge_cost_usd": 0.0,
        "min_net_profit_usd": 0.0,
        "min_net_return_bps": 0.0,
        "max_book_age_seconds": 20.0,
        "simulation": config.PaperExecutionSimulationConfig.zero_friction(),
    }
    values.update(overrides)
    return PaperPortfolioParams(**values)


def state_for(p: PaperPortfolioParams | None = None, *, cash: float | None = None):
    p = p or params()
    state = initial_portfolio_state(p, as_of=AS_OF)
    if cash is not None:
        state["cash"] = cash
    return state


def completed_active_execution_state(p: PaperPortfolioParams) -> dict:
    state = state_for(p)
    state["cash"] = 1000.3
    state["realized_pnl"] = 0.3
    state["total_equity"] = 1000.3
    state["active_execution"] = {
        "execution_id": "paper:m1:1:deadbeef",
        "market_id": "m1",
        "condition_id": "c1",
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "book_fingerprint": "deadbeef",
        "target_quantity": 10.0,
        "target_yes_filled_quantity": 10.0,
        "target_no_filled_quantity": 10.0,
        "completed_quantity": 10.0,
        "completed_yes_filled_quantity": 10.0,
        "completed_no_filled_quantity": 10.0,
        "current_step_quantity": 5.0,
        "step_plan": {
            "step_quantity_shares": 5.0,
            "max_step_count": 4,
            "grow_step_size_after_success": False,
            "merge_cost_per_step": False,
        },
        "planned_execution": {
            "execution_id": "paper:m1:1:deadbeef",
            "market_id": "m1",
            "condition_id": "c1",
            "event_id": "e1",
            "event_title": "Event",
            "question": "Will X happen?",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "executed_at_utc": "2026-06-08T12:00:00Z",
            "book_fingerprint": "deadbeef",
            "quantity": 10.0,
            "quantity_redeemed": 10.0,
            "yes_filled_quantity": 10.0,
            "no_filled_quantity": 10.0,
            "yes_vwap": 0.48,
            "no_vwap": 0.49,
            "yes_cost": 4.8,
            "no_cost": 4.9,
            "gross_cost": 9.7,
            "estimated_fees": 0.0,
            "slippage_buffer": 0.0,
            "tax_cost": 0.0,
            "merge_cost": 0.0,
            "capital_used": 9.7,
            "redeemed_value": 10.0,
            "net_profit": 0.3,
            "net_return_bps": 309.27835051546396,
            "effective_slippage_bps": 0.0,
            "trade_ceiling_usd": 100.0,
            "ceiling_used_usd": 9.7,
            "stop_reason": "cash_or_ceiling_limit",
            "simulation": {"fill_timestamp_utc": "2026-06-08T12:00:00Z"},
            "details": {
                "yes_best_ask": 0.48,
                "no_best_ask": 0.49,
                "yes_source": "rest_book",
                "no_source": "rest_book",
                "min_order_size": 5.0,
                "signal_book_fingerprint": "deadbeef",
            },
        },
        "steps": [
            {
                "execution_id": "paper:m1:1:deadbeef:step:1",
                "executed_at_utc": "2026-06-08T12:00:00Z",
                "book_fingerprint": "deadbeef-step-1",
                "quantity": 5.0,
                "yes_filled_quantity": 5.0,
                "no_filled_quantity": 5.0,
                "quantity_redeemed": 5.0,
                "yes_cost": 2.4,
                "no_cost": 2.45,
                "gross_cost": 4.85,
                "estimated_fees": 0.0,
                "slippage_buffer": 0.0,
                "tax_cost": 0.0,
                "merge_cost": 0.0,
                "capital_used": 4.85,
                "redeemed_value": 5.0,
                "net_profit": 0.15,
                "cash_before": 1000.0,
                "cash_after": 1000.15,
                "redeemed_cost_basis_usd": 4.85,
                "simulation": {"step_index": 1},
            },
            {
                "execution_id": "paper:m1:1:deadbeef:step:2",
                "executed_at_utc": "2026-06-08T12:00:01Z",
                "book_fingerprint": "deadbeef-step-2",
                "quantity": 5.0,
                "yes_filled_quantity": 5.0,
                "no_filled_quantity": 5.0,
                "quantity_redeemed": 5.0,
                "yes_cost": 2.4,
                "no_cost": 2.45,
                "gross_cost": 4.85,
                "estimated_fees": 0.0,
                "slippage_buffer": 0.0,
                "tax_cost": 0.0,
                "merge_cost": 0.0,
                "capital_used": 4.85,
                "redeemed_value": 5.0,
                "net_profit": 0.15,
                "cash_before": 1000.15,
                "cash_after": 1000.3,
                "redeemed_cost_basis_usd": 4.85,
                "simulation": {"step_index": 2},
            },
        ],
    }
    return state


def simulation(**overrides):
    values = {
        "enabled": True,
        "seed": 0,
        "latency_ms": 0.0,
        "latency_jitter_ms": 0.0,
        "latency_mode": "fixed",
        "local_timeout_ms": 0.0,
        "telemetry_latency_window": 50,
        "latency_jitter_seed_scope": "market_book_stage",
        "signing_latency_ms": 0.0,
        "settlement_latency_ms": 0.0,
        "max_fill_price_move_bps": 0.0,
        "fill_eligibility_mode": "strict_public_depth",
        "allow_trade_print_fill_support": True,
        "allow_deterministic_fill_fallback": False,
        "settlement_enabled": True,
        "settlement_source": "public_metadata_or_ws",
        "unmatched_open_valuation": "best_bid_midpoint_or_zero",
        "settlement_require_winner": True,
        "slippage_mode": "fixed_plus_calibrated",
        "slippage_max_bps": 100.0,
        "slippage_lookback_events": 50,
        "slippage_combine_mode": "max",
        "queue_depth_ratio": 0.0,
        "queue_fill_probability": 0.0,
        "partial_fill_probability": 0.0,
        "partial_fill_min_ratio": 0.0,
        "submit_failure_probability": 0.0,
        "accept_failure_probability": 0.0,
        "fill_failure_probability": 0.0,
        "cancel_failure_probability": 0.0,
        "throttle_max_submissions_per_second": 0,
        "throttle_quantity_ratio": 0.0,
        "adverse_selection_probability": 0.0,
        "adverse_depth_removal_ratio": 0.0,
        "adverse_price_move_bps": 0.0,
    }
    values.update(overrides)
    return config.PaperExecutionSimulationConfig(**values)


def test_paired_execution_consumes_multiple_levels_until_edge_disappears():
    p = params()
    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.45, 10), (0.48, 10), (0.55, 10)]),
        asks("no-token", [(0.50, 30)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution is not None
    assert decision.execution["quantity_redeemed"] == pytest.approx(20.0)
    assert decision.execution["gross_cost"] == pytest.approx((10 * 0.45) + (10 * 0.48) + (20 * 0.50))
    assert decision.execution["stop_reason"] == "edge_disappeared"


def test_paired_execution_respects_trade_ceiling_and_cash():
    p = params(trade_ceiling_usd=10.0)
    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution is not None
    assert decision.execution["capital_used"] == pytest.approx(10.0)
    assert decision.execution["quantity"] == pytest.approx(10.0 / 0.97)
    assert decision.execution["stop_reason"] == "cash_or_ceiling_limit"

    cash_limited = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        state=state_for(p, cash=5.0),
        params=p,
        as_of=AS_OF,
    )
    assert cash_limited.action == "EXECUTE"
    assert cash_limited.execution is not None
    assert cash_limited.execution["capital_used"] == pytest.approx(5.0)


def test_trade_ceiling_clamps_deep_profitable_book_instead_of_skipping():
    p = params(trade_ceiling_usd=100.0)
    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 210)]),
        asks("no-token", [(0.49, 210)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution is not None
    assert decision.execution["capital_used"] == pytest.approx(100.0)
    assert decision.execution["quantity"] == pytest.approx(100.0 / 0.97)
    assert decision.execution["quantity"] >= market().effective_min_order_size
    assert decision.execution["stop_reason"] == "cash_or_ceiling_limit"


def test_paper_execution_rejects_profitable_depth_below_polymarket_api_minimum():
    p = params()
    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 4.9)]),
        asks("no-token", [(0.49, 4.9)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "insufficient_depth"
    assert decision.details["available_equal_depth"] == pytest.approx(4.9)
    assert decision.details["min_quantity"] == 5.0


def test_costs_are_applied_before_profitability_decision():
    costly = params(slippage_buffer_bps=200.0)
    edge_lost = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.495, 10)]),
        asks("no-token", [(0.495, 10)]),
        state=state_for(costly),
        params=costly,
        as_of=AS_OF,
    )

    assert edge_lost.action == "SKIP"
    assert edge_lost.reason == "edge_disappeared"

    fixed_cost = params(merge_cost_usd=1.0)
    too_small = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(fixed_cost),
        params=fixed_cost,
        as_of=AS_OF,
    )

    assert too_small.action == "SKIP"
    assert too_small.reason == "not_profitable"


def test_no_fill_when_one_side_has_no_liquidity():
    p = params()
    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", []),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "missing_two_sided_ask_liquidity"


def test_portfolio_redeems_only_completed_pairs(tmp_path):
    p = params()
    portfolio = PaperPortfolio(tmp_path / "state.json", events_path=tmp_path / "events.jsonl", params=p).load()
    portfolio.state["inventory"] = {
        "yes-token": {
            "token_id": "yes-token",
            "market_id": "m1",
            "condition_id": "c1",
            "outcome": "YES",
            "quantity": 2.0,
        }
    }

    assert portfolio._redeem_completed_pairs(  # noqa: SLF001
        market_id="m1",
        yes_token_id="yes-token",
        no_token_id="no-token",
    ) == 0.0

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert portfolio.state["inventory"]["yes-token"]["quantity"] == pytest.approx(2.0)
    assert "no-token" not in portfolio.state["inventory"]


def test_portfolio_initializes_and_persists_state(tmp_path):
    p = params()
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()

    assert portfolio.state["cash"] == 1000.0
    assert not path.exists()

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["cash"] == pytest.approx(1000.3)
    assert saved["realized_pnl"] == pytest.approx(0.3)
    assert len(saved["executions"]) == 1
    assert saved["costs"]["fees_usd"] == 0.0


def test_same_executable_book_with_different_local_timestamps_does_not_duplicate(tmp_path):
    p = params()
    portfolio = PaperPortfolio(tmp_path / "portfolio.json", events_path=tmp_path / "events.jsonl", params=p).load()

    first = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )
    second = portfolio.execute_binary_complete_set(
        market(),
        asks_from_book(
            {"asks": [{"price": "0.48", "size": "10"}]},
            token_id="yes-token",
            updated_at=AS_OF + timedelta(seconds=5),
        ),
        asks_from_book(
            {"asks": [{"price": "0.49", "size": "10"}]},
            token_id="no-token",
            updated_at=AS_OF + timedelta(seconds=5),
        ),
        as_of=AS_OF + timedelta(seconds=5),
    )

    assert first.action == "EXECUTE"
    assert second.action == "SKIP"
    assert second.reason == "unchanged_book_snapshot"
    assert len(portfolio.state["executions"]) == 1


def test_zero_friction_simulation_preserves_legacy_fill_and_fingerprint(tmp_path):
    legacy = params()
    simulated = params(simulation=config.PaperExecutionSimulationConfig.zero_friction())
    legacy_portfolio = PaperPortfolio(tmp_path / "legacy.json", events_path=tmp_path / "legacy.jsonl", params=legacy).load()
    simulated_portfolio = PaperPortfolio(
        tmp_path / "simulated.json",
        events_path=tmp_path / "simulated.jsonl",
        params=simulated,
    ).load()

    legacy_decision = legacy_portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )
    simulated_decision = simulated_portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    assert legacy_decision.action == "EXECUTE"
    assert simulated_decision.action == "EXECUTE"
    for field in ("quantity", "quantity_redeemed", "gross_cost", "capital_used", "net_profit"):
        assert simulated_decision.execution[field] == pytest.approx(legacy_decision.execution[field])
    assert simulated_decision.execution["book_fingerprint"] == legacy_decision.execution["book_fingerprint"]
    assert "simulation" not in simulated_decision.execution
    assert simulated_portfolio.state["cash"] == pytest.approx(legacy_portfolio.state["cash"])


def test_simulated_latency_makes_stale_fill_time_books_skip():
    p = params(
        max_book_age_seconds=1.0,
        simulation=simulation(latency_ms=1500.0, allow_deterministic_fill_fallback=True),
    )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_stale_fill_time_book"
    assert decision.details["simulation_failure"] is True
    assert decision.details["simulation"]["fill_latency_ms"] == pytest.approx(1500.0)


def test_latency_telemetry_uses_recent_request_samples():
    p = params(simulation=simulation(latency_mode="telemetry", allow_deterministic_fill_fallback=True))
    evidence = FillTimeBookEvidence(
        source="rest_snapshot",
        yes_book=asks("yes-token", [(0.48, 10)]),
        no_book=asks("no-token", [(0.49, 10)]),
        request_records=(
            {"latency_seconds": 0.04},
            {"latency_seconds": 0.08},
            {"latency_seconds": 0.12},
        ),
    )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
        fill_time_book_reader=lambda _market, _fill_time: evidence,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution["simulation"]["telemetry"]["sample_count"] == 3
    assert decision.execution["simulation"]["telemetry"]["p95_latency_ms"] == pytest.approx(120.0)


def test_local_timeout_skips_slow_fill_time_request():
    p = params(simulation=simulation(local_timeout_ms=50.0))

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
        fill_time_book_reader=lambda _market, _fill_time: FillTimeBookEvidence(
            source="rest_snapshot",
            yes_book=asks("yes-token", [(0.48, 10)]),
            no_book=asks("no-token", [(0.49, 10)]),
            request_records=({"latency_seconds": 0.08},),
        ),
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_local_timeout"


def test_moved_fill_time_books_skip_when_price_move_exceeds_limit():
    p = params(
        simulation=simulation(max_fill_price_move_bps=10.0, allow_deterministic_fill_fallback=True),
    )

    def fill_reader(_market, _fill_time):
        return FillTimeBookEvidence(
            source="ws_cache",
            yes_book=asks("yes-token", [(0.50, 10)]),
            no_book=asks("no-token", [(0.49, 10)]),
            snapshot_ready={"yes-token": True, "no-token": True},
        )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
        fill_time_book_reader=fill_reader,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_fill_price_moved"
    assert decision.details["simulation_failure"] is True
    assert decision.details["fill_unit_cost"] > decision.details["signal_unit_cost"]
    assert decision.details["simulation"]["live_public_data"]["fill_time"]["source"] == "ws_cache"


def test_queue_depth_ratio_reduces_available_fill_size():
    p = params(
        simulation=simulation(
            queue_depth_ratio=0.5,
            queue_fill_probability=1.0,
            allow_deterministic_fill_fallback=True,
        )
    )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 20)]),
        asks("no-token", [(0.49, 20)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution["quantity"] == pytest.approx(10.0)
    assert decision.execution["simulation"]["queue"]["depth_ratio"] == 0.5
    assert decision.execution["simulation"]["fallback"]["queue"]["source"] == "deterministic_depth_fallback"


def test_public_queue_evidence_drives_partial_one_sided_fill(tmp_path):
    p = params(
        trade_ceiling_usd=100.0,
        simulation=simulation(
            latency_ms=1.0,
            submit_failure_probability=0.0,
            accept_failure_probability=0.0,
            fill_failure_probability=0.0,
            cancel_failure_probability=0.0,
        ),
    )
    portfolio = PaperPortfolio(tmp_path / "portfolio.json", events_path=tmp_path / "events.jsonl", params=p).load()

    def fill_reader(_market, _fill_time):
        return FillTimeBookEvidence(
            source="ws_cache",
            yes_book=asks("yes-token", [(0.48, 100)]),
            no_book=asks("no-token", [(0.49, 100)]),
            snapshot_ready={"yes-token": True, "no-token": True},
            public_price_changes={
                "yes-token": (
                    {
                        "side": "ask",
                        "price": 0.48,
                        "old_size": 100.0,
                        "new_size": 90.0,
                        "delta_size": -10.0,
                    },
                ),
                "no-token": (
                    {
                        "side": "ask",
                        "price": 0.49,
                        "old_size": 100.0,
                        "new_size": 96.0,
                        "delta_size": -4.0,
                    },
                ),
            },
            public_trade_prints={
                "no-token": (
                    {
                        "price": 0.49,
                        "size": 2.0,
                        "side": "BUY",
                    },
                )
            },
        )

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        as_of=AS_OF,
        fill_time_book_reader=fill_reader,
    )

    assert decision.action == "EXECUTE"
    execution = decision.execution
    assert execution["yes_filled_quantity"] == pytest.approx(10.0)
    assert execution["no_filled_quantity"] == pytest.approx(6.0)
    assert execution["quantity_redeemed"] == pytest.approx(6.0)
    assert execution["unmatched_yes_quantity"] == pytest.approx(4.0)
    assert execution["simulation"]["queue"]["public_queue_evidence"]["source"] == "public_trade_delta_evidence"
    assert execution["simulation"]["partial_fill"]["source"] == "public_queue_evidence"
    assert portfolio.status()["unmatched_inventory"]


def test_deterministic_fallback_can_fill_without_public_evidence_when_enabled():
    p = params(
        simulation=simulation(
            allow_deterministic_fill_fallback=True,
            queue_depth_ratio=0.5,
            queue_fill_probability=1.0,
            partial_fill_probability=1.0,
            partial_fill_min_ratio=0.5,
        )
    )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 20)]),
        asks("no-token", [(0.49, 20)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution["simulation"]["fallback"]["queue"]["source"] == "deterministic_depth_fallback"


def test_partial_fill_redeems_matched_quantity_and_leaves_unmatched_inventory(tmp_path):
    p = params(
        trade_ceiling_usd=100.0,
        simulation=simulation(
            partial_fill_probability=1.0,
            partial_fill_min_ratio=0.5,
            allow_deterministic_fill_fallback=True,
        ),
    )
    portfolio = PaperPortfolio(tmp_path / "portfolio.json", events_path=tmp_path / "events.jsonl", params=p).load()

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    execution = decision.execution
    assert execution["yes_filled_quantity"] <= execution["quantity"]
    assert execution["no_filled_quantity"] <= execution["quantity"]
    assert execution["quantity_redeemed"] == pytest.approx(
        min(execution["yes_filled_quantity"], execution["no_filled_quantity"])
    )
    unmatched_total = execution["unmatched_yes_quantity"] + execution["unmatched_no_quantity"]
    assert unmatched_total > 0.0
    status = portfolio.status()
    assert status["unmatched_inventory"]


def test_strict_mode_skips_when_only_signal_book_is_available():
    p = params(simulation=simulation())

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_no_fill_time_public_source"


def test_queue_degraded_pair_below_minimum_does_not_create_execution():
    p = params(
        simulation=simulation(
            queue_depth_ratio=0.4,
            queue_fill_probability=1.0,
            allow_deterministic_fill_fallback=True,
        )
    )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_queue_min_size"
    assert decision.details["available_equal_depth"] == pytest.approx(4.0)


def test_seeded_simulated_submit_failure_is_reproducible_and_marked_fallback():
    p = params(
        simulation=simulation(
            seed=123,
            submit_failure_probability=1.0,
            allow_deterministic_fill_fallback=True,
        )
    )

    first = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )
    second = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert first.action == "SKIP"
    assert second.action == "SKIP"
    assert first.reason == second.reason == "simulation_submit_failure"
    assert first.details["simulation"] == second.details["simulation"]
    assert (
        first.details["simulation"]["fallback"]["legacy_failure_probability"]["source"]
        == "deterministic_legacy_probability_fallback"
    )


def test_throttle_saturation_reduces_quantity_and_skips_below_minimum():
    p = params(
        simulation=simulation(
            throttle_max_submissions_per_second=1,
            throttle_quantity_ratio=0.4,
            allow_deterministic_fill_fallback=True,
        )
    )

    decision = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        state=state_for(p),
        params=p,
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_local_pressure_min_size"
    assert decision.details["degraded_quantity"] == pytest.approx(4.0)


def test_reconcile_public_markets_settles_closed_market_inventory(tmp_path):
    p = params(simulation=simulation(settlement_enabled=True))
    portfolio = PaperPortfolio(tmp_path / "state.json", events_path=tmp_path / "events.jsonl", params=p).load()
    portfolio.state["inventory"] = {
        "yes-token": {
            "token_id": "yes-token",
            "market_id": "m1",
            "condition_id": "c1",
            "outcome": "YES",
            "quantity": 2.0,
            "cost_basis_usd": 0.8,
            "pending_settlement": True,
        },
        "no-token": {
            "token_id": "no-token",
            "market_id": "m1",
            "condition_id": "c1",
            "outcome": "NO",
            "quantity": 2.0,
            "cost_basis_usd": 0.9,
            "pending_settlement": True,
        },
    }
    base_market = market()
    resolved_market = BinaryMarket(
        market_id=base_market.market_id,
        condition_id=base_market.condition_id,
        question=base_market.question,
        yes_token_id=base_market.yes_token_id,
        no_token_id=base_market.no_token_id,
        active=False,
        closed=True,
        accepting_orders=False,
        enable_order_book=base_market.enable_order_book,
        neg_risk=base_market.neg_risk,
        tick_size=base_market.tick_size,
        min_order_size=base_market.min_order_size,
        metadata={**base_market.metadata, "winner_token_id": "yes-token", "winner_outcome": "YES"},
    )

    summary = portfolio.reconcile_public_markets(
        markets_by_id={"m1": resolved_market},
        resolution_events_by_market={"m1": ({"winning_asset_id": "yes-token", "winning_outcome": "YES"},)},
        valuation_snapshots_by_token={
            "yes-token": {"recent_best_bid_asks": [{"best_bid": 0.45, "best_ask": 0.47}]},
            "no-token": {"recent_best_bid_asks": [{"best_bid": 0.03, "best_ask": 0.05}]},
        },
        as_of=AS_OF,
    )

    assert summary["settlements_applied"] == 2
    assert portfolio.state["cash"] == pytest.approx(1002.0)
    assert portfolio.state["inventory"] == {}
    assert portfolio.state["metadata"]["pending_settlement_count"] == 0
    assert portfolio.status()["settlements_applied_count"] == 2


def test_reconcile_public_markets_is_idempotent_for_duplicate_settlement(tmp_path):
    p = params(simulation=simulation(settlement_enabled=True))
    portfolio = PaperPortfolio(tmp_path / "state.json", events_path=tmp_path / "events.jsonl", params=p).load()
    portfolio.state["inventory"] = {
        "yes-token": {
            "token_id": "yes-token",
            "market_id": "m1",
            "condition_id": "c1",
            "outcome": "YES",
            "quantity": 1.0,
            "cost_basis_usd": 0.4,
        }
    }
    base_market = market()
    resolved_market = BinaryMarket(
        market_id=base_market.market_id,
        condition_id=base_market.condition_id,
        question=base_market.question,
        yes_token_id=base_market.yes_token_id,
        no_token_id=base_market.no_token_id,
        active=False,
        closed=True,
        accepting_orders=False,
        enable_order_book=base_market.enable_order_book,
        neg_risk=base_market.neg_risk,
        tick_size=base_market.tick_size,
        min_order_size=base_market.min_order_size,
        metadata={**base_market.metadata, "winner_token_id": "yes-token", "winner_outcome": "YES"},
    )

    first = portfolio.reconcile_public_markets(markets_by_id={"m1": resolved_market}, as_of=AS_OF)
    cash_after_first = portfolio.state["cash"]
    second = portfolio.reconcile_public_markets(markets_by_id={"m1": resolved_market}, as_of=AS_OF)

    assert first["settlements_applied"] == 1
    assert second["settlements_applied"] == 0
    assert portfolio.state["cash"] == pytest.approx(cash_after_first)


def test_simulated_execution_failure_writes_audit_event_without_mutating_state(tmp_path):
    p = params(
        simulation=simulation(
            submit_failure_probability=1.0,
            allow_deterministic_fill_fallback=True,
        )
    )
    path = tmp_path / "portfolio.json"
    events_path = tmp_path / "events.jsonl"
    portfolio = PaperPortfolio(path, events_path=events_path, params=p).load()

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_submit_failure"
    assert not path.exists()
    assert portfolio.state["executions"] == []
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event_type"] == "paper_portfolio_execution_failed"
    assert events[0]["reason"] == "simulation_submit_failure"
    assert events[0]["failure_stage"] == "simulation_submit_failure"
    assert events[0]["simulation"]["failure_reason"] == "simulation_submit_failure"
    assert events[0]["simulation"]["live_public_data"]
    assert events[0]["simulation"]["inferred"]
    assert events[0]["simulation"]["fallback"]


def test_public_data_error_writes_failure_event_without_mutating_state(tmp_path):
    p = params(
        simulation=simulation(
            latency_ms=1.0,
            submit_failure_probability=0.0,
            accept_failure_probability=0.0,
            fill_failure_probability=0.0,
            cancel_failure_probability=0.0,
        )
    )
    path = tmp_path / "portfolio.json"
    events_path = tmp_path / "events.jsonl"
    portfolio = PaperPortfolio(path, events_path=events_path, params=p).load()

    def fill_reader(_market, _fill_time):
        return FillTimeBookEvidence(
            source="error",
            public_error="Timeout: public CLOB request timed out",
        )

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
        fill_time_book_reader=fill_reader,
    )

    assert decision.action == "SKIP"
    assert decision.reason == "simulation_public_data_error"
    assert not path.exists()
    assert portfolio.state["executions"] == []
    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert events[0]["event_type"] == "paper_portfolio_execution_failed"
    assert events[0]["reason"] == "simulation_public_data_error"
    assert events[0]["simulation"]["live_public_data"]["fill_time"]["public_error"].startswith("Timeout:")


def test_capped_deep_book_fingerprint_prevents_duplicate_execution(tmp_path):
    p = params(trade_ceiling_usd=100.0)
    portfolio = PaperPortfolio(tmp_path / "portfolio.json", events_path=tmp_path / "events.jsonl", params=p).load()

    first = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 210)]),
        asks("no-token", [(0.49, 210)]),
        as_of=AS_OF,
    )
    second = portfolio.execute_binary_complete_set(
        market(),
        asks_from_book(
            {"asks": [{"price": "0.48", "size": "210"}]},
            token_id="yes-token",
            updated_at=AS_OF + timedelta(seconds=5),
        ),
        asks_from_book(
            {"asks": [{"price": "0.49", "size": "210"}]},
            token_id="no-token",
            updated_at=AS_OF + timedelta(seconds=5),
        ),
        as_of=AS_OF + timedelta(seconds=5),
    )

    assert first.action == "EXECUTE"
    assert first.execution["capital_used"] == pytest.approx(100.0)
    assert second.action == "SKIP"
    assert second.reason == "unchanged_book_snapshot"
    assert len(portfolio.state["executions"]) == 1


def test_stepped_execution_persists_active_state_then_finalizes(tmp_path):
    p = params(
        simulation=simulation(
            queue_depth_ratio=0.5,
            queue_fill_probability=1.0,
            partial_fill_probability=1.0,
            partial_fill_min_ratio=1.0,
            allow_deterministic_fill_fallback=True,
            max_step_count=4,
            step_quantity_shares=5.0,
            merge_cost_per_step=False,
        )
    )
    portfolio = PaperPortfolio(tmp_path / "portfolio.json", events_path=tmp_path / "events.jsonl", params=p).load()

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 20)]),
        asks("no-token", [(0.49, 20)]),
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution is not None
    assert decision.execution["quantity"] == pytest.approx(10.0)
    assert decision.execution["quantity_redeemed"] == pytest.approx(10.0)
    assert decision.execution["details"]["stepped_execution"]["step_count"] == 2
    assert "active_execution" not in portfolio.state
    assert len(portfolio.state["executions"]) == 1
    assert portfolio.state["executions"][0]["details"]["stepped_execution"]["step_count"] == 2
    saved = json.loads((tmp_path / "portfolio.json").read_text(encoding="utf-8"))
    assert "active_execution" not in saved
    assert saved["executions"][0]["details"]["stepped_execution"]["step_count"] == 2


def test_resumed_active_execution_finishes_and_clears_state(tmp_path):
    p = params(
        simulation=simulation(
            queue_depth_ratio=0.5,
            queue_fill_probability=1.0,
            partial_fill_probability=1.0,
            partial_fill_min_ratio=1.0,
            allow_deterministic_fill_fallback=True,
            max_step_count=4,
            step_quantity_shares=5.0,
            merge_cost_per_step=False,
        )
    )
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()
    portfolio.state["cash"] = 1000.15
    portfolio.state["realized_pnl"] = 0.15
    portfolio.state["total_equity"] = 1000.15
    portfolio.state["active_execution"] = {
        "execution_id": "paper:m1:1:deadbeef",
        "market_id": "m1",
        "condition_id": "c1",
        "yes_token_id": "yes-token",
        "no_token_id": "no-token",
        "book_fingerprint": "deadbeef",
        "target_quantity": 10.0,
        "target_yes_filled_quantity": 10.0,
        "target_no_filled_quantity": 10.0,
        "completed_quantity": 5.0,
        "completed_yes_filled_quantity": 5.0,
        "completed_no_filled_quantity": 5.0,
        "current_step_quantity": 5.0,
        "step_plan": {
            "step_quantity_shares": 5.0,
            "max_step_count": 4,
            "grow_step_size_after_success": False,
            "merge_cost_per_step": False,
        },
        "planned_execution": {
            "execution_id": "paper:m1:1:deadbeef",
            "market_id": "m1",
            "condition_id": "c1",
            "event_id": "e1",
            "event_title": "Event",
            "question": "Will X happen?",
            "yes_token_id": "yes-token",
            "no_token_id": "no-token",
            "executed_at_utc": "2026-06-08T12:00:00Z",
            "book_fingerprint": "deadbeef",
            "quantity": 10.0,
            "quantity_redeemed": 10.0,
            "yes_filled_quantity": 10.0,
            "no_filled_quantity": 10.0,
            "yes_vwap": 0.48,
            "no_vwap": 0.49,
            "yes_cost": 4.8,
            "no_cost": 4.9,
            "gross_cost": 9.7,
            "estimated_fees": 0.0,
            "slippage_buffer": 0.0,
            "tax_cost": 0.0,
            "merge_cost": 0.0,
            "capital_used": 9.7,
            "redeemed_value": 10.0,
            "net_profit": 0.3,
            "net_return_bps": 309.27835051546396,
            "effective_slippage_bps": 0.0,
            "trade_ceiling_usd": 100.0,
            "ceiling_used_usd": 9.7,
            "stop_reason": "cash_or_ceiling_limit",
            "simulation": {
                "fill_timestamp_utc": "2026-06-08T12:00:00Z",
                "step_index": 1,
                "partial_fill": {
                    "applied": False,
                    "source": "full_public_depth",
                    "target_quantity": 10.0,
                    "yes_filled_quantity": 5.0,
                    "no_filled_quantity": 5.0,
                    "matched_quantity": 5.0,
                    "unmatched_yes_quantity": 0.0,
                    "unmatched_no_quantity": 0.0,
                },
            },
            "details": {
                "yes_best_ask": 0.48,
                "no_best_ask": 0.49,
                "yes_source": "rest_book",
                "no_source": "rest_book",
                "min_order_size": 5.0,
                "tranches": [
                    {"quantity": 5.0, "yes_price": 0.48, "no_price": 0.49, "unit_gross_cost": 0.97},
                ],
                "signal_book_fingerprint": "deadbeef",
                "stepped_execution": {"step_count": 1},
            },
        },
        "steps": [
            {
                "execution_id": "paper:m1:1:deadbeef:step:1",
                "executed_at_utc": "2026-06-08T12:00:00Z",
                "book_fingerprint": "deadbeef-step-1",
                "quantity": 5.0,
                "yes_filled_quantity": 5.0,
                "no_filled_quantity": 5.0,
                "quantity_redeemed": 5.0,
                "yes_cost": 2.4,
                "no_cost": 2.45,
                "gross_cost": 4.85,
                "estimated_fees": 0.0,
                "slippage_buffer": 0.0,
                "tax_cost": 0.0,
                "merge_cost": 0.0,
                "capital_used": 4.85,
                "redeemed_value": 5.0,
                "net_profit": 0.15,
                "cash_before": 1000.0,
                "cash_after": 1000.15,
                "simulation": {"step_index": 1},
            }
        ],
    }
    portfolio.save()
    portfolio.load()

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 20)]),
        asks("no-token", [(0.49, 20)]),
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution is not None
    assert "active_execution" not in portfolio.state
    assert len(portfolio.state["executions"]) == 1
    assert portfolio.state["cash"] == pytest.approx(1000.3)
    assert portfolio.state["executions"][-1]["details"]["stepped_execution"]["step_count"] == 2


def test_completed_active_execution_recovery_finalizes_without_reapplying_steps(tmp_path):
    p = params(
        simulation=simulation(
            max_step_count=4,
            step_quantity_shares=5.0,
            merge_cost_per_step=False,
        )
    )
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()
    portfolio.state = completed_active_execution_state(p)
    portfolio.save()
    portfolio.load()

    summary = portfolio.recover_completed_active_execution()

    assert summary is not None
    assert summary["execution_id"] == "paper:m1:1:deadbeef"
    assert summary["already_recorded"] is False
    assert "active_execution" not in portfolio.state
    assert portfolio.state["cash"] == pytest.approx(1000.3)
    assert portfolio.state["realized_pnl"] == pytest.approx(0.3)
    assert len(portfolio.state["executions"]) == 1
    assert portfolio.state["executions"][0]["quantity_redeemed"] == pytest.approx(10.0)
    assert portfolio.state["executions"][0]["net_profit"] == pytest.approx(0.3)
    assert portfolio.state["book_fingerprints"]["m1"]["execution_id"] == "paper:m1:1:deadbeef"
    events = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert "paper_portfolio_execution_recovered" in events


def test_completed_active_execution_recovery_is_idempotent(tmp_path):
    p = params(
        simulation=simulation(
            max_step_count=4,
            step_quantity_shares=5.0,
            merge_cost_per_step=False,
        )
    )
    path = tmp_path / "portfolio.json"
    state = completed_active_execution_state(p)
    recovered = deepcopy(state["active_execution"])
    final_execution = deepcopy(recovered["planned_execution"])
    final_execution.update(
        {
            "execution_id": recovered["execution_id"],
            "book_fingerprint": recovered["book_fingerprint"],
            "executed_at_utc": "2026-06-08T12:00:01Z",
            "quantity": 10.0,
            "quantity_redeemed": 10.0,
            "yes_filled_quantity": 10.0,
            "no_filled_quantity": 10.0,
            "yes_cost": 4.8,
            "no_cost": 4.9,
            "gross_cost": 9.7,
            "capital_used": 9.7,
            "redeemed_value": 10.0,
            "net_profit": 0.3,
            "cash_before": 1000.0,
            "cash_after": 1000.3,
        }
    )
    state["executions"] = [final_execution]
    state["book_fingerprints"] = {
        "m1": {
            "fingerprint": "deadbeef",
            "execution_id": "paper:m1:1:deadbeef",
            "executed_at_utc": "2026-06-08T12:00:01Z",
        }
    }
    path.write_text(json.dumps(state), encoding="utf-8")

    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()
    first = portfolio.recover_completed_active_execution()
    second = portfolio.recover_completed_active_execution()

    assert first is not None
    assert first["already_recorded"] is True
    assert second is None
    assert "active_execution" not in portfolio.state
    assert len(portfolio.state["executions"]) == 1
    assert portfolio.state["book_fingerprints"]["m1"]["execution_id"] == "paper:m1:1:deadbeef"


def test_completed_active_execution_recovery_uses_persisted_max_step_count(tmp_path):
    p = params(
        simulation=simulation(
            max_step_count=8,
            step_quantity_shares=5.0,
            merge_cost_per_step=False,
        )
    )
    path = tmp_path / "portfolio.json"
    state = completed_active_execution_state(p)
    state["active_execution"]["target_quantity"] = 15.0
    state["active_execution"]["target_yes_filled_quantity"] = 15.0
    state["active_execution"]["target_no_filled_quantity"] = 15.0
    state["active_execution"]["completed_quantity"] = 10.0
    state["active_execution"]["completed_yes_filled_quantity"] = 10.0
    state["active_execution"]["completed_no_filled_quantity"] = 10.0
    state["active_execution"]["step_plan"]["max_step_count"] = 2
    path.write_text(json.dumps(state), encoding="utf-8")

    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()
    summary = portfolio.recover_completed_active_execution()

    assert summary is not None
    assert "active_execution" not in portfolio.state
    assert portfolio.state["executions"][0]["stop_reason"] == "max_step_count"
    assert portfolio.state["executions"][0]["details"]["stepped_execution"]["max_step_count"] == 2


def test_state_file_load_ignores_leftover_tmp_file(tmp_path):
    p = params()
    path = tmp_path / "portfolio.json"
    state = initial_portfolio_state(p, as_of=AS_OF)
    state["cash"] = 876.5
    path.write_text(json.dumps(state), encoding="utf-8")
    path.with_name(path.name + ".tmp").write_text("{not-json", encoding="utf-8")

    loaded = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()

    assert loaded.state["cash"] == 876.5


def test_status_uses_state_when_event_append_fails_after_save(tmp_path):
    p = params()
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()

    def fail_append(_record):
        raise RuntimeError("event append failed")

    portfolio.events.append = fail_append
    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    status = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).status()

    assert decision.action == "EXECUTE"
    assert "event append failed" in decision.details["event_log_error"]
    assert "event append failed" in decision.execution["event_log_error"]
    assert status["trade_count"] == 1
    assert status["cash"] == pytest.approx(1000.3)


def test_state_save_retries_transient_replace_permission_error(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_module, "PAPER_STATE_WRITE_RETRY_INITIAL_SECONDS", 0.0)
    p = params()
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()
    path_type = type(path)
    original_replace = path_type.replace
    replace_calls = 0

    def flaky_replace(self, target):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise PermissionError("portfolio file is temporarily locked")
        return original_replace(self, target)

    monkeypatch.setattr(path_type, "replace", flaky_replace)

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert decision.action == "EXECUTE"
    assert replace_calls == 2
    assert len(portfolio.state["executions"]) == 1
    assert len(saved["executions"]) == 1
    assert saved["executions"][0]["execution_id"] == portfolio.state["executions"][0]["execution_id"]


def test_state_save_exhausted_replace_retries_raise_and_roll_back(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_module, "PAPER_STATE_WRITE_RETRY_ATTEMPTS", 2)
    monkeypatch.setattr(paper_module, "PAPER_STATE_WRITE_RETRY_INITIAL_SECONDS", 0.0)
    p = params()
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()
    path_type = type(path)
    replace_calls = 0

    def locked_replace(self, target):
        nonlocal replace_calls
        replace_calls += 1
        raise PermissionError("portfolio file is locked")

    monkeypatch.setattr(path_type, "replace", locked_replace)

    with pytest.raises(PermissionError, match="portfolio file is locked"):
        portfolio.execute_binary_complete_set(
            market(),
            asks("yes-token", [(0.48, 10)]),
            asks("no-token", [(0.49, 10)]),
            as_of=AS_OF,
        )

    assert replace_calls == 2
    assert not path.exists()
    assert path.with_name(path.name + ".tmp").exists()
    assert portfolio.state["cash"] == pytest.approx(1000.0)
    assert portfolio.state["executions"] == []
    assert portfolio.state["inventory"] == {}


def test_status_retries_transient_read_failure_without_mutating_state_file(tmp_path, monkeypatch):
    monkeypatch.setattr(paper_module, "PAPER_STATE_READ_RETRY_INITIAL_SECONDS", 0.0)
    p = params()
    path = tmp_path / "portfolio.json"
    PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).reset(yes=True)
    before = path.read_text(encoding="utf-8")
    path_type = type(path)
    original_open = path_type.open
    open_calls = 0

    def flaky_open(self, *args, **kwargs):
        nonlocal open_calls
        if self == path and open_calls == 0:
            open_calls += 1
            raise PermissionError("portfolio file is briefly locked")
        open_calls += 1
        return original_open(self, *args, **kwargs)

    monkeypatch.setattr(path_type, "open", flaky_open)

    status = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).status()
    with original_open(path, encoding="utf-8") as f:
        after = f.read()

    assert open_calls == 2
    assert status["trade_count"] == 0
    assert before == after


def test_status_reports_realized_and_open_inventory_metrics(tmp_path):
    p = params()
    path = tmp_path / "portfolio.json"
    state = initial_portfolio_state(p, as_of=AS_OF)
    state["cash"] = 999.0
    state["realized_pnl"] = 0.3
    state["executions"] = [
        {
            "execution_id": "paper:m1:1",
            "market_id": "m1",
            "net_profit": 0.3,
            "quantity_redeemed": 10.0,
            "executed_at_utc": "2026-06-08T12:00:00Z",
        },
        {
            "execution_id": "paper:m2:1",
            "market_id": "m2",
            "net_profit": 0.0,
            "quantity_redeemed": 0.0,
            "executed_at_utc": "2026-06-08T12:01:00Z",
        },
        {
            "execution_id": "paper:m3:1",
            "market_id": "m3",
            "net_profit": 0.0,
            "quantity_redeemed": 0.0,
            "executed_at_utc": "2026-06-08T12:02:00Z",
        },
    ]
    state["inventory"] = {
        "m2-yes": {
            "token_id": "m2-yes",
            "market_id": "m2",
            "outcome": "YES",
            "quantity": 4.0,
            "cost_basis_usd": 1.2,
            "last_valuation_price": 0.3,
        },
        "m2-no": {
            "token_id": "m2-no",
            "market_id": "m2",
            "outcome": "NO",
            "quantity": 1.0,
            "cost_basis_usd": 0.45,
            "last_valuation_price": 0.45,
        },
        "m3-yes": {
            "token_id": "m3-yes",
            "market_id": "m3",
            "outcome": "YES",
            "quantity": 2.0,
            "cost_basis_usd": 0.8,
            "last_valuation_price": 0.5,
        },
    }
    path.write_text(json.dumps(state), encoding="utf-8")

    status = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).status()

    assert status["trade_count"] == 3
    assert status["win_rate_pct"] == pytest.approx(100.0 / 3.0)
    assert status["execution_win_rate_pct"] == pytest.approx(100.0 / 3.0)
    assert status["realized_trade_count"] == 1
    assert status["realized_win_rate_pct"] == pytest.approx(100.0)
    assert status["capital_committed_usd"] == pytest.approx(2.45)
    assert status["open_position_value_usd"] == pytest.approx(2.9)
    assert status["active_trade_count"] == 2


def test_execution_save_failure_rolls_back_in_memory_state(tmp_path, monkeypatch):
    p = params()
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).load()

    def fail_write(_state):
        raise RuntimeError("save failed")

    monkeypatch.setattr(portfolio, "_write_state", fail_write)

    with pytest.raises(RuntimeError, match="save failed"):
        portfolio.execute_binary_complete_set(
            market(),
            asks("yes-token", [(0.48, 10)]),
            asks("no-token", [(0.49, 10)]),
            as_of=AS_OF,
        )

    assert not path.exists()
    assert portfolio.state["cash"] == 1000.0
    assert portfolio.state["executions"] == []
    assert portfolio.state["inventory"] == {}


def test_preexisting_matched_inventory_is_not_new_execution_pnl(tmp_path):
    p = params()
    portfolio = PaperPortfolio(tmp_path / "state.json", events_path=tmp_path / "events.jsonl", params=p).load()
    portfolio.state["inventory"] = {
        "yes-token": {
            "token_id": "yes-token",
            "market_id": "m1",
            "condition_id": "c1",
            "outcome": "YES",
            "quantity": 2.0,
        },
        "no-token": {
            "token_id": "no-token",
            "market_id": "m1",
            "condition_id": "c1",
            "outcome": "NO",
            "quantity": 2.0,
        },
    }

    decision = portfolio.execute_binary_complete_set(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
    )

    assert decision.action == "EXECUTE"
    assert decision.execution["preexisting_redeemed_value"] == pytest.approx(2.0)
    assert decision.execution["quantity_redeemed"] == pytest.approx(10.0)
    assert decision.execution["cash_before"] == pytest.approx(1002.0)
    assert decision.execution["cash_after"] == pytest.approx(1002.3)
    assert decision.execution["net_profit"] == pytest.approx(0.3)
    assert portfolio.state["inventory"] == {}


def test_reset_requires_yes_and_status_does_not_mutate_state_file(tmp_path):
    p = params()
    path = tmp_path / "portfolio.json"
    portfolio = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p)

    with pytest.raises(ValueError):
        portfolio.reset(yes=False)

    portfolio.reset(yes=True)
    before = path.read_text(encoding="utf-8")
    status = PaperPortfolio(path, events_path=tmp_path / "events.jsonl", params=p).status()
    after = path.read_text(encoding="utf-8")

    assert status["starting_capital_usd"] == 1000.0
    assert status["trade_count"] == 0
    assert before == after
