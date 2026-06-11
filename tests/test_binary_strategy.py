from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from polymarket_conditional_arb.arb_models import BinaryMarket
from polymarket_conditional_arb.arb_strategy import ArbStrategyParams, evaluate_binary_arbitrage
from polymarket_conditional_arb.order_book import asks_from_book

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
        "max_capital_usd": 50.0,
        "slippage_buffer_bps": 10.0,
        "gas_cost_usd": 0.02,
        "taker_fee_bps": 0.0,
        "max_book_age_seconds": 20.0,
    }
    values.update(overrides)
    return ArbStrategyParams(**values)


def test_profitable_market_enters_using_equal_depth():
    decision = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.action == "ENTER"
    assert decision.opportunity is not None
    assert decision.opportunity.kind == "binary_complete_set"
    assert decision.opportunity.collateral_redeemed == 10.0
    assert decision.opportunity.gross_cost == pytest.approx(9.7)
    assert decision.opportunity.net_profit == pytest.approx(10 - 9.7 - 0.0097 - 0.02)


def test_uses_largest_profitable_depth_under_cap():
    decision = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.45, 10), (0.48, 100)]),
        asks("no-token", [(0.50, 20)]),
        as_of=AS_OF,
        params=params(max_capital_usd=30.0),
    )

    assert decision.action == "ENTER"
    assert decision.opportunity is not None
    assert decision.opportunity.collateral_redeemed == 20.0
    assert decision.opportunity.gross_cost == pytest.approx((10 * 0.45) + (10 * 0.48) + (20 * 0.50))


def test_unprofitable_market_is_rejected():
    decision = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.51, 100)]),
        asks("no-token", [(0.50, 100)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.action == "SKIP"
    assert decision.reason == "not_profitable"


def test_polymarket_minimum_order_size_rejects_sub_five_share_depth():
    decision = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.48, 4.9)]),
        asks("no-token", [(0.49, 4.9)]),
        as_of=AS_OF,
        params=params(min_net_profit_usd=0.0, min_net_return_bps=0.0),
    )

    assert decision.action == "SKIP"
    assert decision.reason == "insufficient_depth"
    assert decision.details["available_equal_depth"] == pytest.approx(4.9)
    assert decision.details["min_quantity"] == 5.0


def test_stale_book_is_rejected():
    decision = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.48, 10)], updated_at=AS_OF - timedelta(seconds=21)),
        asks("no-token", [(0.49, 10)], updated_at=AS_OF),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "stale_book"
    assert decision.details["side"] == "yes"


def test_non_tradable_market_is_rejected():
    decision = evaluate_binary_arbitrage(
        market(acceptingOrders=False, enableOrderBook=False),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "not_accepting_orders"


def test_missing_liquidity_and_token_mismatch_are_rejected():
    missing = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", []),
        as_of=AS_OF,
        params=params(),
    )
    mismatch = evaluate_binary_arbitrage(
        market(),
        asks("wrong-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
        params=params(),
    )

    assert missing.reason == "missing_two_sided_ask_liquidity"
    assert mismatch.reason == "yes_book_token_mismatch"


def test_malformed_duplicate_token_market_is_rejected():
    malformed = BinaryMarket(
        market_id="m-bad",
        condition_id="c-bad",
        question="Bad mapping",
        yes_token_id="same-token",
        no_token_id="same-token",
    )

    decision = evaluate_binary_arbitrage(
        malformed,
        asks("same-token", [(0.48, 10)]),
        asks("same-token", [(0.49, 10)]),
        as_of=AS_OF,
        params=params(),
    )

    assert decision.reason == "invalid_token_mapping"


def test_duplicate_position_guard_checks_market_and_condition_ids():
    decision = evaluate_binary_arbitrage(
        market(),
        asks("yes-token", [(0.48, 10)]),
        asks("no-token", [(0.49, 10)]),
        as_of=AS_OF,
        entered_positions={"other-key": {"condition_id": "c1"}},
        params=params(),
    )

    assert decision.reason == "already_entered"
