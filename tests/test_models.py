from __future__ import annotations

from polymarket_conditional_arb.arb_models import BinaryMarket
from polymarket_conditional_arb.fetcher import GammaClobClient


def raw_market(**overrides):
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


def test_binary_market_parses_yes_no_tokens_and_event_context():
    market = BinaryMarket.from_gamma_market(
        raw_market(_event_id="e1", _event_title="Event", _event_neg_risk=True)
    )

    assert market is not None
    assert market.market_id == "m1"
    assert market.yes_token_id == "yes-token"
    assert market.no_token_id == "no-token"
    assert market.event_id == "e1"
    assert market.neg_risk is True
    assert market.is_tradable is True


def test_binary_market_effective_min_order_size_applies_polymarket_floor():
    missing = BinaryMarket.from_gamma_market(raw_market())
    below = BinaryMarket.from_gamma_market(raw_market(orderMinSize="1.25"))
    above = BinaryMarket.from_gamma_market(raw_market(orderMinSize="7.5"))

    assert missing is not None
    assert below is not None
    assert above is not None
    assert missing.min_order_size is None
    assert missing.effective_min_order_size == 5.0
    assert below.min_order_size == 1.25
    assert below.effective_min_order_size == 5.0
    assert above.min_order_size == 7.5
    assert above.effective_min_order_size == 7.5


def test_binary_market_rejects_missing_or_duplicate_yes_no_mapping():
    assert BinaryMarket.from_gamma_market(raw_market(outcomes='["Up", "Down"]')) is None
    assert (
        BinaryMarket.from_gamma_market(
            raw_market(outcomes='["Yes", "No"]', clobTokenIds='["same-token", "same-token"]')
        )
        is None
    )


def test_tradable_binary_markets_require_open_orderbook_enabled_markets():
    markets = GammaClobClient.tradable_binary_markets(
        [
            raw_market(id="ok", clobTokenIds='["yes-ok", "no-ok"]'),
            raw_market(id="closed", closed=True, clobTokenIds='["yes-c", "no-c"]'),
            raw_market(id="disabled", enableOrderBook=False, clobTokenIds='["yes-d", "no-d"]'),
            raw_market(id="bad", outcomes='["Home", "Away"]', clobTokenIds='["h", "a"]'),
        ]
    )

    assert [market.market_id for market in markets] == ["ok"]


def test_binary_market_prefers_non_empty_alias_values():
    market = BinaryMarket.from_gamma_market(
        raw_market(
            acceptingOrders="",
            accepting_orders=False,
            negRisk="",
            neg_risk=True,
        )
    )

    assert market is not None
    assert market.accepting_orders is False
    assert market.neg_risk is True
    assert market.is_tradable is False
