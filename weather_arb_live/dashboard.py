from __future__ import annotations

import argparse
import json
import os
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, wait
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, config, network, wallet_balance
from .dashboard_ui import DASHBOARD_HTML
from .live_fetcher import midpoint_from_book
from .order_placer import build_clob_client_kwargs


LOG_PATH = config.LOG_DIR / "live_bot.log"
LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"(?P<level>[A-Z]+) (?P<logger>\S+) (?P<message>.*)$"
)

REQUIRED_LIVE_CREDENTIALS = (
    "POLYMARKET_API_KEY",
    "POLYMARKET_API_SECRET",
    "POLYMARKET_API_PASSPHRASE",
    "POLYMARKET_PRIVATE_KEY",
)

OPTIONAL_RUNTIME_ENV = (
    "DRY_RUN",
    "POLL_INTERVAL_MINUTES",
    "OFFLINE_RETRY_SECONDS",
    "RECONCILE_ON_STARTUP",
    "MAX_POSITION_USD",
    "ENABLE_NO_SIDE",
    "LIVE_MARKET_LIMIT",
    "POLYMARKET_MARKET_WS_ENABLED",
    "POLYMARKET_USER_WS_ENABLED",
    "POLYMARKET_WS_BASE_URL",
    "POLYMARKET_WS_HEARTBEAT_SECONDS",
    "POLYMARKET_WS_RECONNECT_MIN_SECONDS",
    "POLYMARKET_WS_RECONNECT_MAX_SECONDS",
    "POLYMARKET_WS_MARKET_STALE_SECONDS",
    "POLYMARKET_WS_MARKET_MAX_TOKENS",
    "POLYMARKET_WS_MARKET_WARMUP_SECONDS",
    "SAFETY_RECONCILE_INTERVAL_MINUTES",
    "SAFETY_RECONCILE_MIN_INTERVAL_SECONDS",
    "POLYMARKET_WALLET_BALANCE_TTL_SECONDS",
    "POLYGON_RPC_URL",
    "POLYGON_RPC_FALLBACK_URLS",
    "WEATHER_ARB_DATA_DIR",
    "WEATHER_ARB_LOG_DIR",
    "POLYMARKET_RECONCILE_USER_ADDRESS",
    "POLYMARKET_FUNDER_ADDRESS",
    "POLYMARKET_PROXY_ADDRESS",
    "POLYMARKET_WALLET_ADDRESS",
    "POLYMARKET_CLOB_HOST",
    "POLYMARKET_CHAIN_ID",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_AUTH_WRITE_DOTENV",
    "POLYMARKET_TICK_SIZE",
    "POLY_BUILDER_CODE",
    "POLYMARKET_BUILDER_CODE",
)

MARK_FETCH_TIMEOUT_SECONDS = 2.0
MARK_FETCH_TOTAL_TIMEOUT_SECONDS = 5.0
MARK_FETCH_WORKERS = 8
MARK_FETCH_MAX_TOKENS = 100
ACCOUNT_FETCH_TOTAL_TIMEOUT_SECONDS = 5.0
PNL_HISTORY_MAX_POINTS = 10000
PNL_HISTORY_MIN_INTERVAL_SECONDS = 10.0
PNL_HISTORY_WRITE_LOCK = threading.Lock()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _account_payload(
    *,
    status: str,
    status_label: str,
    balance_usd: float | None = None,
    allowance_usd: float | None = None,
    error: str | None = None,
    updated_at: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    payload = {
        "status": status,
        "status_label": status_label,
        "balance_usd": balance_usd,
        "allowance_usd": allowance_usd,
        "error": error,
        "updated_at": updated_at,
    }
    payload.update(extra)
    return payload


def account_disabled_payload() -> dict[str, Any]:
    return _account_payload(status="disabled", status_label="Disabled")


def _parse_account_decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError(f"account balance response missing numeric {field}")
    try:
        amount = Decimal(str(value).strip())
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"account balance response has invalid {field}: {value!r}") from exc
    if not amount.is_finite():
        raise ValueError(f"account balance response has non-finite {field}: {value!r}")
    return amount


