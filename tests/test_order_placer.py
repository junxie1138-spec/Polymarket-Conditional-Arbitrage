import os
import requests
import pytest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from weather_arb_live import order_placer
from weather_arb_live.order_placer import (
    InsufficientBalanceError,
    MissingCredentialsError,
    OrderPlacer,
    OrderPostRejectedError,
    build_order_intent,
)
from weather_arb_live.wallet_balance import WalletAllowance, WalletBalance


class FakeAuthError(Exception):
    status_code = 401

    def __str__(self):
        return "PolyApiException[status_code=401, error_message={'error': 'Unauthorized/Invalid api key'}]"


def test_order_intent_uses_slippage_and_position_cap():
    intent = build_order_intent(token_id="yes-token", market_price=0.40, position_usd=1.0, dry_run=True)

    assert intent.limit_price == 0.40 * 1.005
    assert intent.position_usd == 1.0
    assert intent.shares == intent.position_usd / intent.limit_price
    assert intent.order_type == "FOK"
    assert intent.side == "BUY"


def test_post_order_uses_non_resting_fok_order_type():
    intent = build_order_intent(token_id="yes-token", market_price=0.40, position_usd=1.0, dry_run=False)
    captured = {}

    class FakeClient:
        def create_and_post_order(self, **kwargs):
            captured.update(kwargs)
            return {"success": True}

    response = OrderPlacer._post_order(FakeClient(), intent)

    assert response == {"success": True}
    assert captured["order_type"] == "FOK"


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


def test_startup_auth_derives_missing_api_credentials_and_updates_dotenv(monkeypatch):
    env_path = Path("data/test_order_placer_auth.env")
    env_path.parent.mkdir(exist_ok=True)
    try:
        env_path.write_text(
            "\n".join(
                [
                    "DRY_RUN=false",
                    "POLYMARKET_API_KEY=",
                    "POLYMARKET_API_SECRET=",
                    "POLYMARKET_API_PASSPHRASE=",
                    "POLYMARKET_PRIVATE_KEY=private-key",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(order_placer.config, "DOTENV_PATH", env_path)
        monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "private-key")
        monkeypatch.delenv("POLYMARKET_API_KEY", raising=False)
        monkeypatch.delenv("POLYMARKET_API_SECRET", raising=False)
        monkeypatch.delenv("POLYMARKET_API_PASSPHRASE", raising=False)
        monkeypatch.setenv("POLYMARKET_AUTH_WRITE_DOTENV", "true")

        captured = {}

        class FakeClobClient:
            def __init__(self, **kwargs):
                captured["kwargs"] = kwargs
                self.creds = kwargs.get("creds")
                self.derived = False

            def create_or_derive_api_key(self):
                self.derived = True
                return SimpleNamespace(
                    api_key="derived-key",
                    api_secret="derived-secret",
                    api_passphrase="derived-passphrase",
                )

            def set_api_creds(self, creds):
                self.creds = creds

            def update_balance_allowance(self, _params):
                return None

            def get_balance_allowance(self, _params):
                return {"balance": "100", "allowance": "100"}

            def get_address(self):
                return "0x2222222222222222222222222222222222222222"

        monkeypatch.setattr("py_clob_client_v2.ClobClient", FakeClobClient)

        placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")
        placer.ensure_api_credentials()

        assert "creds" not in captured["kwargs"]
        assert placer._client.derived is True
        assert placer._client.creds.api_key == "derived-key"
        assert os.environ["POLYMARKET_API_KEY"] == "derived-key"
        dotenv = env_path.read_text(encoding="utf-8")
        assert "POLYMARKET_API_KEY=derived-key" in dotenv
        assert "POLYMARKET_API_SECRET=derived-secret" in dotenv
        assert "POLYMARKET_API_PASSPHRASE=derived-passphrase" in dotenv
    finally:
        env_path.unlink(missing_ok=True)


def test_live_client_rejects_invalid_signature_type(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "api-passphrase")
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "3")

    placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")

    with pytest.raises(MissingCredentialsError, match="POLYMARKET_SIGNATURE_TYPE"):
        placer._get_client()


def test_live_client_passes_builder_code_to_v2_builder_config(monkeypatch):
    builder_code = "0x" + "1" * 64
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "api-passphrase")
    monkeypatch.setenv("POLY_BUILDER_CODE", builder_code)

    captured = {}

    class FakeClobClient:
        def __init__(self, **kwargs):
            captured["kwargs"] = kwargs

    monkeypatch.setattr("py_clob_client_v2.ClobClient", FakeClobClient)

    placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")
    placer._get_client()

    assert captured["kwargs"]["builder_config"].builder_code == builder_code


