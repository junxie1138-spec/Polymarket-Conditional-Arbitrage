from weather_arb_live.order_placer import OrderPlacer, build_order_intent


def test_order_intent_uses_slippage_and_position_cap():
    intent = build_order_intent(token_id="yes-token", market_price=0.40, position_usd=1.0, dry_run=True)

    assert intent.limit_price == 0.40 * 1.005
    assert intent.position_usd == 1.0
    assert intent.shares == intent.position_usd / intent.limit_price
    assert intent.order_type == "GTC"
    assert intent.side == "BUY"


def test_dry_run_order_does_not_require_credentials():
    placer = OrderPlacer(dry_run=True, clob_host="https://example.invalid")

    result = placer.place_yes_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is False
    assert result.response == {"dry_run": True}