def _parse_account_balance_allowance(response: Any) -> tuple[Decimal, Decimal | None]:
    if not isinstance(response, dict):
        raise ValueError(f"account balance response must be an object, got {type(response).__name__}")

    balance = _parse_account_decimal(response.get("balance"), "balance")
    allowance: Decimal | None = None
    if response.get("allowance") not in (None, ""):
        allowance = _parse_account_decimal(response.get("allowance"), "allowance")
    elif isinstance(response.get("allowances"), dict) and response["allowances"]:
        allowance_values = [
            _parse_account_decimal(value, f"allowances[{key}]")
            for key, value in response["allowances"].items()
            if value not in (None, "")
        ]
        if allowance_values:
            allowance = min(allowance_values)
    return balance, allowance


def _decimal_to_float(value: Decimal | None) -> float | None:
    return None if value is None else float(value)


def _round_decimal_usd(value: Decimal | None) -> float | None:
    return round(_decimal_to_float(value), 2) if value is not None else None


def _mask_address(value: str | None) -> str | None:
    if not value:
        return None
    address = value.strip()
    if len(address) <= 10:
        return address
    return f"{address[:6]}...{address[-4:]}"


def _missing_live_credentials() -> list[str]:
    return [name for name in REQUIRED_LIVE_CREDENTIALS if not os.getenv(name)]


def _fetch_wallet_balance_payload(address: str | None) -> tuple[wallet_balance.WalletBalance | None, str | None]:
    if not address:
        return None, None
    try:
        return (
            wallet_balance.fetch_cached_collateral_balance(
                address,
                ttl_seconds=config.wallet_balance_ttl_seconds(),
            ),
            None,
        )
    except Exception as exc:
        return None, f"wallet balance unavailable: {exc}"


def _fetch_account_snapshot_once(runtime: dict[str, Any]) -> dict[str, Any]:
    from py_clob_client_v2 import ApiCreds, AssetType, BalanceAllowanceParams, BuilderConfig, ClobClient

    creds = ApiCreds(
        api_key=os.environ["POLYMARKET_API_KEY"],
        api_secret=os.environ["POLYMARKET_API_SECRET"],
        api_passphrase=os.environ["POLYMARKET_API_PASSPHRASE"],
    )
    funder = os.getenv("POLYMARKET_FUNDER_ADDRESS")
    kwargs = build_clob_client_kwargs(
        ClobClient,
        BuilderConfig,
        host=str(runtime.get("clob_host") or config.clob_host()).rstrip("/"),
        key=os.environ["POLYMARKET_PRIVATE_KEY"],
        creds=creds,
    )

    client = ClobClient(**kwargs)
    signer_address = str(client.get_address())
    params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
    client.update_balance_allowance(params)
    response = client.get_balance_allowance(params)
    clob_balance, clob_allowance = _parse_account_balance_allowance(response)
    wallet_address = funder or signer_address
    wallet_snapshot, wallet_error = _fetch_wallet_balance_payload(wallet_address)
    display_balance = clob_balance
    balance_source = "clob"
    warning = None
    if wallet_snapshot is not None and wallet_snapshot.balance > clob_balance:
        display_balance = wallet_snapshot.balance
        balance_source = "wallet_collateral"
        if clob_balance == 0:
            warning = f"CLOB balance endpoint reported 0; showing wallet {wallet_snapshot.token_symbol} balance"

    return _account_payload(
        status="ok",
        status_label="Connected",
        balance_usd=_round_decimal_usd(display_balance) or 0.0,
        allowance_usd=_round_decimal_usd(clob_allowance),
        updated_at=utc_now_iso(),
        balance_source=balance_source,
        clob_balance_usd=_round_decimal_usd(clob_balance) or 0.0,
        clob_allowance_usd=_round_decimal_usd(clob_allowance),
        wallet_balance_usd=_round_decimal_usd(wallet_snapshot.balance) if wallet_snapshot else None,
        wallet_token=wallet_snapshot.token_symbol if wallet_snapshot else None,
        wallet_address=_mask_address(wallet_address),
        wallet_rpc_url=wallet_snapshot.rpc_url if wallet_snapshot else None,
        signer_address=_mask_address(signer_address),
        funder_address=_mask_address(funder),
        signature_type=os.getenv("POLYMARKET_SIGNATURE_TYPE") or "0",
        warning=warning,
        wallet_error=wallet_error,
    )


