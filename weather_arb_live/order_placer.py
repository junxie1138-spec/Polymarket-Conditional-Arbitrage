from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from typing import Any

from . import config, network

logger = logging.getLogger(__name__)


class MissingCredentialsError(RuntimeError):
    pass


@dataclass(frozen=True)
class OrderIntent:
    token_id: str
    side: str
    order_type: str
    market_price: float
    limit_price: float
    shares: float
    position_usd: float
    dry_run: bool


@dataclass(frozen=True)
class OrderResult:
    intent: OrderIntent
    posted: bool
    response: dict[str, Any] | None = None


def build_order_intent(
    *,
    token_id: str,
    market_price: float,
    position_usd: float | None = None,
    dry_run: bool | None = None,
) -> OrderIntent:
    max_usd = position_usd if position_usd is not None else config.max_position_usd()
    limit_price = min(0.999, float(market_price) * (1.0 + config.SLIPPAGE))
    return OrderIntent(
        token_id=token_id,
        side="BUY",
        order_type="GTC",
        market_price=float(market_price),
        limit_price=limit_price,
        shares=max_usd / limit_price,
        position_usd=max_usd,
        dry_run=config.dry_run() if dry_run is None else dry_run,
    )


def _required_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise MissingCredentialsError(f"{name} is required when DRY_RUN=false")
    return value


def _is_retryable(exc: Exception) -> bool:
    return network.is_retryable_status(network.response_status(exc))


class OrderPlacer:
    def __init__(self, *, clob_host: str | None = None, dry_run: bool | None = None):
        self.clob_host = (clob_host or config.clob_host()).rstrip("/")
        self.dry_run = config.dry_run() if dry_run is None else dry_run
        network.install()
        self._client = None

    def place_order(
        self,
        *,
        token_id: str,
        market_price: float,
        position_usd: float | None = None,
    ) -> OrderResult:
        intent = build_order_intent(
            token_id=token_id,
            market_price=market_price,
            position_usd=position_usd,
            dry_run=self.dry_run,
        )
        if intent.dry_run:
            logger.info("dry_run_order %s", asdict(intent))
            return OrderResult(intent=intent, posted=False, response={"dry_run": True})

        client = self._get_client()
        last_exc: Exception | None = None
        for attempt in range(1, 4):
            try:
                response = self._post_order(client, intent)
                response_dict = response if isinstance(response, dict) else {"response": response}
                return OrderResult(intent=intent, posted=True, response=response_dict)
            except Exception as exc:
                last_exc = exc
                if attempt == 3 or not _is_retryable(exc):
                    raise
                sleep_seconds = 2 ** (attempt - 1)
                logger.warning("order_retry attempt=%s sleep=%s error=%s", attempt, sleep_seconds, exc)
                time.sleep(sleep_seconds)
        raise RuntimeError(f"order failed after retries: {last_exc}")

    def place_yes_order(
        self,
        *,
        token_id: str,
        market_price: float,
        position_usd: float | None = None,
    ) -> OrderResult:
        return self.place_order(
            token_id=token_id,
            market_price=market_price,
            position_usd=position_usd,
        )

    def _get_client(self):
        if self._client is not None:
            return self._client

        from py_clob_client_v2 import ApiCreds, ClobClient

        creds = ApiCreds(
            api_key=_required_env("POLYMARKET_API_KEY"),
            api_secret=_required_env("POLYMARKET_API_SECRET"),
            api_passphrase=_required_env("POLYMARKET_API_PASSPHRASE"),
        )
        kwargs: dict[str, Any] = {
            "host": self.clob_host,
            "chain_id": int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
            "key": _required_env("POLYMARKET_PRIVATE_KEY"),
            "creds": creds,
        }
        signature_type = os.getenv("POLYMARKET_SIGNATURE_TYPE")
        if signature_type:
            kwargs["signature_type"] = int(signature_type) if signature_type.isdigit() else signature_type
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        if funder:
            kwargs["funder"] = funder

        self._client = ClobClient(**kwargs)
        return self._client

    @staticmethod
    def _post_order(client, intent: OrderIntent):
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        return client.create_and_post_order(
            order_args=OrderArgs(
                token_id=intent.token_id,
                price=intent.limit_price,
                side=Side.BUY,
                size=intent.shares,
            ),
            options=PartialCreateOrderOptions(tick_size=os.getenv("POLYMARKET_TICK_SIZE", "0.01")),
            order_type=OrderType.GTC,
        )
