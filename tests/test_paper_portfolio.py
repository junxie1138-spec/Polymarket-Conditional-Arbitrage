from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from polymarket_conditional_arb import config
from polymarket_conditional_arb.arb_models import BinaryMarket
from polymarket_conditional_arb.order_book import asks_from_book
from polymarket_conditional_arb.paper import (
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


def simulation(**overrides):
    values = {
        "enabled": True,
        "seed": 0,
        "latency_ms": 0.0,
        "latency_jitter_ms": 0.0,
        "signing_latency_ms": 0.0,
        "settlement_latency_ms": 0.0,
        "max_fill_price_move_bps": 0.0,
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
        simulation=simulation(latency_ms=1500.0),
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


def test_moved_fill_time_books_skip_when_price_move_exceeds_limit():
    p = params(
        simulation=simulation(max_fill_price_move_bps=10.0),
    )

    def fill_reader(_market, _fill_time):
        return asks("yes-token", [(0.50, 10)]), asks("no-token", [(0.49, 10)])

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


def test_queue_depth_ratio_reduces_available_fill_size():
    p = params(simulation=simulation(queue_depth_ratio=0.5, queue_fill_probability=1.0))

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


def test_partial_fill_redeems_matched_quantity_and_leaves_unmatched_inventory(tmp_path):
    p = params(
        trade_ceiling_usd=100.0,
        simulation=simulation(partial_fill_probability=1.0, partial_fill_min_ratio=0.5),
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


def test_queue_degraded_pair_below_minimum_does_not_create_execution():
    p = params(simulation=simulation(queue_depth_ratio=0.4, queue_fill_probability=1.0))

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


def test_seeded_simulated_submit_failure_is_reproducible():
    p = params(simulation=simulation(seed=123, submit_failure_probability=1.0))

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


def test_throttle_saturation_reduces_quantity_and_skips_below_minimum():
    p = params(
        simulation=simulation(
            throttle_max_submissions_per_second=1,
            throttle_quantity_ratio=0.4,
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
    assert decision.reason == "simulation_throttle_min_size"
    assert decision.details["degraded_quantity"] == pytest.approx(4.0)


def test_simulated_execution_failure_writes_audit_event_without_mutating_state(tmp_path):
    p = params(simulation=simulation(submit_failure_probability=1.0))
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
