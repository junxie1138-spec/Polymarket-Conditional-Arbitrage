from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .event_log import jsonable, utc_iso
from .portfolio_lock import PortfolioDataLock

SCHEMA_VERSION = 1
HEARTBEAT_INTERVAL_SECONDS = 5.0
STALE_HEARTBEAT_SECONDS = 15.0
WRITE_RETRY_ATTEMPTS = 3
WRITE_RETRY_BACKOFF_SECONDS = 0.05
ANSI_CLEAR_SCREEN = "\x1b[2J\x1b[H"

RUNTIME_PHASES = {"warmup", "online", "stopping"}
STATUS_STATES = {"WARMUP", "ONLINE", "DEAD"}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _seconds_since(value: Any, *, now: datetime | None = None) -> float | None:
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return max(0.0, ((now or _utc_now()).astimezone(timezone.utc) - parsed).total_seconds())


def _format_age(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes = seconds / 60.0
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{minutes / 60.0:.1f}h"


def _format_money(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        amount = 0.0
    return f"${amount:,.2f}"


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _float_value(value: Any) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _format_count(value: int) -> str:
    return f"{max(0, int(value)):,}"


def _format_rate(value: float | None) -> str:
    if value is None or value <= 0.0:
        return "n/a"
    return f"{value:,.1f} tokens/s"


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return dict(data) if isinstance(data, Mapping) else None


def _normalize_status(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    status = value.strip()
    if not status:
        return None
    normalized = status.upper()
    return normalized if normalized in STATUS_STATES else status


def _status_from_phase(phase: Any) -> str:
    if phase == "warmup":
        return "WARMUP"
    if phase == "online":
        return "ONLINE"
    return "DEAD"


def _runtime_status_entries(runtime: Mapping[str, Any]) -> list[str]:
    entries = runtime.get("statusEntries")
    if entries is None:
        entries = runtime.get("status_entries")
    if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
        return []
    return [status for entry in entries if (status := _normalize_status(entry)) is not None]


def _runtime_is_fresh(
    runtime: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> bool:
    if not runtime:
        return False
    if runtime.get("schema_version") != SCHEMA_VERSION:
        return False
    if _runtime_pid_dead(runtime):
        return False
    heartbeat_age = _seconds_since(runtime.get("heartbeat_at_utc"), now=now)
    return heartbeat_age is not None and heartbeat_age <= stale_seconds


class RuntimeStatusWriter:
    def __init__(
        self,
        path: str | Path,
        *,
        cache_path: str | Path,
        mode: str = "paper_portfolio_instance",
        heartbeat_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        write_retry_attempts: int = WRITE_RETRY_ATTEMPTS,
        write_retry_backoff_seconds: float = WRITE_RETRY_BACKOFF_SECONDS,
    ) -> None:
        self.path = Path(path)
        self.cache_path = Path(cache_path)
        self.mode = mode
        self.heartbeat_seconds = max(0.1, float(heartbeat_seconds))
        self.write_retry_attempts = max(1, int(write_retry_attempts))
        self.write_retry_backoff_seconds = max(0.0, float(write_retry_backoff_seconds))
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.started_at_utc = utc_iso()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._pending_write_failure_warning: dict[str, Any] | None = None
        self._payload = self._base_payload()

    def _base_payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "host": self.host,
            "pid": self.pid,
            "started_at_utc": self.started_at_utc,
            "heartbeat_at_utc": self.started_at_utc,
            "phase": "warmup",
            "status": "WARMUP",
            "detail": "starting",
            "mode": self.mode,
            "warmup_started_at_utc": None,
            "warmup_completed_at_utc": None,
            "book_seed_reason": None,
            "book_seed_started_at_utc": None,
            "book_seed_total_tokens": 0,
            "book_seed_completed_tokens": 0,
            "book_seed_remaining_tokens": 0,
            "book_seed_received_books": 0,
            "book_seed_failed_tokens": 0,
            "book_seed_elapsed_seconds": None,
            "book_seed_rate_tokens_per_second": None,
            "book_seed_eta_seconds": None,
            "book_seed_batch_number": 0,
            "book_seed_total_batches": 0,
            "book_seed_batch_start_token": 0,
            "book_seed_batch_end_token": 0,
            "book_seed_batch_status": None,
            "book_seed_batch_started_at_utc": None,
            "book_seed_failed_token_sample": [],
            "book_seed_failure_categories": {},
            "events_fetched": 0,
            "raw_markets": 0,
            "tradable_markets": 0,
            "tokens": 0,
            "cache_path": str(self.cache_path),
            "cache_fetched_at_utc": None,
            "last_cycle_started_at_utc": None,
            "last_cycle_completed_at_utc": None,
            "last_evaluation_reason": None,
            "last_error": None,
            "last_cycle_evaluated_markets": 0,
            "last_cycle_executions": 0,
            "last_cycle_skips": 0,
            "dirty_tokens_pending": 0,
            "dirty_full_universe_pending": False,
            "dirty_full_reconcile_active": False,
            "dirty_update_batches_pending": 0,
            "market_ws_connection_count": 0,
            "market_ws_reconnect_count": 0,
            "market_ws_error_count": 0,
            "market_ws_last_error": None,
            "market_ws_stale_token_batches": 0,
            "market_ws_stale_tokens": 0,
            "runtime_status_write_failures": 0,
            "last_runtime_status_write_error": None,
        }

    def start(self, *, phase: str = "warmup", detail: str = "starting") -> None:
        self.update(phase=phase, detail=detail, force=True)
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._heartbeat_loop,
            name="paper-portfolio-runtime-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def stop(self, *, detail: str = "stopping") -> None:
        self.update(phase="stopping", detail=detail, force=True)
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=self.heartbeat_seconds + 0.5)

    def update(self, *, force: bool = False, **fields: Any) -> None:
        _ = force
        with self._lock:
            heartbeat_at_utc = utc_iso()
            phase = fields.get("phase")
            if phase is not None and phase not in RUNTIME_PHASES:
                raise ValueError(f"unsupported runtime phase: {phase!r}")
            if "status" in fields:
                status = _normalize_status(fields["status"])
                if status is None:
                    raise ValueError("status must be a non-empty string")
                fields["status"] = status
            elif phase is not None:
                fields["status"] = _status_from_phase(phase)
            if phase == "warmup" and not self._payload.get("warmup_started_at_utc"):
                self._payload["warmup_started_at_utc"] = heartbeat_at_utc
            if (
                phase in {"online", "stopping"}
                and self._payload.get("warmup_started_at_utc")
                and not self._payload.get("warmup_completed_at_utc")
            ):
                self._payload["warmup_completed_at_utc"] = heartbeat_at_utc
            self._payload.update(fields)
            self._payload["heartbeat_at_utc"] = heartbeat_at_utc
            self._write_locked()

    def heartbeat(self, **fields: Any) -> None:
        self.update(**fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._payload)

    def consume_write_failure_warning(self) -> dict[str, Any] | None:
        with self._lock:
            warning = self._pending_write_failure_warning
            self._pending_write_failure_warning = None
            return dict(warning) if warning is not None else None

    def record_write_failure(self, exc: BaseException) -> dict[str, Any]:
        with self._lock:
            return self._record_write_failure_locked(exc)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_seconds):
            self.heartbeat()

    def _write_locked(self) -> None:
        last_exc: OSError | None = None
        for attempt in range(1, self.write_retry_attempts + 1):
            try:
                self._write_once_locked()
                return
            except OSError as exc:
                last_exc = exc
                if attempt >= self.write_retry_attempts:
                    break
                if self.write_retry_backoff_seconds > 0.0:
                    time.sleep(self.write_retry_backoff_seconds)
        assert last_exc is not None
        self._record_write_failure_locked(last_exc)

    def _write_once_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(jsonable(self._payload), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def _record_write_failure_locked(self, exc: BaseException) -> dict[str, Any]:
        failures = _int_value(self._payload.get("runtime_status_write_failures")) + 1
        error = f"{type(exc).__name__}: {exc}"
        self._payload["runtime_status_write_failures"] = failures
        self._payload["last_runtime_status_write_error"] = error
        warning = {"failures": failures, "error": error}
        self._pending_write_failure_warning = warning
        return dict(warning)


def load_runtime_status(path: str | Path) -> dict[str, Any] | None:
    return _read_json_object(Path(path))


def _runtime_pid_dead(runtime: Mapping[str, Any]) -> bool:
    host = str(runtime.get("host") or "")
    if host != socket.gethostname():
        return False
    try:
        pid = int(runtime.get("pid"))
    except (TypeError, ValueError):
        return True
    return pid <= 0 or not PortfolioDataLock._process_is_alive(pid)


def derive_runtime_state(
    runtime: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> str:
    if not _runtime_is_fresh(runtime, now=now, stale_seconds=stale_seconds):
        return "DEAD"
    assert runtime is not None
    status = _normalize_status(runtime.get("status"))
    if status in STATUS_STATES:
        return status
    entries = _runtime_status_entries(runtime)
    if entries and entries[-1] in STATUS_STATES:
        return entries[-1]
    return _status_from_phase(runtime.get("phase"))


def derive_live_status(
    runtime: Mapping[str, Any] | None,
    *,
    now: datetime | None = None,
    stale_seconds: float = STALE_HEARTBEAT_SECONDS,
) -> str:
    if not _runtime_is_fresh(runtime, now=now, stale_seconds=stale_seconds):
        return "DEAD"
    assert runtime is not None
    status = _normalize_status(runtime.get("status"))
    if status is not None:
        return status
    entries = _runtime_status_entries(runtime)
    if entries:
        return entries[-1]
    return _status_from_phase(runtime.get("phase"))


def read_runtime_and_portfolio_status(
    *,
    runtime_path: str | Path,
    portfolio_status: Callable[[], dict[str, Any]],
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    runtime = load_runtime_status(runtime_path)
    return runtime, portfolio_status()


DASHBOARD_WIDTH = 100
LEFT_CELL_WIDTH = 46
RIGHT_CELL_WIDTH = DASHBOARD_WIDTH - LEFT_CELL_WIDTH - 1
CELL_PADDING = 2
LABEL_WIDTH = 16
PROGRESS_BAR_WIDTH = 33


def _truncate(text: Any, width: int) -> str:
    rendered = str(text)
    if len(rendered) <= width:
        return rendered
    if width <= 3:
        return rendered[:width]
    return rendered[: width - 3] + "..."


def _cell(text: Any, width: int) -> str:
    content_width = max(0, width - CELL_PADDING)
    return f" {_truncate(text, content_width).ljust(content_width)} "


def _full_border() -> str:
    return "+" + "=" * DASHBOARD_WIDTH + "+"


def _split_border() -> str:
    return "+" + "=" * LEFT_CELL_WIDTH + "+" + "=" * RIGHT_CELL_WIDTH + "+"


def _full_row(text: Any) -> str:
    return "|" + _cell(text, DASHBOARD_WIDTH) + "|"


def _split_row(left: Any = "", right: Any = "") -> str:
    return "|" + _cell(left, LEFT_CELL_WIDTH) + "|" + _cell(right, RIGHT_CELL_WIDTH) + "|"


def _join_left_right(left: str, right: str, width: int) -> str:
    right = _truncate(right, width)
    if len(right) >= width:
        return right
    available_left = max(0, width - len(right) - 1)
    left = _truncate(left, available_left)
    gap = max(1, width - len(left) - len(right))
    return left + (" " * gap) + right


def _format_timestamp(value: Any) -> str:
    parsed = value.astimezone(timezone.utc) if isinstance(value, datetime) else parse_utc(value)
    if parsed is None:
        return "never"
    return parsed.strftime("%Y-%m-%d %H:%M:%SZ")


def _format_status_age(seconds: float | None) -> str:
    if seconds is None:
        return "n/a"
    return f"{_format_age(seconds)} ago"


def _format_status_health(
    *,
    runtime_row: Mapping[str, Any],
    live_status: str,
    heartbeat_age: float | None,
) -> str:
    if not runtime_row:
        return "missing"
    if live_status == "DEAD":
        if heartbeat_age is not None and heartbeat_age > STALE_HEARTBEAT_SECONDS:
            return "stale"
        return "dead"
    return "fresh"


def _format_dashboard_detail(value: Any) -> str:
    detail = str(value or "no active runtime heartbeat").strip()
    if detail.startswith("seeding REST ask books"):
        return "seeding ask books"
    if detail.startswith("REST ask books seeded"):
        return "ask books seeded"
    return detail or "no active runtime heartbeat"


def _format_token_rate(value: float | None) -> str:
    if value is None or value <= 0.0:
        return "n/a"
    return f"{value:,.1f} tok/s"


def _format_sample(values: Any) -> str:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return "none"
    sample = [str(value) for value in values if value not in (None, "")]
    return ", ".join(sample) if sample else "none"


def _format_category_counts(values: Any) -> str:
    if not isinstance(values, Mapping):
        return "none"
    parts: list[str] = []
    for category, count in sorted(values.items()):
        count_value = _int_value(count)
        if count_value <= 0:
            continue
        parts.append(f"{category}={_format_count(count_value)}")
    return ", ".join(parts) if parts else "none"


def _kv(label: str, value: Any) -> str:
    return f"{label.ljust(LABEL_WIDTH)}{value}"


def _append_split_section(rows: list[str], left_lines: Sequence[str], right_lines: Sequence[str]) -> None:
    rows.append(_split_border())
    for index in range(max(len(left_lines), len(right_lines))):
        left = left_lines[index] if index < len(left_lines) else ""
        right = right_lines[index] if index < len(right_lines) else ""
        rows.append(_split_row(left, right))


def _format_progress_bar(percent: float) -> str:
    clamped = min(100.0, max(0.0, percent))
    filled = min(PROGRESS_BAR_WIDTH, int((clamped / 100.0) * PROGRESS_BAR_WIDTH))
    return "#" * filled + "-" * (PROGRESS_BAR_WIDTH - filled)


def format_status_dashboard(
    *,
    runtime: Mapping[str, Any] | None,
    portfolio: Mapping[str, Any],
    now: datetime | None = None,
    show_log: bool = False,
) -> str:
    current_time = now or _utc_now()
    live_status = derive_live_status(runtime, now=current_time)
    runtime_row = runtime or {}
    heartbeat_age = _seconds_since(runtime_row.get("heartbeat_at_utc"), now=current_time)
    cache_age = _seconds_since(runtime_row.get("cache_fetched_at_utc"), now=current_time)
    costs = portfolio.get("costs") if isinstance(portfolio.get("costs"), Mapping) else {}
    unmatched = portfolio.get("unmatched_inventory")
    unmatched_text = "none" if not unmatched else f"{len(unmatched)} positions"
    host = runtime_row.get("host") or "unknown"
    pid = runtime_row.get("pid") or "unknown"
    health_status = _format_status_health(
        runtime_row=runtime_row,
        live_status=live_status,
        heartbeat_age=heartbeat_age,
    )
    header_width = DASHBOARD_WIDTH - CELL_PADDING
    status_badge = _truncate(str(live_status).upper(), 24)
    health_badge = _truncate(health_status.upper(), 14)
    top_right = f"{status_badge}   {health_badge}   PID {pid}"
    rows = [
        _full_border(),
        _full_row(_join_left_right("PAPER PORTFOLIO", top_right, header_width)),
        _full_row(_join_left_right(f"Updated {_format_timestamp(current_time)}", f"Host {host}", header_width)),
    ]

    failures = _int_value(runtime_row.get("runtime_status_write_failures"))
    detail = _format_dashboard_detail(runtime_row.get("detail"))
    health_lines = [
        "HEALTH",
        _kv("Heartbeat", _format_status_age(heartbeat_age)),
        _kv("Status", health_status),
        _kv("Phase", runtime_row.get("phase") or "unknown"),
        _kv("Detail", detail),
    ]
    ws_connection_count = _int_value(runtime_row.get("market_ws_connection_count"))
    ws_reconnect_count = _int_value(runtime_row.get("market_ws_reconnect_count"))
    ws_error_count = _int_value(runtime_row.get("market_ws_error_count"))
    ws_stale_batches = _int_value(runtime_row.get("market_ws_stale_token_batches"))
    ws_stale_tokens = _int_value(runtime_row.get("market_ws_stale_tokens"))
    ws_last_error = runtime_row.get("market_ws_last_error")
    if any((ws_connection_count, ws_reconnect_count, ws_error_count, ws_stale_batches, ws_last_error)):
        health_lines.extend(
            [
                "",
                _kv("WS conns", _format_count(ws_connection_count)),
                _kv("WS reconnects", _format_count(ws_reconnect_count)),
                _kv("WS errors", _format_count(ws_error_count)),
                _kv("WS stale", f"{_format_count(ws_stale_batches)} batches / {_format_count(ws_stale_tokens)} tokens"),
                _kv("WS error", ws_last_error or "none"),
            ]
        )
    if failures > 0:
        health_lines.extend(
            [
                _kv("Runtime writes", f"{_format_count(failures)} failures"),
                _kv("Write error", runtime_row.get("last_runtime_status_write_error") or "unknown"),
            ]
        )

    portfolio_lines = [
        "PORTFOLIO",
        _kv("Cash", _format_money(portfolio.get("cash"))),
        _kv("Equity", _format_money(portfolio.get("total_equity"))),
        _kv("Realized PnL", _format_money(portfolio.get("realized_pnl"))),
        _kv("Return", f"{float(portfolio.get('return_pct') or 0.0):.2f}%"),
        _kv("Trades", _format_count(_int_value(portfolio.get("trade_count")))),
        _kv("Win rate", f"{float(portfolio.get('win_rate_pct') or 0.0):.2f}%"),
    ]
    _append_split_section(rows, health_lines, portfolio_lines)
    if failures > 0:
        rows.append(_full_border())
        rows.append(
            _full_row(
                "Runtime writes: "
                f"failures={_format_count(failures)}; "
                f"last_error={runtime_row.get('last_runtime_status_write_error') or 'unknown'}"
            )
        )

    total_tokens = _int_value(runtime_row.get("book_seed_total_tokens"))
    completed_tokens = min(total_tokens, _int_value(runtime_row.get("book_seed_completed_tokens")))
    percent = (completed_tokens / total_tokens) * 100.0 if total_tokens > 0 else None
    remaining_tokens = _int_value(
        runtime_row.get("book_seed_remaining_tokens"),
        total_tokens - completed_tokens,
    )
    batch_number = _int_value(runtime_row.get("book_seed_batch_number"))
    total_batches = _int_value(runtime_row.get("book_seed_total_batches"))
    batch_start = _int_value(runtime_row.get("book_seed_batch_start_token"))
    batch_end = _int_value(runtime_row.get("book_seed_batch_end_token"))
    in_flight_age = _seconds_since(runtime_row.get("book_seed_batch_started_at_utc"), now=current_time)

    warmup_lines = [
        "WARMUP",
        _kv("Cache fetched", _format_timestamp(runtime_row.get("cache_fetched_at_utc"))),
        _kv("Cache age", _format_age(cache_age)),
        _kv("Events", _format_count(_int_value(runtime_row.get("events_fetched")))),
        _kv("Raw markets", _format_count(_int_value(runtime_row.get("raw_markets")))),
        _kv("Tradable", _format_count(_int_value(runtime_row.get("tradable_markets")))),
        _kv("Tokens", _format_count(_int_value(runtime_row.get("tokens")))),
    ]
    if percent is not None:
        warmup_lines.extend(
            [
                "",
                _kv("Seed progress", f"{_format_count(completed_tokens)} / {_format_count(total_tokens)}  ({percent:.1f}%)"),
                _kv("Remaining", _format_count(remaining_tokens)),
                _kv("Received", _format_count(_int_value(runtime_row.get("book_seed_received_books")))),
                _kv("Rate", _format_token_rate(_float_value(runtime_row.get("book_seed_rate_tokens_per_second")))),
                _kv("ETA", _format_age(_float_value(runtime_row.get("book_seed_eta_seconds")))),
            ]
        )
        if batch_number > 0 and total_batches > 0:
            warmup_lines.append(_kv("Batch", f"{_format_count(batch_number)} / {_format_count(total_batches)}"))
        if batch_start > 0 and batch_end > 0:
            warmup_lines.append(_kv("Batch tokens", f"{_format_count(batch_start)} - {_format_count(batch_end)}"))
        if str(runtime_row.get("book_seed_batch_status") or "") == "in_flight":
            warmup_lines.append(_kv("In flight", _format_age(in_flight_age)))
        warmup_lines.append(_kv("Failed", _format_count(_int_value(runtime_row.get("book_seed_failed_tokens")))))
        failure_sample = _format_sample(runtime_row.get("book_seed_failed_token_sample"))
        failure_categories = _format_category_counts(runtime_row.get("book_seed_failure_categories"))
        if failure_sample != "none":
            warmup_lines.append(_kv("Fail sample", failure_sample))
        if failure_categories != "none":
            warmup_lines.append(_kv("Fail types", failure_categories))

    dirty_tokens = _int_value(runtime_row.get("dirty_tokens_pending"))
    dirty_full_universe = bool(runtime_row.get("dirty_full_universe_pending"))
    dirty_full_reconcile_active = bool(runtime_row.get("dirty_full_reconcile_active"))
    if dirty_full_reconcile_active:
        dirty_backlog_text = "covered by active REST reconcile"
    elif dirty_full_universe:
        dirty_backlog_text = "full universe"
    elif dirty_tokens > 0:
        dirty_backlog_text = f"{_format_count(dirty_tokens)} tokens"
    else:
        dirty_backlog_text = "none"

    execution_lines = [
        "COSTS",
        _kv("Fees", _format_money(costs.get("fees_usd") if isinstance(costs, Mapping) else 0.0)),
        _kv("Slippage", _format_money(costs.get("slippage_usd") if isinstance(costs, Mapping) else 0.0)),
        _kv("Tax", _format_money(costs.get("tax_usd") if isinstance(costs, Mapping) else 0.0)),
        _kv("Merge", _format_money(costs.get("merge_usd") if isinstance(costs, Mapping) else 0.0)),
        "",
        "EXECUTION",
        _kv("Last exec", _format_timestamp(portfolio.get("last_execution_at_utc"))),
        _kv("Last cycle", runtime_row.get("last_evaluation_reason") or "n/a"),
        _kv("Dirty backlog", dirty_backlog_text),
        _kv("Completed", _format_timestamp(runtime_row.get("last_cycle_completed_at_utc"))),
        _kv("Evaluated", _format_count(_int_value(runtime_row.get("last_cycle_evaluated_markets")))),
        _kv("Executions", _format_count(_int_value(runtime_row.get("last_cycle_executions")))),
        _kv("Skips", _format_count(_int_value(runtime_row.get("last_cycle_skips")))),
        _kv("Unmatched", unmatched_text),
        _kv("Last error", runtime_row.get("last_error") or "none"),
    ]
    _append_split_section(rows, warmup_lines, execution_lines)

    if percent is not None:
        rows.append(_split_border())
        rows.append(_full_row(f"Progress [{_format_progress_bar(percent)}] {percent:.1f}%"))
    if show_log:
        rows.append(_full_border())
        rows.append(_full_row("STATUS LOG"))
        entries = _runtime_status_entries(runtime_row)
        if entries:
            rows.extend(_full_row(f"- {entry}") for entry in entries)
        else:
            rows.append(_full_row("- no status history"))
    rows.append(_full_border())
    return "\n".join(rows)


def _status_watch_is_live_terminal(output: Callable[[str], None] | None) -> bool:
    return output is None and sys.stdout.isatty()


def _write_status_watch_frame(
    *,
    frame: str,
    writer: Callable[[str], Any],
    live_terminal: bool,
    clear_screen: Callable[[str], Any] = os.system,
) -> None:
    if live_terminal and os.name == "nt":
        clear_screen("cls")
        writer(frame)
        return
    writer(ANSI_CLEAR_SCREEN + frame)


def run_status_watch(
    *,
    render: Callable[[], str],
    refresh_seconds: float,
    output: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    iterations: int | None = None,
    clear_screen: Callable[[str], Any] = os.system,
) -> None:
    writer = output or sys.stdout.write
    live_terminal = _status_watch_is_live_terminal(output)
    count = 0
    while iterations is None or count < iterations:
        _write_status_watch_frame(
            frame=render(),
            writer=writer,
            live_terminal=live_terminal,
            clear_screen=clear_screen,
        )
        flush = getattr(writer, "flush", None)
        if callable(flush):
            flush()
        elif output is None:
            sys.stdout.flush()
        count += 1
        if iterations is not None and count >= iterations:
            return
        sleep(max(0.1, refresh_seconds))
