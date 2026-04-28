from __future__ import annotations

import logging
import os
import inspect
import time
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable

from . import config, network, wallet_balance

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


@dataclass(frozen=True)
class CollateralPreflightContext:
    token_symbol: str
    token_address: str
    spender_address: str
    order_version: int
    neg_risk: bool


OrderSubmitCallback = Callable[[OrderIntent, int], None]

POLYMARKET_API_ENV_VARS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
)
VALID_SIGNATURE_TYPES = {0, 1, 2}
LIVE_ORDER_TYPE = "FOK"
BYTES32_ZERO = "0x" + "0" * 64
V1_COLLATERAL_SPENDERS = {
    "0x4bfb41d5b3570defd03c39a9a4d8de6bd8b8982e",
    "0xc5d563a36ae78145c45a50134d48a1215220f80a",
    "0xd91e80cf2e7be2e162c6513ced06f1dd0da35296",
}
V1_EXCHANGE = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
V1_NEG_RISK_EXCHANGE = "0xC5d563A36AE78145C45a50134d48A1215220f80a"
V2_EXCHANGE = "0xE111180000d2663C0091e4f400237545B87B996B"
V2_NEG_RISK_EXCHANGE = "0xe2222d279d744050d28e00520010520000310F59"
V2_CTF_COLLATERAL_ADAPTER = "0xADa100874d00e3331D00F2007a9c336a65009718"
V2_NEG_RISK_CTF_COLLATERAL_ADAPTER = "0xAdA200001000ef00D07553cEE7006808F895c6F1"
V2_COLLATERAL_SPENDERS = {
    V2_EXCHANGE.lower(),
    V2_NEG_RISK_EXCHANGE.lower(),
    V2_CTF_COLLATERAL_ADAPTER.lower(),
    V2_NEG_RISK_CTF_COLLATERAL_ADAPTER.lower(),
    "0x4d97dcd97ec945f40cf65f87097ace5ea0476045",
}
POLYMARKET_PROXY_FACTORY = "0xaB45c5A4B0c941a2F231C04C3f49182e1A254052"
POLYMARKET_PROXY_RUNTIME_PREFIX = "363d3d373d3d3d363d73"
POLYMARKET_PROXY_RUNTIME_SUFFIX = "5af43d82803e903d91602b57fd5bf3"


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


def _is_invalid_signature_error(exc: Exception) -> bool:
    current: BaseException | None = exc
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        error_msg = getattr(current, "error_msg", None)
        if error_msg is not None:
            message = f"{message} {error_msg}".lower()
        if "invalid signature" in message:
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


def _bytes32_from_env(*names: str) -> str | None:
    for name in names:
        value = os.getenv(name)
        if not value:
            continue
        normalized = value.strip()
        if not normalized:
            continue
        if (
            not normalized.startswith("0x")
            or len(normalized) != len(BYTES32_ZERO)
            or any(char not in "0123456789abcdefABCDEF" for char in normalized[2:])
        ):
            raise MissingCredentialsError(
                f"{name} must be a 32-byte hex value like 0x{'0' * 64}"
            )
        return normalized
    return None


def _builder_config_from_env(builder_config_cls):
    builder_code = _bytes32_from_env("POLY_BUILDER_CODE", "POLYMARKET_BUILDER_CODE")
    if not builder_code or builder_code == BYTES32_ZERO:
        return None

    params = inspect.signature(builder_config_cls).parameters
    if "builder_code" in params:
        return builder_config_cls(builder_code=builder_code)
    if "builderCode" in params:
        return builder_config_cls(builderCode=builder_code)

    config_obj = builder_config_cls()
    setattr(config_obj, "builder_code", builder_code)
    return config_obj


def _clob_chain_kwarg(clob_client_cls) -> str:
    params = inspect.signature(clob_client_cls).parameters
    if "chain_id" in params:
        return "chain_id"
    if "chain" in params:
        return "chain"
    return "chain_id"


