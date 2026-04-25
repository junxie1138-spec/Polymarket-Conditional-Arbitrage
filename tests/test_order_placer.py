import requests

from weather_arb_live import order_placer
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


def test_live_order_retries_retryable_http_error(monkeypatch):
    monkeypatch.setattr(order_placer.time, "sleep", lambda *_args: None)

    class RetryPlacer(OrderPlacer):
        def __init__(self):
            super().__init__(dry_run=False, clob_host="https://example.invalid")
            self.calls = 0

        def _get_client(self):
            return object()

        def _post_order(self, _client, _intent):
            self.calls += 1
            if self.calls == 1:
                response = requests.Response()
                response.status_code = 503
                raise requests.HTTPError("server unavailable", response=response)
            return {"ok": True}

    placer = RetryPlacer()

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert placer.calls == 2
    assert result.posted is True
    assert result.response == {"ok": True}