def fetch_account_snapshot(
    runtime: dict[str, Any],
    *,
    total_timeout_seconds: float = ACCOUNT_FETCH_TOTAL_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    missing = _missing_live_credentials()
    if missing:
        return _account_payload(
            status="missing_credentials",
            status_label="Missing credentials",
            error=f"{len(missing)} required live credentials missing",
        )

    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="dashboard-account")
    future = executor.submit(_fetch_account_snapshot_once, runtime)
    done, not_done = wait({future}, timeout=total_timeout_seconds)
    if not_done:
        future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)
        return _account_payload(
            status="timeout",
            status_label="Timeout",
            error=f"account balance fetch timed out after {total_timeout_seconds:g}s",
        )

    executor.shutdown(wait=False, cancel_futures=True)
    try:
        return future.result()
    except Exception as exc:
        return _account_payload(
            status="unavailable",
            status_label="Unavailable",
            error=f"account balance unavailable: {exc}",
        )


def _position_side(row: dict[str, Any]) -> str:
    raw_side = str(row.get("side") or "").strip().upper()
    if raw_side in {"YES", "NO"}:
        return raw_side
    # Legacy dry-run rows were created before TradePlan persisted the side.
    # At that point the strategy only entered YES contracts.
    if _safe_float(row.get("forecast_prob")) is not None and _safe_float(row.get("edge")) is not None:
        return "YES"
    return ""


def _effective_position_usd(
    recorded_position_usd: float | None,
    *,
    dry_run: bool,
    max_position_usd: float | None,
) -> float | None:
    if recorded_position_usd is None:
        return None
    if dry_run and max_position_usd is not None:
        return min(recorded_position_usd, max_position_usd)
    return recorded_position_usd


def _effective_shares(
    recorded_shares: float | None,
    *,
    recorded_position_usd: float | None,
    effective_position_usd: float | None,
    entry_price: float | None,
) -> float | None:
    if effective_position_usd is None:
        return recorded_shares
    if (
        recorded_shares is not None
        and recorded_position_usd is not None
        and recorded_position_usd > 0
    ):
        return recorded_shares * (effective_position_usd / recorded_position_usd)
    if entry_price is not None and entry_price > 0:
        return effective_position_usd / entry_price
    return recorded_shares


def _first_float_from_mapping(mapping: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = _safe_float(mapping.get(key))
        if value is not None:
            return value
    return None


def _exchange_payload(row: dict[str, Any]) -> dict[str, Any]:
    reconciliation = row.get("reconciliation") if isinstance(row.get("reconciliation"), dict) else {}
    exchange = reconciliation.get("exchange") if isinstance(reconciliation.get("exchange"), dict) else {}
    if exchange:
        return exchange
    order_response = row.get("order_response") if isinstance(row.get("order_response"), dict) else {}
    exchange = order_response.get("exchange") if isinstance(order_response.get("exchange"), dict) else {}
    return exchange


def _current_price_from_row(row: dict[str, Any], mark_prices: dict[str, float]) -> tuple[float | None, str | None]:
    token_id = str(row.get("token_id") or "")
    if token_id in mark_prices:
        return mark_prices[token_id], "live_book"

    current_price = _first_float_from_mapping(
        row,
        ("current_price", "currentPrice", "cur_price", "curPrice", "last_price", "lastPrice"),
    )
    if current_price is not None:
        return current_price, "ledger"

    exchange = _exchange_payload(row)
    current_price = _first_float_from_mapping(
        exchange,
        ("current_price", "currentPrice", "cur_price", "curPrice", "last_price", "lastPrice"),
    )
    if current_price is not None:
        return current_price, "exchange"
    return None, None


def _stored_pnl_from_row(row: dict[str, Any]) -> float | None:
    value = _first_float_from_mapping(row, ("pnl_usd", "pnl", "cash_pnl", "cashPnl"))
    if value is not None:
        return value
    return _first_float_from_mapping(_exchange_payload(row), ("pnl_usd", "pnl", "cash_pnl", "cashPnl"))


def _position_pnl(
    row: dict[str, Any],
    *,
    shares: float | None,
    position_usd: float | None,
    current_price: float | None,
) -> tuple[float | None, float | None, float | None]:
    current_value_usd = None
    pnl_usd = None
    if current_price is not None and shares is not None:
        current_value_usd = shares * current_price
        if position_usd is not None:
            pnl_usd = current_value_usd - position_usd
    if pnl_usd is None:
        pnl_usd = _stored_pnl_from_row(row)
    pnl_pct = pnl_usd / position_usd if pnl_usd is not None and position_usd and position_usd > 0 else None
    return current_value_usd, pnl_usd, pnl_pct


def fetch_current_midpoints(
    token_ids: list[str],
    *,
    clob_host: str,
    timeout_seconds: float = MARK_FETCH_TIMEOUT_SECONDS,
    total_timeout_seconds: float = MARK_FETCH_TOTAL_TIMEOUT_SECONDS,
) -> tuple[dict[str, float], str | None]:
    unique_token_ids = list(dict.fromkeys(token_id for token_id in token_ids if token_id))[:MARK_FETCH_MAX_TOKENS]
    if not unique_token_ids:
        return {}, None

    host = clob_host.rstrip("/")

    def fetch_one(token_id: str) -> float | None:
        session = network.get_session()
        data = network.get_json_with_retries(
            session,
            f"{host}/book",
            params={"token_id": token_id},
            timeout=timeout_seconds,
            attempts=1,
        )
        return midpoint_from_book(data) if isinstance(data, dict) else None

    max_workers = min(MARK_FETCH_WORKERS, len(unique_token_ids))
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="dashboard-marks")
    futures = {executor.submit(fetch_one, token_id): token_id for token_id in unique_token_ids}
    done, not_done = wait(futures, timeout=total_timeout_seconds)

    marks: dict[str, float] = {}
    failed = len(not_done)
    for future in done:
        token_id = futures[future]
        try:
            price = future.result()
        except Exception:
            failed += 1
            continue
        if price is None:
            failed += 1
            continue
        marks[token_id] = price

    for future in not_done:
        future.cancel()
    executor.shutdown(wait=False, cancel_futures=True)

    error = None
    if failed:
        error = f"{failed} of {len(unique_token_ids)} mark prices unavailable"
    return marks, error