def build_clob_client_kwargs(
    clob_client_cls,
    builder_config_cls,
    *,
    host: str,
    key: str,
    creds: Any | None = None,
) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "host": host,
        _clob_chain_kwarg(clob_client_cls): int(os.getenv("POLYMARKET_CHAIN_ID", "137")),
        "key": key,
    }
    if creds is not None:
        kwargs["creds"] = creds
    signature_type = _signature_type_from_env()
    if signature_type is not None:
        kwargs["signature_type"] = signature_type
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    if funder:
        kwargs["funder"] = funder
    builder_config = _builder_config_from_env(builder_config_cls)
    if builder_config is not None:
        kwargs["builder_config"] = builder_config
    return kwargs


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


def _mask_address(value: str | None) -> str | None:
    if not value:
        return None
    address = str(value).strip()
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def _wallet_balance_preflight_fallback_enabled() -> bool:
    return config.env_bool("POLYMARKET_WALLET_BALANCE_PREFLIGHT_FALLBACK", True)


def _allowance_spenders(response: Any) -> set[str]:
    if not isinstance(response, dict) or not isinstance(response.get("allowances"), dict):
        return set()
    return {str(spender).lower() for spender in response["allowances"]}


def _preferred_wallet_collateral_token(response: Any) -> tuple[str, str] | None:
    spenders = _allowance_spenders(response)
    if spenders & V2_COLLATERAL_SPENDERS:
        return "pUSD", wallet_balance.PUSD_TOKEN
    if spenders & V1_COLLATERAL_SPENDERS:
        return "USDC.e", wallet_balance.BRIDGED_USDC_TOKEN
    return None


def _version_from_balance_response(response: Any) -> int | None:
    spenders = _allowance_spenders(response)
    if spenders & V2_COLLATERAL_SPENDERS:
        return 2
    if spenders & V1_COLLATERAL_SPENDERS:
        return 1
    return None


def _clean_evm_address(address: str) -> str:
    value = str(address or "").strip()
    if not value.startswith("0x") or len(value) != 42:
        raise ValueError(f"invalid EVM address: {address!r}")
    return value


def _evm_address_bytes(address: str) -> bytes:
    return bytes.fromhex(_clean_evm_address(address).removeprefix("0x"))


def _polymarket_proxy_implementation_from_runtime_code(code: str) -> str | None:
    raw = str(code or "").lower().removeprefix("0x")
    expected_len = (
        len(POLYMARKET_PROXY_RUNTIME_PREFIX)
        + 40
        + len(POLYMARKET_PROXY_RUNTIME_SUFFIX)
    )
    if len(raw) != expected_len:
        return None
    if not raw.startswith(POLYMARKET_PROXY_RUNTIME_PREFIX):
        return None
    if not raw.endswith(POLYMARKET_PROXY_RUNTIME_SUFFIX):
        return None
    start = len(POLYMARKET_PROXY_RUNTIME_PREFIX)
    return "0x" + raw[start : start + 40]


def _polymarket_proxy_creation_code(factory: str, implementation: str) -> bytes:
    from eth_utils import keccak

    selector = keccak(text="cloneConstructor(bytes)")[:4]
    empty_bytes_arg = selector + (32).to_bytes(32, "big") + (0).to_bytes(32, "big")
    return (
        bytes.fromhex("3d3d606380380380913d393d73")
        + _evm_address_bytes(factory)
        + bytes.fromhex("5af4602a57600080fd5b602d8060366000396000f3363d3d373d3d3d363d73")
        + _evm_address_bytes(implementation)
        + bytes.fromhex("5af43d82803e903d91602b57fd5bf3")
        + empty_bytes_arg
    )


