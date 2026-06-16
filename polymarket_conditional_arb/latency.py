from __future__ import annotations

import asyncio
import json
import statistics
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import config, network
from .arb_models import BinaryMarket
from .event_log import jsonable, utc_iso
from .fetcher import GammaClobClient
from .market_data import market_subscribe_payload

SCHEMA_VERSION = 1
DEFAULT_REST_SAMPLES = 5
DEFAULT_WS_SAMPLES = 1
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_PAUSE_SECONDS = 0.25
DEFAULT_DISCOVERY_LIMIT = 20
DEFAULT_WS_FIRST_MESSAGE_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class LatencyProbeSettings:
    rest_samples: int = DEFAULT_REST_SAMPLES
    ws_samples: int = DEFAULT_WS_SAMPLES
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    pause_seconds: float = DEFAULT_PAUSE_SECONDS
    discovery_limit: int = DEFAULT_DISCOVERY_LIMIT
    include_websocket: bool = False
    ws_first_message_timeout_seconds: float = DEFAULT_WS_FIRST_MESSAGE_TIMEOUT_SECONDS


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _endpoint_path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _is_success(status_code: int | None, error: str | None) -> bool:
    return error is None and status_code is not None and 100 <= status_code < 400


def _percentile(values: Sequence[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((percentile / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def summarize_latency_samples(samples: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for sample in samples:
        endpoint_family = str(sample.get("endpoint_family") or "unknown")
        grouped.setdefault(endpoint_family, []).append(sample)

    summaries: dict[str, dict[str, Any]] = {}
    for endpoint_family, rows in sorted(grouped.items()):
        successful = [
            max(0.0, float(row.get("latency_ms") or 0.0))
            for row in rows
            if _is_success(
                int(row["status_code"]) if row.get("status_code") is not None else None,
                str(row.get("error")) if row.get("error") else None,
            )
        ]
        summaries[endpoint_family] = {
            "sample_count": len(rows),
            "success_count": len(successful),
            "error_count": len(rows) - len(successful),
            "p50_latency_ms": _percentile(successful, 50.0),
            "p95_latency_ms": _percentile(successful, 95.0),
            "p99_latency_ms": _percentile(successful, 99.0),
            "min_latency_ms": min(successful) if successful else None,
            "max_latency_ms": max(successful) if successful else None,
            "mean_latency_ms": statistics.fmean(successful) if successful else None,
        }
    return summaries


def _request_json_sample(
    session: Any,
    *,
    method: str,
    url: str,
    endpoint_family: str,
    sample_index: int,
    timeout_seconds: float,
    clock: Callable[[], float] = time.perf_counter,
    params: Mapping[str, Any] | None = None,
    json_body: Any = None,
    extra: Mapping[str, Any] | None = None,
) -> tuple[Any, dict[str, Any]]:
    started_at = _utc_now()
    start = clock()
    status_code: int | None = None
    error: str | None = None
    data: Any = None
    try:
        if method == "GET":
            response = session.get(url, params=dict(params or {}), timeout=timeout_seconds)
        elif method == "POST":
            response = session.post(url, json=json_body, timeout=timeout_seconds)
        else:
            raise ValueError(f"unsupported probe method: {method}")
        status_code = int(getattr(response, "status_code", 0) or 0)
        response.raise_for_status()
        data = response.json()
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
    completed_at = _utc_now()
    latency_seconds = max(0.0, clock() - start)
    sample = {
        "endpoint_family": endpoint_family,
        "endpoint": _endpoint_path(url),
        "method": method,
        "sample_index": sample_index,
        "started_at_utc": utc_iso(started_at),
        "completed_at_utc": utc_iso(completed_at),
        "latency_seconds": latency_seconds,
        "latency_ms": latency_seconds * 1000.0,
        "status_code": status_code,
        "error": error,
    }
    if extra:
        sample.update(dict(extra))
    return data, sample


def _discover_probe_market(events_payloads: Sequence[Any]) -> BinaryMarket | None:
    for payload in reversed(events_payloads):
        if not isinstance(payload, list):
            continue
        raw_markets = GammaClobClient.flatten_event_markets([row for row in payload if isinstance(row, dict)])
        markets = GammaClobClient.tradable_binary_markets(raw_markets)
        if markets:
            return markets[0]
    return None


def _recommendation_from_summaries(summaries: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    preferred_sources = ("clob_books", "clob_book", "market_ws_first_message", "gamma_events")
    source = next(
        (
            endpoint
            for endpoint in preferred_sources
            if summaries.get(endpoint, {}).get("p95_latency_ms") is not None
        ),
        None,
    )
    if source is None:
        return {
            "source": None,
            "latency_ms": None,
            "latency_jitter_ms": None,
            "env": [],
        }
    summary = summaries[source]
    p50 = float(summary.get("p50_latency_ms") or 0.0)
    p95 = float(summary.get("p95_latency_ms") or 0.0)
    latency_ms = max(0.0, p95)
    jitter_ms = max(0.0, p95 - p50)
    return {
        "source": source,
        "latency_ms": round(latency_ms, 3),
        "latency_jitter_ms": round(jitter_ms, 3),
        "env": [
            "COND_ARB_PAPER_LATENCY_MODE=fixed",
            f"COND_ARB_PAPER_LATENCY_MS={latency_ms:.3f}",
            f"COND_ARB_PAPER_LATENCY_JITTER_MS={jitter_ms:.3f}",
        ],
        "note": "Public endpoint RTT only; private order acceptance and matching latency are not observable here.",
    }


def measure_polymarket_rest_latency(
    *,
    scan_config: config.ScanConfig,
    settings: LatencyProbeSettings,
    session: Any | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    session = session or network.get_session()
    rest_samples = max(1, int(settings.rest_samples))
    timeout_seconds = max(0.1, float(settings.timeout_seconds))
    pause_seconds = max(0.0, float(settings.pause_seconds))
    discovery_limit = max(1, int(settings.discovery_limit))
    samples: list[dict[str, Any]] = []
    gamma_payloads: list[Any] = []
    gamma_params = {
        "closed": "false",
        "limit": discovery_limit,
        "order": "volume24hr",
        "ascending": "false",
    }

    for sample_index in range(1, rest_samples + 1):
        payload, sample = _request_json_sample(
            session,
            method="GET",
            url=config.GAMMA_EVENTS_URL,
            endpoint_family="gamma_events",
            sample_index=sample_index,
            timeout_seconds=timeout_seconds,
            clock=clock,
            params=gamma_params,
            extra={"limit": discovery_limit},
        )
        samples.append(sample)
        if sample["error"] is None:
            gamma_payloads.append(payload)
        if pause_seconds and sample_index < rest_samples:
            sleep(pause_seconds)

    probe_market = _discover_probe_market(gamma_payloads)
    token_ids = (
        [probe_market.yes_token_id, probe_market.no_token_id]
        if probe_market is not None
        else []
    )
    if token_ids:
        clob_books_url = f"{scan_config.clob_host.rstrip('/')}/books"
        for sample_index in range(1, rest_samples + 1):
            _payload, sample = _request_json_sample(
                session,
                method="POST",
                url=clob_books_url,
                endpoint_family="clob_books",
                sample_index=sample_index,
                timeout_seconds=timeout_seconds,
                clock=clock,
                json_body=[{"token_id": token_id} for token_id in token_ids],
                extra={
                    "token_count": len(token_ids),
                    "market_id": probe_market.market_id if probe_market is not None else None,
                },
            )
            samples.append(sample)
            if pause_seconds and sample_index < rest_samples:
                sleep(pause_seconds)

    summaries = summarize_latency_samples(samples)
    return {
        "schema_version": SCHEMA_VERSION,
        "measured_at_utc": utc_iso(),
        "clob_host": scan_config.clob_host,
        "gamma_events_url": config.GAMMA_EVENTS_URL,
        "market_ws_endpoint": scan_config.market_ws_endpoint,
        "settings": {
            "rest_samples": rest_samples,
            "timeout_seconds": timeout_seconds,
            "pause_seconds": pause_seconds,
            "discovery_limit": discovery_limit,
            "include_websocket": bool(settings.include_websocket),
            "ws_samples": max(0, int(settings.ws_samples)),
            "ws_first_message_timeout_seconds": max(
                0.1,
                float(settings.ws_first_message_timeout_seconds),
            ),
        },
        "probe_market": (
            {
                "market_id": probe_market.market_id,
                "question": probe_market.question,
                "yes_token_id": probe_market.yes_token_id,
                "no_token_id": probe_market.no_token_id,
            }
            if probe_market is not None
            else None
        ),
        "samples": samples,
        "summaries": summaries,
        "recommendation": _recommendation_from_summaries(summaries),
    }


async def _measure_market_websocket_latency(
    *,
    endpoint: str,
    token_ids: Sequence[str],
    samples: int,
    timeout_seconds: float,
    first_message_timeout_seconds: float,
    max_message_size_bytes: int,
    clock: Callable[[], float],
) -> list[dict[str, Any]]:
    if not token_ids:
        return []
    import websockets

    rows: list[dict[str, Any]] = []
    for sample_index in range(1, max(1, int(samples)) + 1):
        started_at = _utc_now()
        start = clock()
        connect_latency_seconds = 0.0
        first_message_latency_seconds = 0.0
        error: str | None = None
        try:
            async with websockets.connect(
                endpoint,
                ping_interval=None,
                open_timeout=timeout_seconds,
                close_timeout=timeout_seconds,
                max_size=max_message_size_bytes,
            ) as websocket:
                connect_latency_seconds = max(0.0, clock() - start)
                await websocket.send(json.dumps(market_subscribe_payload(token_ids)))
                deadline = clock() + first_message_timeout_seconds
                while True:
                    remaining = deadline - clock()
                    if remaining <= 0:
                        raise TimeoutError("timed out waiting for market WebSocket message")
                    message = await asyncio.wait_for(websocket.recv(), timeout=remaining)
                    if isinstance(message, bytes):
                        message = message.decode("utf-8", errors="replace")
                    if str(message).strip() not in {"", "PING", "PONG"}:
                        first_message_latency_seconds = max(0.0, clock() - start)
                        break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            first_message_latency_seconds = max(0.0, clock() - start)
        completed_at = _utc_now()
        rows.append(
            {
                "endpoint_family": "market_ws_connect",
                "endpoint": _endpoint_path(endpoint),
                "method": "WS",
                "sample_index": sample_index,
                "started_at_utc": utc_iso(started_at),
                "completed_at_utc": utc_iso(completed_at),
                "latency_seconds": connect_latency_seconds,
                "latency_ms": connect_latency_seconds * 1000.0,
                "status_code": 101 if error is None else None,
                "error": error,
                "token_count": len(token_ids),
            }
        )
        rows.append(
            {
                "endpoint_family": "market_ws_first_message",
                "endpoint": _endpoint_path(endpoint),
                "method": "WS",
                "sample_index": sample_index,
                "started_at_utc": utc_iso(started_at),
                "completed_at_utc": utc_iso(completed_at),
                "latency_seconds": first_message_latency_seconds,
                "latency_ms": first_message_latency_seconds * 1000.0,
                "status_code": 101 if error is None else None,
                "error": error,
                "token_count": len(token_ids),
            }
        )
    return rows


def measure_polymarket_latency(
    *,
    scan_config: config.ScanConfig,
    settings: LatencyProbeSettings,
    session: Any | None = None,
    clock: Callable[[], float] = time.perf_counter,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    report = measure_polymarket_rest_latency(
        scan_config=scan_config,
        settings=settings,
        session=session,
        clock=clock,
        sleep=sleep,
    )
    if settings.include_websocket:
        probe_market = report.get("probe_market") if isinstance(report.get("probe_market"), Mapping) else None
        token_ids = [
            str(probe_market.get("yes_token_id")),
            str(probe_market.get("no_token_id")),
        ] if probe_market else []
        websocket_samples = asyncio.run(
            _measure_market_websocket_latency(
                endpoint=scan_config.market_ws_endpoint,
                token_ids=token_ids,
                samples=max(1, int(settings.ws_samples)),
                timeout_seconds=max(0.1, float(settings.timeout_seconds)),
                first_message_timeout_seconds=max(0.1, float(settings.ws_first_message_timeout_seconds)),
                max_message_size_bytes=scan_config.market_ws_max_message_size_bytes,
                clock=clock,
            )
        )
        report["samples"].extend(websocket_samples)
        report["summaries"] = summarize_latency_samples(report["samples"])
        report["recommendation"] = _recommendation_from_summaries(report["summaries"])
    return report


def write_latency_report(path: str | Path, report: Mapping[str, Any]) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = output_path.with_name(output_path.name + ".tmp")
    tmp.write_text(json.dumps(jsonable(report), indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(output_path)


def _format_ms(value: Any) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.1f}ms"
    except (TypeError, ValueError):
        return "n/a"


def format_latency_report(report: Mapping[str, Any]) -> str:
    rows = ["Polymarket public latency probe", f"Measured at: {report.get('measured_at_utc')}"]
    probe_market = report.get("probe_market")
    if isinstance(probe_market, Mapping):
        rows.append(
            "Probe market: "
            f"{probe_market.get('market_id')} "
            f"({probe_market.get('yes_token_id')}, {probe_market.get('no_token_id')})"
        )
    else:
        rows.append("Probe market: unavailable; CLOB book probes were skipped")
    rows.append("")
    rows.append("Endpoint                    ok/total   p50      p95      max      errors")
    summaries = report.get("summaries") if isinstance(report.get("summaries"), Mapping) else {}
    for endpoint_family, summary in sorted(summaries.items()):
        if not isinstance(summary, Mapping):
            continue
        success_count = int(summary.get("success_count") or 0)
        sample_count = int(summary.get("sample_count") or 0)
        error_count = int(summary.get("error_count") or 0)
        rows.append(
            f"{str(endpoint_family)[:27].ljust(27)} "
            f"{success_count:>2}/{sample_count:<5} "
            f"{_format_ms(summary.get('p50_latency_ms')).rjust(8)} "
            f"{_format_ms(summary.get('p95_latency_ms')).rjust(8)} "
            f"{_format_ms(summary.get('max_latency_ms')).rjust(8)} "
            f"{error_count:>6}"
        )
    recommendation = report.get("recommendation") if isinstance(report.get("recommendation"), Mapping) else {}
    rows.append("")
    if recommendation.get("latency_ms") is not None:
        rows.append("Suggested simulation env:")
        for line in recommendation.get("env") or []:
            rows.append(f"  {line}")
        if recommendation.get("source"):
            rows.append(f"Source: p95 {recommendation.get('source')} RTT")
    else:
        rows.append("Suggested simulation env: unavailable; no successful latency samples")
    rows.append("Note: this is public endpoint RTT, not private order matching latency.")
    return "\n".join(rows)
