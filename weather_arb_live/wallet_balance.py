from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import requests


BRIDGED_USDC_TOKEN = "0x2791Bca1f2de4661ED88A30C99A7a9449Aa84174"
NATIVE_USDC_TOKEN = "0x3c499c542cef5e3811e1192ce70d8cc03d5c3359"
USDC_DECIMALS = Decimal(10**6)
BALANCE_OF_SELECTOR = "0x70a08231"

DEFAULT_RPC_URLS = (
    "https://polygon-bor-rpc.publicnode.com",
    "https://polygon.llamarpc.com",
    "https://polygon.drpc.org",
    "https://1rpc.io/matic",
)


@dataclass(frozen=True)
class WalletBalance:
    address: str
    token_address: str
    token_symbol: str
    balance: Decimal
    rpc_url: str


_CACHE: dict[tuple[str, str], tuple[float, WalletBalance]] = {}
_CACHE_LOCK = threading.Lock()


def rpc_urls() -> list[str]:
    urls: list[str] = []
    primary = os.getenv("POLYGON_RPC_URL")
    if primary:
        urls.append(primary)
    extra = os.getenv("POLYGON_RPC_FALLBACK_URLS")
    if extra:
        urls.extend(item.strip() for item in extra.split(",") if item.strip())
    urls.extend(DEFAULT_RPC_URLS)
    deduped: list[str] = []
    for url in urls:
        if url not in deduped:
            deduped.append(url)
    return deduped


def _clean_address(address: str) -> str:
    value = str(address or "").strip()
    if not value.startswith("0x") or len(value) != 42:
        raise ValueError(f"invalid EVM address: {address!r}")
    return value


def _balance_call_data(address: str) -> str:
    return BALANCE_OF_SELECTOR + _clean_address(address).lower().removeprefix("0x").rjust(64, "0")


def _eth_call(
    *,
    rpc_url: str,
    token_address: str,
    call_data: str,
    timeout_seconds: float,
) -> int:
    response = requests.post(
        rpc_url,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "eth_call",
            "params": [{"to": token_address, "data": call_data}, "latest"],
        },
        headers={"User-Agent": "weather-arb-live"},
        timeout=timeout_seconds,
    )
    response.raise_for_status()
    payload: Any = response.json()
    if not isinstance(payload, dict):
        raise ValueError(f"unexpected RPC response: {type(payload).__name__}")
    if payload.get("error"):
        raise ValueError(f"RPC error: {payload['error']}")
    result = payload.get("result")
    if not isinstance(result, str) or not result.startswith("0x"):
        raise ValueError(f"RPC response missing hex result: {payload!r}")
    return int(result, 16)


def fetch_erc20_balance(
    address: str,
    *,
    token_address: str = BRIDGED_USDC_TOKEN,
    token_symbol: str = "USDC.e",
    timeout_seconds: float = 4.0,
) -> WalletBalance:
    last_error: Exception | None = None
    call_data = _balance_call_data(address)
    for rpc_url in rpc_urls():
        try:
            raw_balance = _eth_call(
                rpc_url=rpc_url,
                token_address=token_address,
                call_data=call_data,
                timeout_seconds=timeout_seconds,
            )
            return WalletBalance(
                address=_clean_address(address),
                token_address=token_address,
                token_symbol=token_symbol,
                balance=Decimal(raw_balance) / USDC_DECIMALS,
                rpc_url=rpc_url,
            )
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"wallet balance RPC failed: {last_error}") from last_error


def fetch_cached_erc20_balance(
    address: str,
    *,
    token_address: str = BRIDGED_USDC_TOKEN,
    token_symbol: str = "USDC.e",
    ttl_seconds: float = 60.0,
    timeout_seconds: float = 4.0,
) -> WalletBalance:
    key = (_clean_address(address).lower(), token_address.lower())
    now = time.monotonic()
    with _CACHE_LOCK:
        cached = _CACHE.get(key)
        if cached and now - cached[0] <= ttl_seconds:
            return cached[1]

    balance = fetch_erc20_balance(
        address,
        token_address=token_address,
        token_symbol=token_symbol,
        timeout_seconds=timeout_seconds,
    )
    with _CACHE_LOCK:
        _CACHE[key] = (now, balance)
    return balance