def _derive_polymarket_proxy_funder(signer_address: str, implementation: str) -> str:
    from eth_utils import keccak, to_checksum_address

    salt = keccak(_evm_address_bytes(signer_address))
    creation_code = _polymarket_proxy_creation_code(POLYMARKET_PROXY_FACTORY, implementation)
    raw_address = keccak(
        b"\xff"
        + _evm_address_bytes(POLYMARKET_PROXY_FACTORY)
        + salt
        + keccak(creation_code)
    )[-20:]
    return to_checksum_address(raw_address)


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
        self._wallet_configuration_checked = False

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
        self._ensure_wallet_configuration_valid(client)
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
                if _is_invalid_signature_error(exc):
                    self._wallet_configuration_checked = False
                    self._ensure_wallet_configuration_valid(client, force=True)
                    raise OrderPostRejectedError(
                        "Polymarket rejected the signed order as invalid. "
                        "Check POLYMARKET_PRIVATE_KEY, POLYMARKET_SIGNATURE_TYPE, "
                        "and POLYMARKET_FUNDER_ADDRESS; API credentials alone cannot "
                        "fix a signer/funder mismatch."
                    ) from exc
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

        from py_clob_client_v2 import ApiCreds, BuilderConfig, ClobClient

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
        kwargs = build_clob_client_kwargs(
            ClobClient,
            BuilderConfig,
            host=self.clob_host,
            key=_required_env("POLYMARKET_PRIVATE_KEY"),
            creds=creds,
        )

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

    def _ensure_wallet_configuration_valid(self, client, *, force: bool = False) -> None:
        if self.dry_run:
            return
        if self._wallet_configuration_checked and not force:
            return

        funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        if not funder:
            self._wallet_configuration_checked = True
            return

        signature_type = _signature_type_from_env()
        try:
            signer_address = str(client.get_address())
        except Exception as exc:
            logger.warning(
                "wallet_configuration_check_unavailable funder=%s error=%s",
                _mask_address(funder),
                exc,
            )
            self._wallet_configuration_checked = True
            return
        try:
            code, rpc_url = wallet_balance.fetch_contract_code(
                funder,
                timeout_seconds=4.0,
            )
        except Exception as exc:
            logger.warning(
                "wallet_configuration_check_unavailable signer=%s funder=%s error=%s",
                _mask_address(signer_address),
                _mask_address(funder),
                exc,
            )
            self._wallet_configuration_checked = True
            return

        implementation = _polymarket_proxy_implementation_from_runtime_code(code)
        if implementation is None:
            self._wallet_configuration_checked = True
            return

        derived_funder = _derive_polymarket_proxy_funder(signer_address, implementation)
        logger.info(
            "wallet_configuration_check signer=%s funder=%s derived_proxy_funder=%s "
            "wallet_type=polymarket_proxy signature_type=%s rpc=%s",
            _mask_address(signer_address),
            _mask_address(funder),
            _mask_address(derived_funder),
            signature_type if signature_type is not None else "unset",
            rpc_url,
        )

        if signature_type != 1:
            raise MissingCredentialsError(
                "POLYMARKET_FUNDER_ADDRESS is a Polymarket Proxy wallet clone, "
                f"so POLYMARKET_SIGNATURE_TYPE must be 1, got {signature_type!r}. "
                "Restart the bot after changing .env."
            )
        if derived_funder.lower() != _clean_evm_address(funder).lower():
            raise MissingCredentialsError(
                "POLYMARKET_PRIVATE_KEY does not control the configured "
                "POLYMARKET_FUNDER_ADDRESS. "
                f"Signer {signer_address} derives proxy {derived_funder}, "
                f"but .env funder is {funder}. Use the private key exported from "
                "the Polymarket account that owns the funded proxy wallet, or move "
                "funds to the derived proxy wallet."
            )
        self._wallet_configuration_checked = True

    def ensure_api_credentials(self) -> None:
        if self.dry_run:
            return

        from py_clob_client_v2 import AssetType, BalanceAllowanceParams

        client = self._get_client()
        self._ensure_wallet_configuration_valid(client)
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

    def _fetch_wallet_collateral_for_preflight(
        self,
        client,
        response: Any,
    ) -> wallet_balance.WalletBalance | None:
        if not _wallet_balance_preflight_fallback_enabled():
            return None

        address = os.getenv("POLYMARKET_FUNDER_ADDRESS")
        if not address:
            address = str(client.get_address())

        preferred = _preferred_wallet_collateral_token(response)
        try:
            if preferred is not None:
                token_symbol, token_address = preferred
                return wallet_balance.fetch_cached_erc20_balance(
                    address,
                    token_address=token_address,
                    token_symbol=token_symbol,
                    ttl_seconds=config.wallet_balance_ttl_seconds(),
                )
            return wallet_balance.fetch_cached_collateral_balance(
                address,
                ttl_seconds=config.wallet_balance_ttl_seconds(),
            )
        except Exception as exc:
            logger.warning(
                "wallet_balance_preflight_fallback_unavailable address=%s error=%s",
                _mask_address(address),
                exc,
            )
            return None

    def _collateral_preflight_context(
        self,
        client,
        intent: OrderIntent,
        response: Any,
    ) -> CollateralPreflightContext:
        try:
            order_version = int(client.get_version())
        except Exception:
            order_version = _version_from_balance_response(response) or 1

        try:
            neg_risk = bool(client.get_neg_risk(intent.token_id))
        except Exception:
            neg_risk = False

        if order_version == 2:
            return CollateralPreflightContext(
                token_symbol="pUSD",
                token_address=wallet_balance.PUSD_TOKEN,
                spender_address=V2_NEG_RISK_EXCHANGE if neg_risk else V2_EXCHANGE,
                order_version=order_version,
                neg_risk=neg_risk,
            )
        return CollateralPreflightContext(
            token_symbol="USDC.e",
            token_address=wallet_balance.BRIDGED_USDC_TOKEN,
            spender_address=V1_NEG_RISK_EXCHANGE if neg_risk else V1_EXCHANGE,
            order_version=1,
            neg_risk=neg_risk,
        )

    def _fetch_wallet_collateral_allowance_for_preflight(
        self,
        client,
        intent: OrderIntent,
        response: Any,
    ) -> wallet_balance.WalletAllowance | None:
        if not _wallet_balance_preflight_fallback_enabled():
            return None

        owner_address = os.getenv("POLYMARKET_FUNDER_ADDRESS") or str(client.get_address())
        context = self._collateral_preflight_context(client, intent, response)
        try:
            return wallet_balance.fetch_cached_erc20_allowance(
                owner_address,
                context.spender_address,
                token_address=context.token_address,
                token_symbol=context.token_symbol,
                ttl_seconds=config.wallet_balance_ttl_seconds(),
            )
        except Exception as exc:
            logger.warning(
                "wallet_allowance_preflight_fallback_unavailable owner=%s spender=%s "
                "token=%s order_version=%s neg_risk=%s error=%s",
                _mask_address(owner_address),
                _mask_address(context.spender_address),
                context.token_symbol,
                context.order_version,
                context.neg_risk,
                exc,
            )
            return None

    def _ensure_sufficient_collateral(self, client, intent: OrderIntent) -> None:
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
            wallet_snapshot = (
                self._fetch_wallet_collateral_for_preflight(client, response)
                if balance == 0
                else None
            )
            if wallet_snapshot is None or wallet_snapshot.balance < required:
                raise InsufficientBalanceError(
                    f"insufficient Polymarket collateral balance: required={required} available={balance}"
                )
            logger.warning(
                "balance_preflight_wallet_fallback required_usd=%s clob_balance=%s "
                "wallet_balance=%s wallet_token=%s wallet_address=%s",
                required,
                balance,
                wallet_snapshot.balance,
                wallet_snapshot.token_symbol,
                _mask_address(wallet_snapshot.address),
            )
            balance = wallet_snapshot.balance
        if allowance is not None and allowance < required:
            wallet_allowance = self._fetch_wallet_collateral_allowance_for_preflight(
                client,
                intent,
                response,
            ) if allowance == 0 else None
            if wallet_allowance is None or wallet_allowance.allowance < required:
                raise InsufficientBalanceError(
                    f"insufficient Polymarket collateral allowance: required={required} available={allowance}"
                )
            logger.warning(
                "allowance_preflight_wallet_fallback required_usd=%s clob_allowance=%s "
                "wallet_allowance=%s wallet_token=%s owner=%s spender=%s",
                required,
                allowance,
                wallet_allowance.allowance,
                wallet_allowance.token_symbol,
                _mask_address(wallet_allowance.owner_address),
                _mask_address(wallet_allowance.spender_address),
            )
            allowance = wallet_allowance.allowance
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