def _parse_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _file_status(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return {
            "path": str(path),
            "exists": False,
            "size_bytes": 0,
            "modified_at": None,
        }
    return {
        "path": str(path),
        "exists": True,
        "size_bytes": stat.st_size,
        "modified_at": datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
    }


def _read_json(path: Path) -> tuple[Any, str | None]:
    if not path.exists():
        return None, None
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle), None
    except Exception as exc:
        return None, str(exc)


def _nonnegative_int(value: Any) -> int:
    parsed = _safe_float(value)
    if parsed is None:
        return 0
    return max(0, int(parsed))


def _normalize_pnl_history_entry(entry: Any) -> dict[str, Any] | None:
    if not isinstance(entry, dict):
        return None
    timestamp = entry.get("timestamp") or entry.get("generated_at") or entry.get("time")
    parsed_timestamp = _parse_timestamp(timestamp)
    pnl_usd = _safe_float(entry.get("pnl_usd"))
    if parsed_timestamp is None or pnl_usd is None:
        return None
    position_usd = _safe_float(entry.get("position_usd"))
    return {
        "timestamp": parsed_timestamp.astimezone(timezone.utc).isoformat(),
        "pnl_usd": round(pnl_usd, 2),
        "position_usd": round(position_usd, 2) if position_usd is not None else 0.0,
        "position_count": _nonnegative_int(entry.get("position_count")),
        "pnl_count": _nonnegative_int(entry.get("pnl_count")),
        "mark_count": _nonnegative_int(entry.get("mark_count")),
        "source": str(entry.get("source") or "dashboard"),
    }


def _load_pnl_history(
    path: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None, dict[str, Any] | None]:
    history_path = path or config.PNL_HISTORY_PATH
    payload, error = _read_json(history_path)
    if payload is None:
        return [], error, None
    raw_entries = payload.get("points") if isinstance(payload, dict) else payload
    if not isinstance(raw_entries, list):
        return [], "pnl history file is not a JSON list", payload if isinstance(payload, dict) else None
    history = [
        normalized
        for entry in raw_entries
        if (normalized := _normalize_pnl_history_entry(entry)) is not None
    ]
    history.sort(key=lambda entry: _parse_timestamp(entry["timestamp"]) or datetime.min.replace(tzinfo=timezone.utc))
    return history[-PNL_HISTORY_MAX_POINTS:], error, payload if isinstance(payload, dict) else None


def read_pnl_history(path: Path | None = None) -> tuple[list[dict[str, Any]], str | None]:
    history, error, _envelope = _load_pnl_history(path)
    return history, error