def test_live_client_rejects_invalid_builder_code(monkeypatch):
    monkeypatch.setenv("POLYMARKET_PRIVATE_KEY", "private-key")
    monkeypatch.setenv("POLYMARKET_API_KEY", "api-key")
    monkeypatch.setenv("POLYMARKET_API_SECRET", "api-secret")
    monkeypatch.setenv("POLYMARKET_API_PASSPHRASE", "api-passphrase")
    monkeypatch.setenv("POLY_BUILDER_CODE", "not-bytes32")

    placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")

    with pytest.raises(MissingCredentialsError, match="POLY_BUILDER_CODE"):
        placer._get_client()


def test_clob_client_kwargs_uses_chain_alias_when_sdk_exposes_chain(monkeypatch):
    class ChainClient:
        def __init__(self, host, chain, key):
            pass

    class BuilderConfig:
        def __init__(self, builder_code=""):
            self.builder_code = builder_code

    monkeypatch.setenv("POLYMARKET_CHAIN_ID", "137")
    monkeypatch.delenv("POLY_BUILDER_CODE", raising=False)
    monkeypatch.delenv("POLYMARKET_BUILDER_CODE", raising=False)

    kwargs = order_placer.build_clob_client_kwargs(
        ChainClient,
        BuilderConfig,
        host="https://example.invalid",
        key="private-key",
    )

    assert kwargs["chain"] == 137
    assert "chain_id" not in kwargs


def _proxy_runtime_code(implementation: str) -> str:
    return (
        "0x"
        + order_placer.POLYMARKET_PROXY_RUNTIME_PREFIX
        + implementation.lower().removeprefix("0x")
        + order_placer.POLYMARKET_PROXY_RUNTIME_SUFFIX
    )


def test_wallet_configuration_rejects_proxy_wallet_with_safe_signature_type(monkeypatch):
    signer = "0x0000000000000000000000000000000000000001"
    implementation = "0x44e999d5c2f66ef08613b5c27311b18c26a0e74d"
    funder = order_placer._derive_polymarket_proxy_funder(signer, implementation)

    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", funder)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "2")
    monkeypatch.setattr(
        order_placer.wallet_balance,
        "fetch_contract_code",
        lambda *_args, **_kwargs: (_proxy_runtime_code(implementation), "https://rpc.example"),
    )

    class FakeClient:
        def get_address(self):
            return signer

    placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")

    with pytest.raises(MissingCredentialsError, match="POLYMARKET_SIGNATURE_TYPE must be 1"):
        placer._ensure_wallet_configuration_valid(FakeClient())


def test_wallet_configuration_rejects_proxy_wallet_signer_mismatch(monkeypatch):
    signer = "0x0000000000000000000000000000000000000001"
    implementation = "0x44e999d5c2f66ef08613b5c27311b18c26a0e74d"
    wrong_funder = "0x0000000000000000000000000000000000000002"

    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", wrong_funder)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    monkeypatch.setattr(
        order_placer.wallet_balance,
        "fetch_contract_code",
        lambda *_args, **_kwargs: (_proxy_runtime_code(implementation), "https://rpc.example"),
    )

    class FakeClient:
        def get_address(self):
            return signer

    placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")

    with pytest.raises(MissingCredentialsError, match="does not control"):
        placer._ensure_wallet_configuration_valid(FakeClient())


def test_wallet_configuration_accepts_matching_proxy_wallet(monkeypatch):
    signer = "0x0000000000000000000000000000000000000001"
    implementation = "0x44e999d5c2f66ef08613b5c27311b18c26a0e74d"
    funder = order_placer._derive_polymarket_proxy_funder(signer, implementation)

    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", funder)
    monkeypatch.setenv("POLYMARKET_SIGNATURE_TYPE", "1")
    monkeypatch.setattr(
        order_placer.wallet_balance,
        "fetch_contract_code",
        lambda *_args, **_kwargs: (_proxy_runtime_code(implementation), "https://rpc.example"),
    )

    class FakeClient:
        def get_address(self):
            return signer

    placer = OrderPlacer(dry_run=False, clob_host="https://example.invalid")

    placer._ensure_wallet_configuration_valid(FakeClient())

    assert placer._wallet_configuration_checked is True


