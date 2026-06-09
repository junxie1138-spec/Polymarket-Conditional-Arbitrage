from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

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
    }
    values.update(overrides)
    return PaperPortfolioParams(**values)


def state_for(p: PaperPortfolioParams | None = None, *, cash: float | None = None):
    p = p or params()
    state = initial_portfolio_state(p, as_of=AS_OF)
    if cash is not None:
        state["cash"] = cash
    return state


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

    fixed_cost = params(merge_cost_usd=0.05)
    too_small = evaluate_binary_paper_execution(
        market(),
        asks("yes-token", [(0.48, 1)]),
        asks("no-token", [(0.49, 1)]),
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
