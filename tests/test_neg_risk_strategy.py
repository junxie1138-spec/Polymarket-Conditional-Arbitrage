from __future__ import annotations

from datetime import datetime, timezone

import pytest

from polymarket_conditional_arb.arb_models import BinaryMarket
from polymarket_conditional_arb.arb_strategy import ArbStrategyParams, evaluate_neg_risk_event_group
from polymarket_conditional_arb.order_book import asks_from_book

AS_OF = datetime(2026, 6, 8, 12, tzinfo=timezone.utc)


def market(index: int, *, event_id: str | None = "e1", yes_token: str | None = None, no_token: str | None = None):
    row = {
        "id": f"m{index}",
        "conditionId": f"c{index}",
        "question": f"Outcome {index}?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{yes_token or f"y{index}"}", "{no_token or f"n{index}"}"]',
        "negRisk": True,
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "_event_title": "Neg risk event",
    }
    if event_id is not None:
        row["_event_id"] = event_id
    parsed = BinaryMarket.from_gamma_market(row)
    assert parsed is not None
    return parsed


def ask_book(token_id: str, levels):
    return asks_from_book(
        {"asks": [{"price": price, "size": size} for price, size in levels]},
        token_id=token_id,
        updated_at=AS_OF,
    )


def books_for(markets, level_map):
    books = {}
    for item in markets:
        books[item.yes_token_id] = ask_book(item.yes_token_id, level_map.get(item.yes_token_id, []))
        books[item.no_token_id] = ask_book(item.no_token_id, level_map.get(item.no_token_id, []))
    return books


def params(**overrides):
    values = {
        "min_net_profit_usd": 0.0,
        "min_net_return_bps": 0.0,
        "max_capital_usd": 50.0,
        "slippage_buffer_bps": 0.0,
        "gas_cost_usd": 0.0,
        "taker_fee_bps": 0.0,
        "max_book_age_seconds": 20.0,
    }
    values.update(overrides)
    return ArbStrategyParams(**values)


def test_neg_risk_profitable_three_outcome_group_enters():
    markets = [market(0), market(1), market(2)]
    books = books_for(
        markets,
        {
            "y0": [(0.60, 10)],
            "n0": [(0.20, 10)],
        },
    )

    decision = evaluate_neg_risk_event_group(markets, books, as_of=AS_OF, params=params())

    assert decision.action == "ENTER"
    assert decision.opportunity is not None
    assert decision.opportunity.kind == "neg_risk_event_set"
    assert decision.opportunity.event_id == "e1"
    assert decision.opportunity.gross_cost == pytest.approx(8.0)
    assert decision.opportunity.collateral_redeemed == pytest.approx(10.0)
    assert decision.opportunity.net_profit == pytest.approx(2.0)


def test_neg_risk_solver_respects_capital_limit():
    markets = [market(0), market(1), market(2)]
    books = books_for(
        markets,
        {
            "y0": [(0.60, 100)],
            "n0": [(0.20, 100)],
        },
    )

    decision = evaluate_neg_risk_event_group(
        markets,
        books,
        as_of=AS_OF,
        params=params(max_capital_usd=4.0),
    )

    assert decision.action == "ENTER"
    assert decision.opportunity is not None
    assert decision.opportunity.gross_cost <= 4.0 + 1e-9
    assert decision.opportunity.collateral_redeemed == pytest.approx(5.0)


def test_neg_risk_unprofitable_group_is_rejected():
    markets = [market(0), market(1), market(2)]
    books = books_for(
        markets,
        {
            "y0": [(0.80, 10)],
            "n0": [(0.30, 10)],
        },
    )

    decision = evaluate_neg_risk_event_group(markets, books, as_of=AS_OF, params=params())

    assert decision.action == "SKIP"
    assert decision.reason == "not_profitable"


def test_neg_risk_missing_group_metadata_is_rejected():
    markets = [market(0, event_id=None), market(1, event_id=None), market(2, event_id=None)]
    books = books_for(markets, {})

    decision = evaluate_neg_risk_event_group(markets, books, as_of=AS_OF, params=params())

    assert decision.reason == "missing_grouping_metadata"


def test_neg_risk_duplicate_token_map_is_rejected():
    markets = [
        market(0, yes_token="shared-yes", no_token="n0"),
        market(1, yes_token="shared-yes", no_token="n1"),
        market(2, yes_token="y2", no_token="n2"),
    ]
    books = books_for(markets, {})

    decision = evaluate_neg_risk_event_group(markets, books, as_of=AS_OF, params=params())

    assert decision.reason == "invalid_token_mapping"
