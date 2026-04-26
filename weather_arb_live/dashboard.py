from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import __version__, config
from .dashboard_ui import DASHBOARD_HTML


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
    "POLYMARKET_RECONCILE_USER_ADDRESS",
    "POLYMARKET_FUNDER_ADDRESS",
    "POLYMARKET_PROXY_ADDRESS",
    "POLYMARKET_WALLET_ADDRESS",
    "POLYMARKET_CLOB_HOST",
    "POLYMARKET_CHAIN_ID",
    "POLYMARKET_SIGNATURE_TYPE",
    "POLYMARKET_TICK_SIZE",
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_float(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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


def summarize_positions(positions: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    side_counts: Counter[str] = Counter()
    dry_run_count = 0
    live_count = 0
    unknown_posted = 0
    manual_review = 0
    total_position_usd = 0.0

    for key, value in positions.items():
        if not isinstance(value, dict):
            continue
        dry_run = bool(value.get("dry_run"))
        dry_run_count += int(dry_run)
        live_count += int(not dry_run)

        side = str(value.get("side") or "").upper()
        if side:
            side_counts[side] += 1

        order_response = value.get("order_response") if isinstance(value.get("order_response"), dict) else {}
        posted = order_response.get("posted")
        if posted == "unknown":
            unknown_posted += 1

        reconciliation = value.get("reconciliation") if isinstance(value.get("reconciliation"), dict) else {}
        requires_review = bool(reconciliation.get("requires_manual_review"))
        manual_review += int(requires_review)

        position_usd = _safe_float(value.get("position_usd"))
        if position_usd is not None:
            total_position_usd += position_usd

        rows.append(
            {
                "market_id": str(value.get("market_id") or key),
                "token_id": str(value.get("token_id") or ""),
                "side": side,
                "question": str(value.get("question") or ""),
                "city": str(value.get("city") or ""),
                "target_date": value.get("target_date"),
                "market_price": _safe_float(value.get("market_price")),
                "entry_price": _safe_float(value.get("entry_price")),
                "shares": _safe_float(value.get("shares")),
                "position_usd": position_usd,
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

    rows.sort(key=sort_key, reverse=True)

    return {
        "total": len(rows),
        "dry_run": dry_run_count,
        "live": live_count,
        "yes_count": side_counts.get("YES", 0),
        "no_count": side_counts.get("NO", 0),
        "unknown_posted": unknown_posted,
        "manual_review": manual_review,
        "total_position_usd": round(total_position_usd, 2),
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


def build_dashboard_state(*, log_limit: int = 160) -> dict[str, Any]:
    runtime = runtime_payload()
    environment = environment_payload()
    artifacts = artifacts_payload()
    log_status = next(item for item in artifacts if item["name"] == "live_bot_log")

    positions_data, positions_error = _read_json(config.POSITIONS_PATH)
    if positions_data is None:
        positions = {}
    elif isinstance(positions_data, dict):
        positions = positions_data
    else:
        positions = {}
        positions_error = positions_error or "positions file is not a JSON object"

    log_lines, logs_error = tail_lines(LOG_PATH, log_limit)
    parsed_logs = parse_log_lines(log_lines)
    position_summary = summarize_positions(positions)
    recent_positions = position_summary.pop("recent")

    return {
        "generated_at": utc_now_iso(),
        "version": __version__,
        "runtime": runtime,
        "environment": environment,
        "health": health_payload(runtime, log_status),
        "artifacts": artifacts,
        "positions": {
            "path": str(config.POSITIONS_PATH),
            "error": positions_error,
            "summary": position_summary,
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
            self._send_json(build_dashboard_state(log_limit=log_limit))
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
