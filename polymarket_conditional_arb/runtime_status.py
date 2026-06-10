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
    ) -> None:
        self.path = Path(path)
        self.cache_path = Path(cache_path)
        self.mode = mode
        self.heartbeat_seconds = max(0.1, float(heartbeat_seconds))
        self.host = socket.gethostname()
        self.pid = os.getpid()
        self.started_at_utc = utc_iso()
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
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
            self._payload.update(fields)
            self._payload["heartbeat_at_utc"] = utc_iso()
            self._write_locked()

    def heartbeat(self, **fields: Any) -> None:
        self.update(**fields)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._payload)

    def _heartbeat_loop(self) -> None:
        while not self._stop_event.wait(self.heartbeat_seconds):
            self.heartbeat()

    def _write_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(jsonable(self._payload), indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)


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
    detail = str(runtime_row.get("detail") or "no active runtime heartbeat")
    host = runtime_row.get("host") or "unknown"
    pid = runtime_row.get("pid") or "unknown"
    skip_count = runtime_row.get("last_cycle_skips", 0)

    lines = [
        "Paper Portfolio Status",
        f"Current: {live_status}",
        f"Last refreshed: {utc_iso(current_time)}",
        f"PID/Host: {pid} on {host}",
        f"Heartbeat: {_format_age(heartbeat_age)} ago; phase={runtime_row.get('phase') or 'unknown'}; {detail}",
        (
            "Warmup/cache: "
            f"cache_fetched={runtime_row.get('cache_fetched_at_utc') or 'never'} "
            f"age={_format_age(cache_age)} "
            f"events={runtime_row.get('events_fetched', 0)} "
            f"raw_markets={runtime_row.get('raw_markets', 0)} "
            f"tradable={runtime_row.get('tradable_markets', 0)} "
            f"tokens={runtime_row.get('tokens', 0)}"
        ),
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
    if show_log:
        lines.extend(["", "Status Log"])
        entries = _runtime_status_entries(runtime_row)
        if entries:
            lines.extend(f"- {entry}" for entry in entries)
        else:
            lines.append("- no status history")
    return "\n".join(lines)


def run_status_watch(
    *,
    render: Callable[[], str],
    refresh_seconds: float,
    output: Callable[[str], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    iterations: int | None = None,
) -> None:
    writer = output or sys.stdout.write
    count = 0
    while iterations is None or count < iterations:
        writer("\x1b[2J\x1b[H" + render())
        flush = getattr(writer, "flush", None)
        if callable(flush):
            flush()
        elif output is None:
            sys.stdout.flush()
        count += 1
        if iterations is not None and count >= iterations:
            return
        sleep(max(0.1, refresh_seconds))
