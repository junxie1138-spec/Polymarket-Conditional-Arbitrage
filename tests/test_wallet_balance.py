from decimal import Decimal

from weather_arb_live import wallet_balance


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


def test_fetch_erc20_balance_reads_usdc_units(monkeypatch):
    calls = []

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return FakeResponse({"result": hex(83_376_702)})

    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.test")
    monkeypatch.setattr(wallet_balance.requests, "post", fake_post)

    balance = wallet_balance.fetch_erc20_balance("0x1111111111111111111111111111111111111111")

    assert balance.balance == Decimal("83.376702")
    assert balance.token_symbol == "USDC.e"
    assert balance.rpc_url == "https://rpc.test"
    assert calls[0][1]["method"] == "eth_call"
    assert calls[0][1]["params"][0]["data"].startswith(wallet_balance.BALANCE_OF_SELECTOR)


def test_fetch_erc20_balance_tries_rpc_fallback(monkeypatch):
    attempts = []

    def fake_post(url, *, json, headers, timeout):
        attempts.append(url)
        if len(attempts) == 1:
            raise RuntimeError("primary down")
        return FakeResponse({"result": "0x0"})

    monkeypatch.setenv("POLYGON_RPC_URL", "https://primary.test")
    monkeypatch.setenv("POLYGON_RPC_FALLBACK_URLS", "https://fallback.test")
    monkeypatch.setattr(wallet_balance.requests, "post", fake_post)

    balance = wallet_balance.fetch_erc20_balance("0x1111111111111111111111111111111111111111")

    assert balance.balance == Decimal("0")
    assert attempts[:2] == ["https://primary.test", "https://fallback.test"]


def test_fetch_cached_collateral_balance_returns_first_positive_token(monkeypatch):
    responses = {
        wallet_balance.PUSD_TOKEN.lower(): "0x0",
        wallet_balance.BRIDGED_USDC_TOKEN.lower(): hex(83_376_702),
        wallet_balance.NATIVE_USDC_TOKEN.lower(): "0x0",
    }

    def fake_post(url, *, json, headers, timeout):
        token = json["params"][0]["to"].lower()
        return FakeResponse({"result": responses[token]})

    with wallet_balance._CACHE_LOCK:
        wallet_balance._CACHE.clear()
    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.test")
    monkeypatch.setattr(wallet_balance.requests, "post", fake_post)

    balance = wallet_balance.fetch_cached_collateral_balance(
        "0x1111111111111111111111111111111111111111"
    )

    assert balance.balance == Decimal("83.376702")
    assert balance.token_symbol == "USDC.e"


def test_fetch_erc20_allowance_reads_usdc_units(monkeypatch):
    calls = []

    def fake_post(url, *, json, headers, timeout):
        calls.append((url, json, headers, timeout))
        return FakeResponse({"result": hex(50_000_000)})

    monkeypatch.setenv("POLYGON_RPC_URL", "https://rpc.test")
    monkeypatch.setattr(wallet_balance.requests, "post", fake_post)

    allowance = wallet_balance.fetch_erc20_allowance(
        "0x1111111111111111111111111111111111111111",
        "0x2222222222222222222222222222222222222222",
    )

    assert allowance.allowance == Decimal("50")
    assert allowance.token_symbol == "USDC.e"
    assert allowance.rpc_url == "https://rpc.test"
    assert calls[0][1]["params"][0]["data"].startswith(wallet_balance.ALLOWANCE_SELECTOR)