def test_balance_preflight_refreshes_api_credentials_once_after_401(monkeypatch):
    monkeypatch.setenv("POLYMARKET_AUTH_WRITE_DOTENV", "false")

    class RefreshingClient:
        def __init__(self):
            self.update_calls = 0
            self.refresh_calls = 0
            self.creds = SimpleNamespace(
                api_key="old-key",
                api_secret="old-secret",
                api_passphrase="old-passphrase",
            )

        def update_balance_allowance(self, _params):
            self.update_calls += 1
            if self.update_calls == 1:
                raise FakeAuthError()

        def get_balance_allowance(self, _params):
            return {"balance": "100", "allowance": "100"}

        def create_or_derive_api_key(self):
            self.refresh_calls += 1
            return SimpleNamespace(
                api_key="new-key",
                api_secret="new-secret",
                api_passphrase="new-passphrase",
            )

        def set_api_creds(self, creds):
            self.creds = creds

    class RefreshingPlacer(OrderPlacer):
        def __init__(self):
            super().__init__(dry_run=False, clob_host="https://example.invalid")
            self.client = RefreshingClient()

        def _get_client(self):
            return self.client

        def _post_order(self, _client, _intent):
            return {"success": True}

    placer = RefreshingPlacer()

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is True
    assert placer.client.update_calls == 2
    assert placer.client.refresh_calls == 1
    assert placer.client.creds.api_key == "new-key"
    assert os.environ["POLYMARKET_API_KEY"] == "new-key"


def test_balance_preflight_refresh_derives_before_create_after_401(monkeypatch):
    monkeypatch.setenv("POLYMARKET_AUTH_WRITE_DOTENV", "false")

    class RefreshingClient:
        def __init__(self):
            self.update_calls = 0
            self.derive_calls = 0
            self.create_calls = 0

        def update_balance_allowance(self, _params):
            self.update_calls += 1
            if self.update_calls == 1:
                raise FakeAuthError()

        def get_balance_allowance(self, _params):
            return {"balance": "100", "allowance": "100"}

        def derive_api_key(self):
            self.derive_calls += 1
            return SimpleNamespace(
                api_key="derived-key",
                api_secret="derived-secret",
                api_passphrase="derived-passphrase",
            )

        def create_api_key(self):
            self.create_calls += 1
            raise AssertionError("refresh after 401 should derive before creating")

        def set_api_creds(self, creds):
            self.creds = creds

    class RefreshingPlacer(OrderPlacer):
        def __init__(self):
            super().__init__(dry_run=False, clob_host="https://example.invalid")
            self.client = RefreshingClient()

        def _get_client(self):
            return self.client

        def _post_order(self, _client, _intent):
            return {"success": True}

    placer = RefreshingPlacer()

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is True
    assert placer.client.update_calls == 2
    assert placer.client.derive_calls == 1
    assert placer.client.create_calls == 0
    assert placer.client.creds.api_key == "derived-key"


def test_fetch_open_orders_refreshes_api_credentials_after_401(monkeypatch):
    monkeypatch.setenv("POLYMARKET_AUTH_WRITE_DOTENV", "false")

    class OpenOrdersClient:
        def __init__(self):
            self.calls = 0
            self.refresh_calls = 0

        def get_open_orders(self):
            self.calls += 1
            if self.calls == 1:
                raise FakeAuthError()
            return [{"id": "order-1"}]

        def create_or_derive_api_key(self):
            self.refresh_calls += 1
            return SimpleNamespace(
                api_key="new-key",
                api_secret="new-secret",
                api_passphrase="new-passphrase",
            )

        def set_api_creds(self, creds):
            self.creds = creds

    class OpenOrdersPlacer(OrderPlacer):
        def __init__(self):
            super().__init__(dry_run=False, clob_host="https://example.invalid")
            self.client = OpenOrdersClient()

        def _get_client(self):
            return self.client

    placer = OpenOrdersPlacer()

    assert placer.fetch_open_orders() == [{"id": "order-1"}]
    assert placer.client.calls == 2
    assert placer.client.refresh_calls == 1


class BalanceClient:
    def __init__(self, response):
        self.response = response
        self.updated = False

    def update_balance_allowance(self, _params):
        self.updated = True

    def get_balance_allowance(self, _params):
        return self.response

    def get_address(self):
        return "0x2222222222222222222222222222222222222222"


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


def test_live_order_uses_wallet_balance_fallback_for_zero_clob_balance(monkeypatch):
    funder = "0x1111111111111111111111111111111111111111"
    calls = []
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", funder)

    def fake_fetch(address, *, token_address, token_symbol, ttl_seconds, timeout_seconds=4.0):
        calls.append((address, token_address, token_symbol, ttl_seconds, timeout_seconds))
        return WalletBalance(
            address=address,
            token_address=token_address,
            token_symbol=token_symbol,
            balance=Decimal("83.376702"),
            rpc_url="https://rpc.test",
        )

    monkeypatch.setattr(order_placer.wallet_balance, "fetch_cached_erc20_balance", fake_fetch)
    placer = BalanceGuardPlacer(
        {
            "balance": "0",
            "allowances": {
                "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E": "100",
            },
        }
    )

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is True
    assert placer.posted is True
    assert calls[0][0] == funder
    assert calls[0][1] == order_placer.wallet_balance.BRIDGED_USDC_TOKEN
    assert calls[0][2] == "USDC.e"


