from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from weather_arb_live.arb_models import BinaryMarket
from weather_arb_live.arb_strategy import ArbStrategyParams, evaluate_binary_merge_arbitrage
from weather_arb_live.order_book import asks_from_book


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
    }
    row.update(overrides)
    return row


def market(**overrides) -> BinaryMarket:
    parsed = BinaryMarket.from_gamma_market(raw_binary_market(**overrides))
    assert parsed is not None
    return parsed


def asks(token_id: str, levels, *, updated_at=AS_OF):
    return asks_from_book(
        {"asks": [{"price": price, "size": size} for price, size in levels]},
        token_id=token_id,
        updated_at=updated_at,
    )


def params(**overrides) -> ArbStrategyParams:
    values = {
        "min_net_profit_usd": 0.25,
        "min_net_return_bps": 25.0,
        "max_paper_position_usd": 50.0,
        "slippage_buffer_bps": 10.0,
        "gas_cost_usd": 0.02,
        "taker_fee_bps": 0.0,
        "max_book_age_seconds": 20.0,
    }
    values.update(overrides)
    return ArbStrategyParams(**values)


def test_binary_market_requires_yes_no_outcome_mapping():
    assert BinaryMarket.from_gamma_market(raw_binary_market(outcomes='["Up", "Down"]')) is None


def test_profitable_market_enters_using_executable_equal_depth():
    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.action == "ENTER"
    assert decision.opportunity is not None
    assert decision.opportunity.executable_size == 50.0
    assert decision.opportunity.yes_vwap == 0.48
    assert decision.opportunity.no_vwap == 0.49
    assert decision.opportunity.net_profit == pytest.approx(50 - 48.5 - 0.0485 - 0.02)


def test_uses_largest_still_profitable_equal_depth_not_midpoint():
    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.45, 10), (0.48, 100)]),
        asks("no-token", [(0.50, 20)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.action == "ENTER"
    assert decision.opportunity is not None
    assert decision.opportunity.executable_size == 20.0
    assert decision.opportunity.gross_cost == pytest.approx((10 * 0.45) + (10 * 0.48) + (20 * 0.50))


def test_unprofitable_market_is_rejected():
    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.51, 100)]),
        asks("no-token", [(0.50, 100)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.action == "SKIP"
    assert decision.reason == "not_profitable"


def test_tiny_profit_below_safety_threshold_is_rejected():
    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.499, 50)]),
        asks("no-token", [(0.499, 50)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "not_profitable"
    assert decision.details["net_profit"] < 0.25


def test_missing_ask_liquidity_is_rejected():
    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", []),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "missing_two_sided_ask_liquidity"


def test_insufficient_depth_for_market_min_order_size_is_rejected():
    decision = evaluate_binary_merge_arbitrage(
        market(orderMinSize="10"),
        asks("yes-token", [(0.48, 5)]),
        asks("no-token", [(0.49, 5)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "insufficient_depth"


def test_stale_book_is_rejected():
    stale_at = AS_OF - timedelta(seconds=21)

    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.48, 100)], updated_at=stale_at),
        asks("no-token", [(0.49, 100)], updated_at=AS_OF),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "stale_book"
    assert decision.details["side"] == "yes"


def test_future_dated_book_is_rejected():
    future_at = AS_OF + timedelta(seconds=1)

    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.48, 100)], updated_at=future_at),
        asks("no-token", [(0.49, 100)], updated_at=AS_OF),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "stale_book"
    assert decision.details["age_seconds"] < 0


def test_non_accepting_market_is_rejected():
    decision = evaluate_binary_merge_arbitrage(
        market(acceptingOrders=False, enableOrderBook=False),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "not_accepting_orders"


def test_duplicate_position_guard_checks_market_and_condition_ids():
    decision = evaluate_binary_merge_arbitrage(
        market(),
        asks("yes-token", [(0.48, 100)]),
        asks("no-token", [(0.49, 100)]),
        as_of=AS_OF,
        entered_positions={"other-key": {"condition_id": "c1"}},
        params=params(),
    )

    assert decision.reason == "already_entered"