def _same_pnl_snapshot_values(left: dict[str, Any], right: dict[str, Any]) -> bool:
    keys = ("pnl_usd", "position_usd", "position_count", "pnl_count", "mark_count", "source")
    return all(left.get(key) == right.get(key) for key in keys)


def record_pnl_history_snapshot(
    position_summary: dict[str, Any],
    *,
    generated_at: str,
    mark_count: int,
    path: Path | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    history_path = path or config.PNL_HISTORY_PATH
    with PNL_HISTORY_WRITE_LOCK:
        history, error, envelope = _load_pnl_history(history_path)
        if error:
            return history, error
        snapshot = _normalize_pnl_history_entry(
            {
                "timestamp": generated_at,
                "pnl_usd": position_summary.get("total_pnl_usd"),
                "position_usd": position_summary.get("total_position_usd"),
                "position_count": position_summary.get("total"),
                "pnl_count": position_summary.get("pnl_count"),
                "mark_count": mark_count,
                "source": "live_marks" if mark_count else "ledger",
            }
        )
        if snapshot is None:
            return history, error

        if snapshot["position_count"] == 0 and snapshot["pnl_count"] == 0:
            return history, error

        should_append = True
        if history:
            last = history[-1]
            last_time = _parse_timestamp(last.get("timestamp"))
            current_time = _parse_timestamp(snapshot.get("timestamp"))
            elapsed = (
                (current_time - last_time).total_seconds()
                if current_time is not None and last_time is not None
                else PNL_HISTORY_MIN_INTERVAL_SECONDS
            )
            should_append = elapsed >= PNL_HISTORY_MIN_INTERVAL_SECONDS or not _same_pnl_snapshot_values(last, snapshot)

        if should_append:
            history = [*history, snapshot][-PNL_HISTORY_MAX_POINTS:]
            try:
                history_path.parent.mkdir(parents=True, exist_ok=True)
                tmp_path = history_path.with_name(
                    f"{history_path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
                )
                payload: Any = history
                if envelope is not None:
                    payload = dict(envelope)
                    payload["points"] = history
                with tmp_path.open("w", encoding="utf-8") as handle:
                    json.dump(payload, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                tmp_path.replace(history_path)
                error = None
            except Exception as exc:
                return history, f"pnl history write failed: {exc}"

        return history, error


def tail_lines(path: Path, limit: int) -> tuple[list[str], str | None]:
    if limit <= 0 or not path.exists():
        return [], None
    try:
        with path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            remaining = handle.tell()
            chunks: list[bytes] = []
            newline_count = 0
            while remaining > 0 and newline_count <= limit:
                read_size = min(8192, remaining)
                remaining -= read_size
                handle.seek(remaining)
                chunk = handle.read(read_size)
                chunks.append(chunk)
                newline_count += chunk.count(b"\n")
        content = b"".join(reversed(chunks)).decode("utf-8", errors="replace")
        return content.splitlines()[-limit:], None
    except Exception as exc:
        return [], str(exc)


def parse_log_lines(lines: list[str]) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    levels: Counter[str] = Counter()
    last_cycle_start: str | None = None
    last_cycle_end: str | None = None
    last_enter: str | None = None

    for line in lines:
        match = LOG_PATTERN.match(line)
        if match:
            entry = match.groupdict()
            entry["raw"] = line
        else:
            entry = {
                "timestamp": "",
                "level": "",
                "logger": "",
                "message": line,
                "raw": line,
            }
        level = entry["level"]
        message = entry["message"]
        if level:
            levels[level] += 1
        if message.startswith("cycle_start"):
            last_cycle_start = entry["timestamp"]
        elif message.startswith("cycle_end"):
            last_cycle_end = entry["timestamp"]
        elif message.startswith("decision_enter"):
            last_enter = entry["timestamp"]
        entries.append(entry)

    return {
        "entries": entries,
        "level_counts": dict(levels),
        "last_cycle_start": last_cycle_start,
        "last_cycle_end": last_cycle_end,
        "last_enter": last_enter,
    }


def summarize_positions(
    positions: dict[str, Any],
    *,
    max_position_usd: float | None = None,
    mark_prices: dict[str, float] | None = None,
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    side_counts: Counter[str] = Counter()
    dry_run_count = 0
    live_count = 0
    unknown_posted = 0
    manual_review = 0
    total_position_usd = 0.0
    total_recorded_position_usd = 0.0
    total_pnl_usd = 0.0
    pnl_count = 0
    win_count = 0
    loss_count = 0
    flat_count = 0
    marks = mark_prices or {}

    for key, value in positions.items():
        if not isinstance(value, dict):
            continue
        dry_run = bool(value.get("dry_run"))
        dry_run_count += int(dry_run)
        live_count += int(not dry_run)

        side = _position_side(value)
        if side:
            side_counts[side] += 1

        order_response = value.get("order_response") if isinstance(value.get("order_response"), dict) else {}
        posted = order_response.get("posted")
        if posted == "unknown":
            unknown_posted += 1

        reconciliation = value.get("reconciliation") if isinstance(value.get("reconciliation"), dict) else {}
        requires_review = bool(reconciliation.get("requires_manual_review"))
        manual_review += int(requires_review)

        recorded_position_usd = _safe_float(value.get("position_usd"))
        entry_price = _safe_float(value.get("entry_price"))
        recorded_shares = _safe_float(value.get("shares"))
        position_usd = _effective_position_usd(
            recorded_position_usd,
            dry_run=dry_run,
            max_position_usd=max_position_usd,
        )
        shares = _effective_shares(
            recorded_shares,
            recorded_position_usd=recorded_position_usd,
            effective_position_usd=position_usd,
            entry_price=entry_price,
        )
        current_price, mark_source = _current_price_from_row(value, marks)
        current_value_usd, pnl_usd, pnl_pct = _position_pnl(
            value,
            shares=shares,
            position_usd=position_usd,
            current_price=current_price,
        )
        if position_usd is not None:
            total_position_usd += position_usd
        if recorded_position_usd is not None:
            total_recorded_position_usd += recorded_position_usd
        if pnl_usd is not None:
            total_pnl_usd += pnl_usd
            pnl_count += 1
            rounded_pnl_usd = round(pnl_usd, 2)
            if rounded_pnl_usd > 0:
                win_count += 1
            elif rounded_pnl_usd < 0:
                loss_count += 1
            else:
                flat_count += 1

        rows.append(
            {
                "market_id": str(value.get("market_id") or key),
                "token_id": str(value.get("token_id") or ""),
                "side": side,
                "question": str(value.get("question") or ""),
                "city": str(value.get("city") or ""),
                "target_date": value.get("target_date"),
                "market_price": _safe_float(value.get("market_price")),
                "entry_price": entry_price,
                "shares": shares,
                "recorded_shares": recorded_shares,
                "position_usd": position_usd,
                "recorded_position_usd": recorded_position_usd,
                "current_price": current_price,
                "current_value_usd": current_value_usd,
                "pnl_usd": pnl_usd,
                "pnl_pct": pnl_pct,
                "pnl_source": mark_source,
                "forecast_prob": _safe_float(value.get("forecast_prob")),
                "edge": _safe_float(value.get("edge")),
                "lead_days": value.get("lead_days"),
                "entry_time": value.get("entry_time"),
                "dry_run": dry_run,
                "posted": posted,
                "manual_review": requires_review,
                "reconciliation_status": reconciliation.get("status"),
            }
        )

    def sort_key(row: dict[str, Any]) -> datetime:
        return _parse_timestamp(row.get("entry_time")) or datetime.min.replace(tzinfo=timezone.utc)

    cumulative_pnl_usd = 0.0
    pnl_curve = []
    for row in sorted(rows, key=sort_key):
        pnl_usd = _safe_float(row.get("pnl_usd"))
        if pnl_usd is None:
            continue
        cumulative_pnl_usd += pnl_usd
        pnl_curve.append(
            {
                "entry_time": row.get("entry_time"),
                "market_id": row.get("market_id"),
                "question": row.get("question"),
                "side": row.get("side"),
                "pnl_usd": round(pnl_usd, 2),
                "cumulative_pnl_usd": round(cumulative_pnl_usd, 2),
            }
        )

    rows.sort(key=sort_key, reverse=True)
    win_rate_count = win_count + loss_count

    return {
        "total": len(rows),
        "dry_run": dry_run_count,
        "live": live_count,
        "yes_count": side_counts.get("YES", 0),
        "no_count": side_counts.get("NO", 0),
        "unknown_posted": unknown_posted,
        "manual_review": manual_review,
        "total_position_usd": round(total_position_usd, 2),
        "total_recorded_position_usd": round(total_recorded_position_usd, 2),
        "total_pnl_usd": round(total_pnl_usd, 2),
        "pnl_count": pnl_count,
        "win_count": win_count,
        "loss_count": loss_count,
        "flat_count": flat_count,
        "win_rate_count": win_rate_count,
        "win_rate": round(win_count / win_rate_count, 4) if win_rate_count else None,
        "pnl_curve": pnl_curve,
        "recent": rows,
    }


def runtime_payload() -> dict[str, Any]:
    runtime = config.load_runtime_config()
    return {
        "dry_run": runtime.dry_run,
        "poll_interval_seconds": runtime.poll_interval_seconds,
        "max_position_usd": runtime.max_position_usd,
        "clob_host": runtime.clob_host,
        "model_name": runtime.model_name,
        "model_variant": runtime.model_variant,
        "enable_no_side": runtime.enable_no_side,
        "offline_retry_seconds": runtime.offline_retry_seconds,
        "reconcile_on_startup": runtime.reconcile_on_startup,
        "live_market_limit": config.live_market_limit(),
        "polymarket_ws_base_url": runtime.polymarket_ws_base_url,
        "market_ws_enabled": runtime.market_ws_enabled,
        "user_ws_enabled": runtime.user_ws_enabled,
        "ws_heartbeat_seconds": runtime.ws_heartbeat_seconds,
        "ws_reconnect_min_seconds": runtime.ws_reconnect_min_seconds,
        "ws_reconnect_max_seconds": runtime.ws_reconnect_max_seconds,
        "ws_market_stale_seconds": runtime.ws_market_stale_seconds,
        "ws_market_max_tokens": runtime.ws_market_max_tokens,
        "ws_market_warmup_seconds": runtime.ws_market_warmup_seconds,
        "safety_reconcile_interval_seconds": runtime.safety_reconcile_interval_seconds,
        "safety_reconcile_min_interval_seconds": runtime.safety_reconcile_min_interval_seconds,
        "wallet_balance_ttl_seconds": runtime.wallet_balance_ttl_seconds,
        "data_dir": str(config.DATA_DIR),
        "log_dir": str(config.LOG_DIR),
    }


def environment_payload() -> dict[str, Any]:
    variables = []
    for name in REQUIRED_LIVE_CREDENTIALS:
        variables.append(
            {
                "name": name,
                "present": bool(os.getenv(name)),
                "required_for_live": True,
            }
        )
    for name in OPTIONAL_RUNTIME_ENV:
        variables.append(
            {
                "name": name,
                "present": bool(os.getenv(name)),
                "required_for_live": False,
            }
        )
    missing_required = [
        item["name"]
        for item in variables
        if item["required_for_live"] and not item["present"]
    ]
    return {
        "variables": variables,
        "missing_required": missing_required,
        "live_credentials_ready": not missing_required,
    }


def artifacts_payload() -> list[dict[str, Any]]:
    artifacts = (
        ("live_positions", config.POSITIONS_PATH),
        ("live_bot_log", LOG_PATH),
        ("weather_cache", config.WEATHER_CACHE_PATH),
        ("empirical_residuals", config.RESIDUALS_CACHE_PATH),
        ("sigma_cache", config.SIGMA_CACHE_PATH),
        ("calibration_table", config.CALIBRATION_PATH),
        ("pnl_history", config.PNL_HISTORY_PATH),
    )
    return [{"name": name, **_file_status(path)} for name, path in artifacts]


def health_payload(runtime: dict[str, Any], log_status: dict[str, Any]) -> dict[str, Any]:
    if not log_status["exists"] or not log_status["modified_at"]:
        return {
            "activity": "no_log",
            "activity_label": "No log",
            "detail": "logs/live_bot.log has not been created",
            "last_log_age_seconds": None,
        }

    modified_at = _parse_timestamp(log_status["modified_at"])
    if modified_at is None:
        return {
            "activity": "unknown",
            "activity_label": "Unknown",
            "detail": "Log timestamp could not be parsed",
            "last_log_age_seconds": None,
        }

    age_seconds = max(0.0, (datetime.now(timezone.utc) - modified_at).total_seconds())
    threshold = max(
        300,
        int(runtime["poll_interval_seconds"]) * 2 + 60,
        int(runtime["offline_retry_seconds"]) * 2 + 60,
    )
    is_recent = age_seconds <= threshold
    return {
        "activity": "recent" if is_recent else "stale",
        "activity_label": "Recent" if is_recent else "Stale",
        "detail": "Last log write is within the expected polling window" if is_recent else "Last log write is older than the polling window",
        "last_log_age_seconds": round(age_seconds, 1),
    }


def build_dashboard_state(
    *,
    log_limit: int = 160,
    include_live_marks: bool = False,
    mark_prices: dict[str, float] | None = None,
    include_account: bool = False,
    account_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    runtime = runtime_payload()
    environment = environment_payload()
    account = (
        account_snapshot
        if account_snapshot is not None
        else fetch_account_snapshot(runtime) if include_account else account_disabled_payload()
    )
    log_status = _file_status(LOG_PATH)

    positions_data, positions_error = _read_json(config.POSITIONS_PATH)
    if positions_data is None:
        positions = {}
    elif isinstance(positions_data, dict):
        positions = positions_data
    else:
        positions = {}
        positions_error = positions_error or "positions file is not a JSON object"

    live_mark_prices: dict[str, float] = {}
    mark_error = None
    if mark_prices is not None:
        live_mark_prices = mark_prices
    elif include_live_marks:
        token_ids = [
            str(row.get("token_id") or "")
            for row in positions.values()
            if isinstance(row, dict) and row.get("token_id")
        ]
        live_mark_prices, mark_error = fetch_current_midpoints(
            token_ids,
            clob_host=str(runtime.get("clob_host") or config.clob_host()),
        )

    log_lines, logs_error = tail_lines(LOG_PATH, log_limit)
    parsed_logs = parse_log_lines(log_lines)
    position_summary = summarize_positions(
        positions,
        max_position_usd=_safe_float(runtime.get("max_position_usd")),
        mark_prices=live_mark_prices,
    )
    recent_positions = position_summary.pop("recent")
    pnl_curve = position_summary.pop("pnl_curve")
    generated_at = utc_now_iso()
    pnl_history, pnl_history_error = record_pnl_history_snapshot(
        position_summary,
        generated_at=generated_at,
        mark_count=len(live_mark_prices),
    )
    artifacts = artifacts_payload()

    return {
        "generated_at": generated_at,
        "version": __version__,
        "runtime": runtime,
        "environment": environment,
        "account": account,
        "health": health_payload(runtime, log_status),
        "artifacts": artifacts,
        "positions": {
            "path": str(config.POSITIONS_PATH),
            "error": positions_error,
            "mark_error": mark_error,
            "mark_count": len(live_mark_prices),
            "summary": position_summary,
            "pnl_curve": pnl_curve,
            "pnl_history": pnl_history,
            "pnl_history_error": pnl_history_error,
            "recent": recent_positions,
        },
        "logs": {
            "path": str(LOG_PATH),
            "error": logs_error,
            **parsed_logs,
        },
    }


def _query_int(query: dict[str, list[str]], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = (query.get(name) or [default])[0]
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


class DashboardHandler(BaseHTTPRequestHandler):
    server_version = "WeatherArbDashboard/0.1"

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/", "/index.html"}:
            self._send_bytes(
                DASHBOARD_HTML.encode("utf-8"),
                content_type="text/html; charset=utf-8",
            )
            return
        if parsed.path == "/api/status":
            query = parse_qs(parsed.query)
            log_limit = _query_int(query, "log_lines", 160, 0, 500)
            marks = (query.get("marks") or ["1"])[0] != "0"
            account = (query.get("account") or ["1"])[0] != "0"
            self._send_json(
                build_dashboard_state(
                    log_limit=log_limit,
                    include_live_marks=marks,
                    include_account=account,
                )
            )
            return
        if parsed.path == "/healthz":
            self._send_json({"ok": True, "generated_at": utc_now_iso()})
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def _send_json(self, payload: dict[str, Any]) -> None:
        self._send_bytes(
            json.dumps(payload, indent=2, sort_keys=True).encode("utf-8"),
            content_type="application/json; charset=utf-8",
        )

    def _send_bytes(self, payload: bytes, *, content_type: str) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(payload)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Polymarket weather live bot dashboard")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host")
    parser.add_argument("--port", type=int, default=8765, help="Bind port")
    args = parser.parse_args(argv)

    server = ThreadingHTTPServer((args.host, args.port), DashboardHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"dashboard listening on {url}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