def test_live_order_uses_pusd_wallet_balance_fallback_for_v2_spenders(monkeypatch):
    funder = "0x1111111111111111111111111111111111111111"
    calls = []
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", funder)

    def fake_fetch(address, *, token_address, token_symbol, ttl_seconds, timeout_seconds=4.0):
        calls.append((address, token_address, token_symbol, ttl_seconds, timeout_seconds))
        return WalletBalance(
            address=address,
            token_address=token_address,
            token_symbol=token_symbol,
            balance=Decimal("83.376702"),
            rpc_url="https://rpc.test",
        )

    monkeypatch.setattr(order_placer.wallet_balance, "fetch_cached_erc20_balance", fake_fetch)
    placer = BalanceGuardPlacer(
        {
            "balance": "0",
            "allowances": {
                order_placer.V2_CTF_COLLATERAL_ADAPTER: "100",
            },
        }
    )

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is True
    assert calls[0][0] == funder
    assert calls[0][1] == order_placer.wallet_balance.PUSD_TOKEN
    assert calls[0][2] == "pUSD"


def test_live_order_blocks_when_collateral_allowance_is_too_low():
    placer = BalanceGuardPlacer({"balance": "100", "allowance": "0.50"})

    with pytest.raises(InsufficientBalanceError, match="collateral allowance"):
        placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert placer.posted is False


def test_live_order_uses_wallet_allowance_fallback_for_zero_clob_allowance(monkeypatch):
    funder = "0x1111111111111111111111111111111111111111"
    calls = []
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", funder)

    def fake_fetch(owner_address, spender_address, *, token_address, token_symbol, ttl_seconds, timeout_seconds=4.0):
        calls.append((owner_address, spender_address, token_address, token_symbol, ttl_seconds, timeout_seconds))
        return WalletAllowance(
            owner_address=owner_address,
            spender_address=spender_address,
            token_address=token_address,
            token_symbol=token_symbol,
            allowance=Decimal("100"),
            rpc_url="https://rpc.test",
        )

    class AllowanceFallbackClient(BalanceClient):
        def get_version(self):
            return 1

        def get_neg_risk(self, _token_id):
            return True

    monkeypatch.setattr(order_placer.wallet_balance, "fetch_cached_erc20_allowance", fake_fetch)
    placer = BalanceGuardPlacer({"balance": "100", "allowance": "0"})
    placer.client = AllowanceFallbackClient({"balance": "100", "allowance": "0"})

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is True
    assert calls[0][0] == funder
    assert calls[0][1] == order_placer.V1_NEG_RISK_EXCHANGE
    assert calls[0][2] == order_placer.wallet_balance.BRIDGED_USDC_TOKEN
    assert calls[0][3] == "USDC.e"


def test_live_order_uses_pusd_v2_exchange_allowance_fallback(monkeypatch):
    funder = "0x1111111111111111111111111111111111111111"
    calls = []
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", funder)

    def fake_fetch(owner_address, spender_address, *, token_address, token_symbol, ttl_seconds, timeout_seconds=4.0):
        calls.append((owner_address, spender_address, token_address, token_symbol, ttl_seconds, timeout_seconds))
        return WalletAllowance(
            owner_address=owner_address,
            spender_address=spender_address,
            token_address=token_address,
            token_symbol=token_symbol,
            allowance=Decimal("100"),
            rpc_url="https://rpc.test",
        )

    class AllowanceFallbackClient(BalanceClient):
        def get_version(self):
            return 2

        def get_neg_risk(self, _token_id):
            return True

    monkeypatch.setattr(order_placer.wallet_balance, "fetch_cached_erc20_allowance", fake_fetch)
    placer = BalanceGuardPlacer({"balance": "100", "allowance": "0"})
    placer.client = AllowanceFallbackClient({"balance": "100", "allowance": "0"})

    result = placer.place_order(token_id="yes-token", market_price=0.40, position_usd=1.0)

    assert result.posted is True
    assert calls[0][0] == funder
    assert calls[0][1] == order_placer.V2_NEG_RISK_EXCHANGE
    assert calls[0][2] == order_placer.wallet_balance.PUSD_TOKEN
    assert calls[0][3] == "pUSD"


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
