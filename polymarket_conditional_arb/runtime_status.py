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


def _format_heartbeat_line(
    *,
    runtime_row: Mapping[str, Any],
    live_status: str,
    heartbeat_age: float | None,
) -> str:
    phase = runtime_row.get("phase") or "unknown"
    detail = str(runtime_row.get("detail") or "no active runtime heartbeat")
    if runtime_row and live_status == "DEAD" and heartbeat_age is not None and heartbeat_age > STALE_HEARTBEAT_SECONDS:
        return (
            f"Heartbeat: {_format_age(heartbeat_age)} ago (stale); "
            f"last-known phase={phase}; last-known detail={detail}"
        )
    return f"Heartbeat: {_format_age(heartbeat_age)} ago; phase={phase}; {detail}"


def _format_warmup_progress_line(
    *,
    runtime_row: Mapping[str, Any],
    current_time: datetime,
) -> str | None:
    phase = runtime_row.get("phase")
    detail = str(runtime_row.get("detail") or "")
    seeding_active = detail.startswith("seeding REST ask books") or detail.startswith("REST ask books seeded")
    if phase != "warmup" and not seeding_active:
        return None

    total_tokens = _int_value(runtime_row.get("book_seed_total_tokens"))
    completed_tokens = min(total_tokens, _int_value(runtime_row.get("book_seed_completed_tokens")))
    warmup_started_at = runtime_row.get("warmup_started_at_utc") or runtime_row.get("started_at_utc")
    warmup_elapsed = _seconds_since(warmup_started_at, now=current_time)
    parts: list[str] = []
    if warmup_elapsed is not None and phase == "warmup":
        parts.append(f"elapsed={_format_age(warmup_elapsed)}")
    if total_tokens > 0:
        remaining_tokens = _int_value(
            runtime_row.get("book_seed_remaining_tokens"),
            total_tokens - completed_tokens,
        )
        received_books = _int_value(runtime_row.get("book_seed_received_books"))
        failed_tokens = _int_value(runtime_row.get("book_seed_failed_tokens"))
        percent = (completed_tokens / total_tokens) * 100.0
        reason = runtime_row.get("book_seed_reason") or "n/a"
        eta_seconds = _float_value(runtime_row.get("book_seed_eta_seconds"))
        rate = _float_value(runtime_row.get("book_seed_rate_tokens_per_second"))
        parts.extend(
            [
                (
                    f"book_seed={reason} {_format_count(completed_tokens)}/"
                    f"{_format_count(total_tokens)} tokens ({percent:.1f}%)"
                ),
                f"remaining={_format_count(remaining_tokens)}",
                f"received_books={_format_count(received_books)}",
                f"failed={_format_count(failed_tokens)}",
                f"rate={_format_rate(rate)}",
                f"ETA={_format_age(eta_seconds)}",
            ]
        )
    if not parts:
        return None
    prefix = "Warmup progress" if phase == "warmup" else "Book seed progress"
    return f"{prefix}: " + "; ".join(parts)


def _format_runtime_status_write_failures_line(runtime_row: Mapping[str, Any]) -> str | None:
    failures = _int_value(runtime_row.get("runtime_status_write_failures"))
    if failures <= 0:
        return None
    error = runtime_row.get("last_runtime_status_write_error") or "unknown"
    return f"Runtime status writes: failures={_format_count(failures)}; last_error={error}"


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
    skip_count = runtime_row.get("last_cycle_skips", 0)
    heartbeat_line = _format_heartbeat_line(
        runtime_row=runtime_row,
        live_status=live_status,
        heartbeat_age=heartbeat_age,
    )
    warmup_progress_line = _format_warmup_progress_line(
        runtime_row=runtime_row,
        current_time=current_time,
    )
    runtime_status_write_failures_line = _format_runtime_status_write_failures_line(runtime_row)

    lines = [
        "Paper Portfolio Status",
        f"Current: {live_status}",
        f"Last refreshed: {utc_iso(current_time)}",
        f"PID/Host: {pid} on {host}",
        heartbeat_line,
        (
            "Warmup/cache: "
            f"cache_fetched={runtime_row.get('cache_fetched_at_utc') or 'never'} "
            f"age={_format_age(cache_age)} "
            f"events={runtime_row.get('events_fetched', 0)} "
            f"raw_markets={runtime_row.get('raw_markets', 0)} "
            f"tradable={runtime_row.get('tradable_markets', 0)} "
            f"tokens={runtime_row.get('tokens', 0)}"
        ),
    ]
    if warmup_progress_line is not None:
        lines.append(warmup_progress_line)
    if runtime_status_write_failures_line is not None:
        lines.append(runtime_status_write_failures_line)
    lines.extend(
        [
            f"Cache path: {runtime_row.get('cache_path') or 'unknown'}",
            (
                "Portfolio: "
                f"cash {_format_money(portfolio.get('cash'))}; "
                f"equity {_format_money(portfolio.get('total_equity'))}; "
                f"realized PnL {_format_money(portfolio.get('realized_pnl'))}; "
                f"return {float(portfolio.get('return_pct') or 0.0):.2f}%"
            ),
            (
                "Trades: "
                f"{portfolio.get('trade_count', 0)}; "
                f"win rate {float(portfolio.get('win_rate_pct') or 0.0):.2f}%; "
                f"last execution {portfolio.get('last_execution_at_utc') or 'never'}"
            ),
            (
                "Costs: "
                f"fees {_format_money(costs.get('fees_usd') if isinstance(costs, Mapping) else 0.0)}, "
                f"slippage {_format_money(costs.get('slippage_usd') if isinstance(costs, Mapping) else 0.0)}, "
                f"tax {_format_money(costs.get('tax_usd') if isinstance(costs, Mapping) else 0.0)}, "
                f"merge {_format_money(costs.get('merge_usd') if isinstance(costs, Mapping) else 0.0)}"
            ),
            f"Unmatched inventory: {unmatched_text}",
            (
                "Last cycle: "
                f"reason={runtime_row.get('last_evaluation_reason') or 'n/a'}; "
                f"completed={runtime_row.get('last_cycle_completed_at_utc') or 'never'}; "
                f"evaluated={runtime_row.get('last_cycle_evaluated_markets', 0)}; "
                f"executions={runtime_row.get('last_cycle_executions', 0)}; "
                f"skips={skip_count}"
            ),
            f"Last error: {runtime_row.get('last_error') or 'none'}",
        ]
    )
    if show_log:
        lines.extend(["", "Status Log"])
        entries = _runtime_status_entries(runtime_row)
        if entries:
            lines.extend(f"- {entry}" for entry in entries)
        else:
            lines.append("- no status history")
    return "\n".join(lines)


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
