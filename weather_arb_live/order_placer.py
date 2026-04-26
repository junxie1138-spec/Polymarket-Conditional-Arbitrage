from __future__ import annotations

import logging
import os
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from . import config, network

logger = logging.getLogger(__name__)


class MissingCredentialsError(RuntimeError):
    pass


class BalancePreflightError(RuntimeError):
    pass


class InsufficientBalanceError(BalancePreflightError):
    pass


class OrderPostRejectedError(RuntimeError):
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


OrderSubmitCallback = Callable[[OrderIntent, int], None]

POLYMARKET_API_ENV_VARS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)
VALID_SIGNATURE_TYPES = {0, 1, 2}
LIVE_ORDER_TYPE = "FOK"


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
        order_type=LIVE_ORDER_TYPE,
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


def _is_api_key_auth_error(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if network.response_status(current) == 401:
            return True
        message = str(current).lower()
        error_msg = getattr(current, "error_msg", None)
        if error_msg is not None:
            message = f"{message} {error_msg}".lower()
        if "invalid api key" in message or ("unauthorized" in message and "api key" in message):
            return True
        current = current.__cause__ or current.__context__
    return False


def _is_retryable(exc: Exception) -> bool:
    return network.is_retryable_status(network.response_status(exc))


def _api_creds_values(creds) -> dict[str, str]:
    return {
        "POLYMARKET_API_KEY": str(getattr(creds, "api_key", "") or ""),
        "POLYMARKET_API_SECRET": str(getattr(creds, "api_secret", "") or ""),
        "POLYMARKET_API_PASSPHRASE": str(getattr(creds, "api_passphrase", "") or ""),
    }


def _set_api_creds_env(creds) -> None:
    for name, value in _api_creds_values(creds).items():
        if value:
            os.environ[name] = value


def _signature_type_from_env() -> int | str | None:
    value = os.getenv("POLYMARKET_SIGNATURE_TYPE")
    if not value:
        return None
    if not value.isdigit():
        raise MissingCredentialsError(
            "POLYMARKET_SIGNATURE_TYPE must be 0, 1, or 2 "
            f"(got {value!r})"
        )
    signature_type = int(value)
    if signature_type not in VALID_SIGNATURE_TYPES:
        raise MissingCredentialsError(
            "POLYMARKET_SIGNATURE_TYPE must be 0, 1, or 2 "
            f"(got {signature_type})"
        )
    return signature_type


def _replace_dotenv_creds(path: str | Path, creds) -> bool:
    if not config.polymarket_auth_write_dotenv():
        return False

    env_path = Path(path)
    if not env_path.exists():
        return False

    values = _api_creds_values(creds)
    if not all(values.values()):
        return False

    raw_lines = env_path.read_text(encoding="utf-8").splitlines()
    updated_lines: list[str] = []
    seen: set[str] = set()
    for raw_line in raw_lines:
        line = raw_line.strip()
        prefix = ""
        assignment = line
        if assignment.startswith("export "):
            prefix = "export "
            assignment = assignment[len("export ") :].strip()
        if not assignment.startswith("#") and "=" in assignment:
            name = assignment.split("=", 1)[0].strip()
            if name in values:
                updated_lines.append(f"{prefix}{name}={values[name]}")
                seen.add(name)
                continue
        updated_lines.append(raw_line)

    for name in POLYMARKET_API_ENV_VARS:
        if name not in seen:
            updated_lines.append(f"{name}={values[name]}")

    env_path.write_text("\n".join(updated_lines) + "\n", encoding="utf-8")
    return True


def _parse_decimal_amount(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise BalancePreflightError(f"balance preflight response missing numeric {field}")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise BalancePreflightError(f"balance preflight response has invalid {field}: {value!r}") from exc
    if not amount.is_finite():
        raise BalancePreflightError(f"balance preflight response has non-finite {field}: {value!r}")
    return amount


def _parse_balance_allowance(response: Any) -> tuple[Decimal, Decimal | None]:
    if not isinstance(response, dict):
        raise BalancePreflightError(
            f"balance preflight response must be an object, got {type(response).__name__}"
        )

    balance = _parse_decimal_amount(response.get("balance"), "balance")
    allowance: Decimal | None = None
    if response.get("allowance") not in (None, ""):
        allowance = _parse_decimal_amount(response.get("allowance"), "allowance")
    elif isinstance(response.get("allowances"), dict) and response["allowances"]:
        allowance_values = [
            _parse_decimal_amount(value, f"allowances[{key}]")
            for key, value in response["allowances"].items()
            if value not in (None, "")
        ]
        if allowance_values:
            allowance = min(allowance_values)
    return balance, allowance


def _raise_for_rejected_order_response(response: dict[str, Any]) -> None:
    error = response.get("error") or response.get("errorMsg") or response.get("error_msg")
    if error:
        raise OrderPostRejectedError(f"order rejected by CLOB: {error}")
    if response.get("success") is False:
        raise OrderPostRejectedError(f"order rejected by CLOB: {response}")


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
        on_submit_attempt: OrderSubmitCallback | None = None,
    ) -> OrderResult:
        intent = build_order_intent(
            token_id=token_id,
            market_price=market_price,
            position_usd=position_usd,
            dry_run=self.dry_run,
        )
        if intent.dry_run:
            if on_submit_attempt is not None:
                on_submit_attempt(intent, 0)
            logger.info("dry_run_order %s", asdict(intent))
            return OrderResult(intent=intent, posted=False, response={"dry_run": True})

        client = self._get_client()
        try:
            self._ensure_sufficient_collateral(client, intent)
        except BalancePreflightError as exc:
            if not _is_api_key_auth_error(exc):
                raise
            logger.warning("clob_auth_refresh reason=balance_preflight_401")
            client = self._refresh_api_credentials(client, reason="balance_preflight_401")
            self._ensure_sufficient_collateral(client, intent)

        last_exc: Exception | None = None
        auth_refreshed = False
        for attempt in range(1, 4):
            try:
                if on_submit_attempt is not None:
                    on_submit_attempt(intent, attempt)
                response = self._post_order(client, intent)
                response_dict = response if isinstance(response, dict) else {"response": response}
                _raise_for_rejected_order_response(response_dict)
                return OrderResult(intent=intent, posted=True, response=response_dict)
            except Exception as exc:
                last_exc = exc
                if not auth_refreshed and _is_api_key_auth_error(exc):
                    auth_refreshed = True
                    logger.warning("clob_auth_refresh reason=order_post_401")
                    client = self._refresh_api_credentials(client, reason="order_post_401")
                    continue
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

        api_key = os.getenv("POLYMARKET_API_KEY")
        api_secret = os.getenv("POLYMARKET_API_SECRET")
        api_passphrase = os.getenv("POLYMARKET_API_PASSPHRASE")
        creds = None
        if api_key and api_secret and api_passphrase:
            creds = ApiCreds(
                api_key=api_key,
                api_secret=api_secret,
                api_passphrase=api_passphrase,
            )
        kwargs: dict[str, Any] = {
            "host": self.clob_host,
            "chain_id": int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
            "key": _required_env("POLYMARKET_PRIVATE_KEY"),
        }
        if creds is not None:
            kwargs["creds"] = creds
        signature_type = _signature_type_from_env()
        if signature_type:
            kwargs["signature_type"] = signature_type
        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        if funder:
            kwargs["funder"] = funder

        self._client = ClobClient(**kwargs)
        if creds is None:
            logger.info("clob_auth_refresh reason=missing_api_credentials")
            self._refresh_api_credentials(self._client, reason="missing_api_credentials")
        return self._client

    def _refresh_api_credentials(self, client=None, *, reason: str):
        if client is None:
            client = self._get_client()
        creds = client.create_or_derive_api_key()
        client.set_api_creds(creds)
        _set_api_creds_env(creds)
        dotenv_updated = _replace_dotenv_creds(config.DOTENV_PATH, creds)
        logger.info("clob_auth_refreshed reason=%s dotenv_updated=%s", reason, dotenv_updated)
        return client

    def ensure_api_credentials(self) -> None:
        if self.dry_run:
            return

        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        client = self._get_client()
        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        try:
            client.update_balance_allowance(params)
            client.get_balance_allowance(params)
        except Exception as exc:
            if not _is_api_key_auth_error(exc):
                raise
            logger.warning("clob_auth_refresh reason=startup_401")
            client = self._refresh_api_credentials(client, reason="startup_401")
            client.update_balance_allowance(params)
            client.get_balance_allowance(params)
        logger.info("clob_auth_ok address=%s", client.get_address())

    def get_client_address(self) -> str:
        if self.dry_run:
            raise MissingCredentialsError("client address is only available when DRY_RUN=false")
        return str(self._get_client().get_address())

    def fetch_open_orders(self) -> list[dict[str, Any]]:
        if self.dry_run:
            return []

        client = self._get_client()
        last_exc: Exception | None = None
        auth_refreshed = False
        for attempt in range(1, 4):
            try:
                response = client.get_open_orders()
                if not isinstance(response, list):
                    raise ValueError(f"unexpected open orders response: {type(response).__name__}")
                return [row for row in response if isinstance(row, dict)]
            except Exception as exc:
                last_exc = exc
                if not auth_refreshed and _is_api_key_auth_error(exc):
                    auth_refreshed = True
                    logger.warning("clob_auth_refresh reason=open_orders_401")
                    client = self._refresh_api_credentials(client, reason="open_orders_401")
                    continue
                if attempt == 3 or not network.is_retryable_exception(exc):
                    raise
                sleep_seconds = 2 ** (attempt - 1)
                logger.warning(
                    "open_orders_retry attempt=%s sleep=%s error=%s",
                    attempt,
                    sleep_seconds,
                    exc,
                )
                time.sleep(sleep_seconds)
        raise RuntimeError(f"open orders fetch failed after retries: {last_exc}")

    @staticmethod
    def _ensure_sufficient_collateral(client, intent: OrderIntent) -> None:
        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
        try:
            client.update_balance_allowance(params)
            response = client.get_balance_allowance(params)
        except BalancePreflightError:
            raise
        except Exception as exc:
            raise BalancePreflightError(f"balance preflight failed before order submit: {exc}") from exc

        balance, allowance = _parse_balance_allowance(response)
        required = Decimal(str(intent.position_usd))
        if balance < required:
            raise InsufficientBalanceError(
                f"insufficient Polymarket collateral balance: required={required} available={balance}"
            )
        if allowance is not None and allowance < required:
            raise InsufficientBalanceError(
                f"insufficient Polymarket collateral allowance: required={required} available={allowance}"
            )
        logger.info(
            "balance_preflight_ok required_usd=%s collateral_balance=%s collateral_allowance=%s",
            required,
            balance,
            allowance,
        )

    @staticmethod
    def _post_order(client, intent: OrderIntent):
        from py_clob_client_v2 import OrderArgs, OrderType, PartialCreateOrderOptions, Side

        order_type = getattr(OrderType, intent.order_type, None)
        if order_type is None:
            raise ValueError(f"unsupported Polymarket order type: {intent.order_type!r}")

        return client.create_and_post_order(
            order_args=OrderArgs(
                token_id=intent.token_id,
                price=intent.limit_price,
                side=Side.BUY,
                size=intent.shares,
            ),
            options=PartialCreateOrderOptions(tick_size=os.getenv("POLYMARKET_TICK_SIZE", "0.01")),
            order_type=order_type,
        )
