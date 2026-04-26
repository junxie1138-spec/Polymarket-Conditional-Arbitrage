import requests
import pytest

from weather_arb_live import order_placer
from weather_arb_live.order_placer import (
    InsufficientBalanceError,
    OrderPlacer,
    OrderPostRejectedError,
    build_order_intent,
)


def test_order_intent_uses_slippage_and_position_cap():
    intent = build_order_intent(token_id="yes-token", market_price=0.40, position_usd=1.0, dry_run=True)

    assert intent.limit_price == 0.40 * 1.005
    assert intent.position_usd == 1.0
    assert intent.shares == intent.position_usd / intent.limit_price
    assert intent.order_type == "GTC"
    assert intent.side == "BUY"


def test_dry_run_order_does_not_require_credentials():
    placer = OrderPlacer(dry_run=True, clob_host="https://example.invalid")
    attempts = []

    result = placer.place_order(
        token_id="yes-token",
        market_price=0.40,
        position_usd=1.0,
        on_submit_attempt=lambda intent, attempt: attempts.append((attempt, intent.dry_run)),
    )

    assert result.posted is False
    assert result.response == {"dry_run": True}
    assert attempts == [(0, True)]


def test_live_order_retries_retryable_http_error(monkeypatch):
    monkeypatch.setattr(order_placer.time, "sleep", lambda *_args: None)

    class RetryPlacer(OrderPlacer):
        def __init__(self):
            super().__init__(dry_run=False, clob_host="https://example.invalid")
            self.calls = 0

        def _get_client(self):
            return object()

        def _ensure_sufficient_collateral(self, _client, _intent):
            return None

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


class BalanceClient:
    def __init__(self, response):
        self.response = response
        self.updated = False

    def update_balance_allowance(self, _params):
        self.updated = True

    def get_balance_allowance(self, _params):
        return self.response


class BalanceGuardPlacer(OrderPlacer):
    def __init__(self, response):
        super().__init__(dry_run=False, clob_host="https://example.invalid")
        self.client = BalanceClient(response)
        self.posted = False

    def _get_client(self):
        return self.client

    def _post_order(self, _client, _intent):
        self.posted = True
        return {"success": True}


def test_live_order_blocks_when_collateral_balance_is_too_low():
    placer = BalanceGuardPlacer({"balance": "0.50", "allowance": "100"})

    with pytest.raises(InsufficientBalanceError, match="collateral balance"):
        placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert placer.client.updated is True
    assert placer.posted is False


def test_live_order_blocks_when_collateral_allowance_is_too_low():
    placer = BalanceGuardPlacer({"balance": "100", "allowance": "0.50"})

    with pytest.raises(InsufficientBalanceError, match="collateral allowance"):
        placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert placer.posted is False


def test_live_order_uses_lowest_allowance_from_allowance_map():
    placer = BalanceGuardPlacer(
        {
            "balance": "100",
            "allowances": {
                "exchange": "100",
                "neg_risk_exchange": "0.50",
            },
        }
    )

    with pytest.raises(InsufficientBalanceError, match="collateral allowance"):
        placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert placer.posted is False


def test_live_order_posts_after_successful_balance_preflight():
    placer = BalanceGuardPlacer({"balance": "100", "allowance": "100"})
    attempts = []

    result = placer.place_order(
        token_id="yes-token",
        market_price=0.40,
        position_usd=1.0,
        on_submit_attempt=lambda intent, attempt: attempts.append((attempt, intent.limit_price)),
    )

    assert placer.posted is True
    assert result.posted is True
    assert result.response == {"success": True}
    assert attempts == [(1, 0.40 * 1.005)]


def test_live_order_rejects_error_response_without_marking_posted():
    class ErrorResponsePlacer(BalanceGuardPlacer):
        def _post_order(self, _client, _intent):
            self.posted = True
            return {"success": False, "error": "not enough balance / allowance"}

    placer = ErrorResponsePlacer({"balance": "100", "allowance": "100"})

    with pytest.raises(OrderPostRejectedError, match="not enough balance"):
        placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert placer.posted is True
