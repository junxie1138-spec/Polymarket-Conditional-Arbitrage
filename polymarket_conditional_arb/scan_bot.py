from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import time
from collections import defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import config
from .arb_models import BinaryMarket, OrderBookSide
from .event_log import utc_iso
from .fetcher import GammaClobClient
from .latency import (
    LatencyProbeSettings,
    format_latency_report,
    measure_polymarket_latency,
    write_latency_report,
)
from .market_data import MarketDataCache, MarketWebSocketManager, MarketWebSocketSettings
from .market_universe_cache import (
    MarketUniverseCacheRecord,
    load_market_universe_cache,
    write_market_universe_cache,
)
from .paper import (
    FillTimeBookEvidence,
    PaperPortfolio,
    PaperPortfolioDecision,
    PaperPortfolioLoadError,
    PaperPortfolioParams,
)
from .portfolio_lock import PortfolioDataLock, PortfolioLockError
from .runtime_status import (
    RuntimeStatusWriter,
    format_status_dashboard,
    read_runtime_and_portfolio_status,
    run_status_watch,
)

DIRTY_EVALUATION_DEBOUNCE_SECONDS = 0.1
RUNTIME_STATUS_WARNING_INTERVAL_SECONDS = 60.0


class ScannerStopped(RuntimeError):
    pass


@dataclass(frozen=True)
class ScannerRetryPolicy:
    initial_backoff_seconds: float = 1.0
    max_backoff_seconds: float = 30.0
    max_attempts: int | None = 3

    def backoff_seconds(self, failed_attempt: int) -> float:
        initial = max(0.0, self.initial_backoff_seconds)
        cap = max(initial, self.max_backoff_seconds)
        return min(cap, initial * (2 ** max(0, failed_attempt - 1)))


@dataclass(frozen=True)
class MarketUniverse:
    events_fetched: int
    raw_markets: int
    markets: tuple[BinaryMarket, ...]
    markets_by_token: Mapping[str, tuple[BinaryMarket, ...]]
    neg_risk_groups: Mapping[str, tuple[BinaryMarket, ...]]

    @property
    def token_ids(self) -> list[str]:
        return _token_ids_for_markets(list(self.markets))


@dataclass(frozen=True)
class _StartupUniverseSelection:
    universe: MarketUniverse
    coverage_status: str
    coverage_complete: bool


@dataclass(frozen=True)
class _DirtyTokenBatch:
    token_ids: set[str] | None
    evaluation_reason: str
    coalesced_updates: int


@dataclass(frozen=True)
class _BookChunkResult:
    books: Mapping[str, OrderBookSide]
    failed_tokens: int
    failed_token_sample: tuple[str, ...]
    failure_categories: Mapping[str, int]


@dataclass(frozen=True)
class _StartupLatencyCalibration:
    source: str
    p50_latency_ms: float
    p95_latency_ms: float
    latency_jitter_ms: float
    measured_at_utc: str | None
    report_path: str

    def event_payload(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "p50_latency_ms": self.p50_latency_ms,
            "p95_latency_ms": self.p95_latency_ms,
            "latency_ms": self.p95_latency_ms,
            "latency_jitter_ms": self.latency_jitter_ms,
            "measured_at_utc": self.measured_at_utc,
            "report_path": self.report_path,
        }

    def runtime_fields(self) -> dict[str, Any]:
        return {
            "latency_calibration_source": self.source,
            "latency_calibration_p50_ms": self.p50_latency_ms,
            "latency_calibration_p95_ms": self.p95_latency_ms,
            "latency_calibration_jitter_ms": self.latency_jitter_ms,
            "latency_calibration_measured_at_utc": self.measured_at_utc,
            "latency_report_path": self.report_path,
        }


StartupLatencyCalibrator = Callable[
    [Any],
    _StartupLatencyCalibration | None,
]


def _float_latency_value(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return None
    return max(0.0, parsed)


def _latency_summary(
    report: Mapping[str, Any],
    endpoint_family: str,
) -> Mapping[str, Any] | None:
    summaries = report.get("summaries") if isinstance(report.get("summaries"), Mapping) else {}
    summary = summaries.get(endpoint_family) if isinstance(summaries, Mapping) else None
    return summary if isinstance(summary, Mapping) else None


def _required_latency_values(
    report: Mapping[str, Any],
    endpoint_family: str,
) -> tuple[float, float]:
    summary = _latency_summary(report, endpoint_family)
    if summary is None:
        raise RuntimeError(f"startup latency calibration missing required {endpoint_family} summary")
    p50 = _float_latency_value(summary.get("p50_latency_ms"))
    p95 = _float_latency_value(summary.get("p95_latency_ms"))
    success_count = _progress_int(summary.get("success_count"))
    if success_count <= 0 or p50 is None or p95 is None:
        raise RuntimeError(f"startup latency calibration has no usable {endpoint_family} samples")
    return p50, p95


def _startup_latency_calibration_from_report(
    report: Mapping[str, Any],
    *,
    report_path: str,
) -> _StartupLatencyCalibration:
    _required_latency_values(report, "gamma_events")
    _required_latency_values(report, "clob_books")

    recommendation = report.get("recommendation") if isinstance(report.get("recommendation"), Mapping) else {}
    recommended_source = str(recommendation.get("source") or "clob_books")
    source_summary = _latency_summary(report, recommended_source)
    source = recommended_source if source_summary is not None else "clob_books"
    p50, p95 = _required_latency_values(report, source)
    measured_at_utc = report.get("measured_at_utc")
    return _StartupLatencyCalibration(
        source=source,
        p50_latency_ms=round(p50, 3),
        p95_latency_ms=round(p95, 3),
        latency_jitter_ms=round(max(0.0, p95 - p50), 3),
        measured_at_utc=str(measured_at_utc) if measured_at_utc else None,
        report_path=report_path,
    )


def _calibrate_startup_latency(scanner: Any) -> _StartupLatencyCalibration:
    settings = LatencyProbeSettings(include_websocket=bool(scanner.config.market_ws_enabled))
    scanner.logger.info(
        "startup_latency_calibration_start rest_samples=%s include_websocket=%s report_path=%s",
        settings.rest_samples,
        settings.include_websocket,
        scanner.config.latency_report_path,
    )
    try:
        report = measure_polymarket_latency(scan_config=scanner.config, settings=settings)
    except Exception as exc:
        if not settings.include_websocket:
            raise RuntimeError(f"startup latency calibration failed: {type(exc).__name__}: {exc}") from exc
        scanner.logger.warning(
            "startup_latency_calibration_websocket_probe_failed error=%r; retrying_rest_only",
            exc,
        )
        rest_settings = replace(settings, include_websocket=False)
        report = measure_polymarket_latency(scan_config=scanner.config, settings=rest_settings)

    write_latency_report(scanner.config.latency_report_path, report)
    return _startup_latency_calibration_from_report(
        report,
        report_path=str(scanner.config.latency_report_path),
    )


@dataclass
class _RestBookSeedBatchStallMonitor:
    reason: str
    stall_seconds: float
    logger: logging.Logger
    runtime_update: Callable[..., None]
    runtime_snapshot: Callable[[], Mapping[str, Any]]
    current_batch_number: int = 0
    total_batches: int = 0
    total_tokens: int = 0
    batch_start_token: int = 0
    batch_end_token: int = 0
    batch_started_at_utc: str | None = None
    batch_started_at_loop_time: float | None = None
    warning_message: str | None = None
    warned_batch_key: tuple[int, str] | None = None

    def note_progress(self, progress: Mapping[str, Any], *, loop_time: float) -> None:
        status = progress.get("current_batch_status")
        if status == "in_flight":
            self._clear_warning_if_current()
            self.current_batch_number = _progress_int(progress.get("current_batch_number"))
            self.total_batches = _progress_int(progress.get("total_batches"))
            self.total_tokens = _progress_int(progress.get("total_tokens"))
            self.batch_start_token = _progress_int(progress.get("current_batch_start_token"))
            self.batch_end_token = _progress_int(progress.get("current_batch_end_token"))
            started_at = progress.get("current_batch_started_at_utc")
            self.batch_started_at_utc = str(started_at) if started_at else None
            self.batch_started_at_loop_time = loop_time
            self.warning_message = None
            self.warned_batch_key = None
            return
        if status in {"complete", "failed"}:
            self.reset()

    def maybe_warn(self, *, loop_time: float) -> None:
        if self.batch_started_at_loop_time is None:
            return
        age_seconds = loop_time - self.batch_started_at_loop_time
        if age_seconds < self.stall_seconds:
            return
        warning_message = self._warning_text()
        if self.runtime_snapshot().get("last_error") != warning_message:
            self.runtime_update(last_error=warning_message)
        batch_key = self._current_batch_key()
        if batch_key == self.warned_batch_key:
            self.warning_message = warning_message
            return
        self.logger.warning(
            "rest_book_seed_batch_stalled reason=%s age_seconds=%.1f batch=%s/%s tokens=%s-%s total_tokens=%s",
            self.reason,
            age_seconds,
            self.current_batch_number,
            self.total_batches,
            self.batch_start_token,
            self.batch_end_token,
            self.total_tokens,
        )
        self.warning_message = warning_message
        self.warned_batch_key = batch_key

    def reset(self) -> None:
        self._clear_warning_if_current()
        self.current_batch_number = 0
        self.total_batches = 0
        self.total_tokens = 0
        self.batch_start_token = 0
        self.batch_end_token = 0
        self.batch_started_at_utc = None
        self.batch_started_at_loop_time = None
        self.warning_message = None
        self.warned_batch_key = None

    def _clear_warning_if_current(self) -> None:
        if self.warning_message and self.runtime_snapshot().get("last_error") == self.warning_message:
            self.runtime_update(last_error=None)

    def _current_batch_key(self) -> tuple[int, str]:
        return (self.current_batch_number, self.batch_started_at_utc or "")

    def _warning_text(self) -> str:
        return (
            f"{self.reason} stalled batch={self.current_batch_number}/{self.total_batches} "
            f"tokens={self.batch_start_token}-{self.batch_end_token} "
            f"threshold={self.stall_seconds:g}s"
        )


class _DirtyTokenAccumulator:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._tokens: set[str] = set()
        self._full_universe = False
        self._full_reconcile_active = False
        self._full_universe_ready = False
        self._evaluation_reason = "ws_dirty_update"
        self._coalesced_updates = 0

    @property
    def has_pending(self) -> bool:
        return bool(self._tokens) or (self._full_universe and self._full_universe_ready)

    def runtime_fields(self) -> dict[str, Any]:
        return {
            "dirty_tokens_pending": 0 if self._full_universe else len(self._tokens),
            "dirty_full_universe_pending": self._full_universe,
            "dirty_full_reconcile_active": self._full_reconcile_active,
            "dirty_update_batches_pending": self._coalesced_updates,
        }

    def mark(self, token_ids: set[str] | list[str] | tuple[str, ...], *, reason: str = "ws_dirty_update") -> bool:
        ids = {str(token_id) for token_id in token_ids if token_id}
        if not ids:
            return False
        if self._full_universe:
            self._tokens.clear()
            self._full_universe = True
            self._coalesced_updates = max(1, self._coalesced_updates)
            return False
        if not self._full_universe:
            self._tokens.update(ids)
            self._evaluation_reason = reason
        self._coalesced_updates += 1
        self._event.set()
        return True

    def mark_full_universe(self, *, reason: str = "rest_reconcile") -> None:
        self._tokens.clear()
        self._full_universe = True
        self._full_reconcile_active = False
        self._full_universe_ready = True
        self._evaluation_reason = reason
        self._coalesced_updates = 1
        self._event.set()

    def begin_full_reconcile(self, *, reason: str = "rest_reconcile") -> None:
        self._tokens.clear()
        self._full_universe = True
        self._full_reconcile_active = True
        self._full_universe_ready = False
        self._evaluation_reason = reason
        self._coalesced_updates = 1
        self._event.clear()

    def finish_full_reconcile(self) -> None:
        if not self._full_universe:
            self._full_universe = True
        self._full_reconcile_active = False
        self._full_universe_ready = True
        self._coalesced_updates = 1
        self._event.set()

    def fail_full_reconcile(self) -> None:
        if not self._full_universe:
            self._full_universe = True
        self._full_reconcile_active = False
        self._full_universe_ready = False
        self._coalesced_updates = 1
        self._event.clear()

    async def wait(self, *, timeout: float) -> _DirtyTokenBatch | None:
        if not self.has_pending:
            try:
                await asyncio.wait_for(self._event.wait(), timeout=timeout)
            except TimeoutError:
                return None
        await asyncio.sleep(DIRTY_EVALUATION_DEBOUNCE_SECONDS)
        return self.take_nowait()

    def take_nowait(self) -> _DirtyTokenBatch | None:
        if not self.has_pending:
            if not self._full_reconcile_active:
                self._event.clear()
            return None
        if self._full_universe:
            token_ids = None
        else:
            token_ids = set(self._tokens)
        batch = _DirtyTokenBatch(
            token_ids=token_ids,
            evaluation_reason=self._evaluation_reason,
            coalesced_updates=self._coalesced_updates,
        )
        self._tokens.clear()
        self._full_universe = False
        self._full_reconcile_active = False
        self._full_universe_ready = False
        self._evaluation_reason = "ws_dirty_update"
        self._coalesced_updates = 0
        self._event.clear()
        return batch


def setup_logging(scan_config: config.ScanConfig) -> logging.Logger:
    scan_config.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("polymarket_conditional_arb.portfolio")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(scan_config.scan_log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def _token_ids_for_markets(markets: list[BinaryMarket]) -> list[str]:
    token_ids: list[str] = []
    for market in markets:
        token_ids.extend([market.yes_token_id, market.no_token_id])
    return token_ids


def _progress_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return max(0, int(default))


def _cap_markets_by_token_limit(markets: list[BinaryMarket], token_limit: int | None) -> list[BinaryMarket]:
    if token_limit is None:
        return list(markets)
    capped: list[BinaryMarket] = []
    token_count = 0
    for market in markets:
        next_count = token_count + 2
        if next_count > token_limit:
            break
        capped.append(market)
        token_count = next_count
    return capped


def _merge_markets_by_priority(
    priority_markets: list[BinaryMarket],
    backfill_markets: list[BinaryMarket],
) -> list[BinaryMarket]:
    merged: list[BinaryMarket] = []
    seen_market_ids: set[str] = set()
    seen_token_ids: set[str] = set()
    for market in [*priority_markets, *backfill_markets]:
        market_tokens = {market.yes_token_id, market.no_token_id}
        if market.market_id in seen_market_ids or seen_token_ids.intersection(market_tokens):
            continue
        merged.append(market)
        seen_market_ids.add(market.market_id)
        seen_token_ids.update(market_tokens)
    return merged


def _build_market_universe(
    *,
    events_fetched: int,
    raw_markets: int,
    markets: list[BinaryMarket],
) -> MarketUniverse:
    markets_by_token: dict[str, list[BinaryMarket]] = defaultdict(list)
    neg_risk_groups: dict[str, list[BinaryMarket]] = defaultdict(list)
    for market in markets:
        markets_by_token[market.yes_token_id].append(market)
        markets_by_token[market.no_token_id].append(market)
        if market.neg_risk and market.event_id:
            neg_risk_groups[market.event_id].append(market)
    return MarketUniverse(
        events_fetched=events_fetched,
        raw_markets=raw_markets,
        markets=tuple(markets),
        markets_by_token={token: tuple(rows) for token, rows in markets_by_token.items()},
        neg_risk_groups={event_id: tuple(rows) for event_id, rows in neg_risk_groups.items()},
    )


class ConditionalArbScanner:
    def __init__(
        self,
        *,
        scan_config: config.ScanConfig | None = None,
        client: GammaClobClient | None = None,
        portfolio: PaperPortfolio | None = None,
        logger: logging.Logger | None = None,
        params: PaperPortfolioParams | None = None,
        retry_policy: ScannerRetryPolicy | None = None,
        startup_latency_calibrator: StartupLatencyCalibrator | None = _calibrate_startup_latency,
        **_legacy_kwargs: Any,
    ):
        self.config = scan_config or config.load_scan_config()
        self.client = client or GammaClobClient(clob_host=self.config.clob_host)
        self.params = params or PaperPortfolioParams.from_config(self.config)
        self.portfolio = portfolio or PaperPortfolio(
            self.config.paper_portfolio_instance_path,
            events_path=self.config.paper_portfolio_events_path,
            params=self.params,
        )
        self.logger = logger or logging.getLogger("polymarket_conditional_arb.portfolio")
        self.retry_policy = retry_policy or ScannerRetryPolicy()
        self._startup_latency_calibrator = startup_latency_calibrator
        self.startup_latency_calibration: _StartupLatencyCalibration | None = None
        self.running = True
        self.runtime = RuntimeStatusWriter(
            self.config.paper_portfolio_runtime_path,
            cache_path=self.config.market_universe_cache_path,
        )
        self._runtime_started = False
        self._last_runtime_status_warning_at: float | None = None

    def _should_retry(self, failed_attempt: int) -> bool:
        return self.running and (
            self.retry_policy.max_attempts is None
            or failed_attempt < self.retry_policy.max_attempts
        )

    def _sleep_retry_backoff(self, seconds: float) -> None:
        if seconds <= 0.0:
            return
        deadline = time.monotonic() + seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return
            time.sleep(min(remaining, 0.25))

    async def _sleep_async_retry_backoff(self, seconds: float) -> None:
        if seconds <= 0.0:
            await asyncio.sleep(0)
            return
        deadline = asyncio.get_running_loop().time() + seconds
        while self.running:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0.0:
                return
            await asyncio.sleep(min(remaining, 0.25))

    def _ensure_running(self) -> bool:
        if not self.running:
            raise ScannerStopped("scanner stopped")
        return True

    def _start_runtime(self, *, detail: str) -> None:
        self._runtime_started = True
        self.runtime.start(phase="warmup", detail=detail)
        if self.startup_latency_calibration is not None:
            self._runtime_update(**self.startup_latency_calibration.runtime_fields())

    def _stop_runtime(self, *, detail: str = "stopping") -> None:
        if not self._runtime_started:
            return
        self.runtime.stop(detail=detail)
        self._runtime_started = False

    def _runtime_update(self, **fields: Any) -> None:
        if not self._runtime_started:
            return
        try:
            self.runtime.update(**fields)
        except OSError as exc:
            self._log_runtime_status_write_failure(self.runtime.record_write_failure(exc))
            return
        warning = self.runtime.consume_write_failure_warning()
        if warning is not None:
            self._log_runtime_status_write_failure(warning)

    def _log_runtime_status_write_failure(self, warning: Mapping[str, Any]) -> None:
        now = time.monotonic()
        if (
            self._last_runtime_status_warning_at is not None
            and now - self._last_runtime_status_warning_at < RUNTIME_STATUS_WARNING_INTERVAL_SECONDS
        ):
            return
        self._last_runtime_status_warning_at = now
        self.logger.warning(
            "runtime_status_write_failed failures=%s error=%s",
            warning.get("failures"),
            warning.get("error"),
        )

    def _runtime_error(self, exc: Exception) -> None:
        self._runtime_update(last_error=f"{type(exc).__name__}: {exc}")

    def _apply_startup_latency_calibration(self) -> _StartupLatencyCalibration | None:
        if self._startup_latency_calibrator is None:
            return None
        try:
            calibration = self._startup_latency_calibrator(self)
        except Exception as exc:
            self.logger.error("startup_latency_calibration_failed error=%r", exc)
            raise
        if calibration is None:
            return None
        calibrated_simulation = replace(
            self.params.simulation,
            latency_ms=calibration.p95_latency_ms,
            latency_jitter_ms=calibration.latency_jitter_ms,
        )
        self.params = replace(self.params, simulation=calibrated_simulation)
        self.portfolio.params = self.params
        self.startup_latency_calibration = calibration
        fields = calibration.event_payload()
        self.logger.info(
            "startup_latency_calibrated source=%s p50_ms=%.3f p95_ms=%.3f jitter_ms=%.3f measured_at=%s report_path=%s",
            fields["source"],
            fields["p50_latency_ms"],
            fields["p95_latency_ms"],
            fields["latency_jitter_ms"],
            fields.get("measured_at_utc"),
            fields["report_path"],
        )
        self._runtime_update(**calibration.runtime_fields())
        return calibration

    def _log_rest_book_seed_failures(self, *, reason: str) -> None:
        if not self._runtime_started:
            return
        snapshot = self.runtime.snapshot()
        failed_tokens = _progress_int(snapshot.get("book_seed_failed_tokens"))
        failure_categories_raw = snapshot.get("book_seed_failure_categories")
        failure_categories = (
            dict(failure_categories_raw)
            if isinstance(failure_categories_raw, Mapping)
            else {}
        )
        if failed_tokens <= 0 and not failure_categories:
            return
        failed_token_sample_raw = snapshot.get("book_seed_failed_token_sample")
        failed_token_sample = (
            list(failed_token_sample_raw)
            if isinstance(failed_token_sample_raw, (list, tuple))
            else []
        )
        self.logger.warning(
            "rest_book_seed_failures reason=%s failed_tokens=%s failed_token_sample=%s failure_categories=%s",
            reason,
            failed_tokens,
            failed_token_sample,
            failure_categories,
        )

    def _book_seed_progress_callback(
        self,
        *,
        reason: str,
        total_tokens: int,
    ) -> Callable[[Mapping[str, Any]], None]:
        started_at = datetime.now(timezone.utc)
        started_at_utc = utc_iso(started_at)
        total = _progress_int(total_tokens)

        def update(progress: Mapping[str, Any]) -> None:
            progress_total = _progress_int(progress.get("total_tokens"), total)
            completed = min(progress_total, _progress_int(progress.get("completed_tokens")))
            remaining = _progress_int(progress.get("remaining_tokens"), progress_total - completed)
            received_books = _progress_int(progress.get("received_books"))
            failed_tokens = _progress_int(progress.get("failed_tokens"))
            failed_token_sample_raw = progress.get("failed_token_sample")
            failed_token_sample = (
                [str(token_id) for token_id in failed_token_sample_raw if token_id][:5]
                if isinstance(failed_token_sample_raw, (list, tuple))
                else []
            )
            failure_categories_raw = progress.get("failure_categories")
            failure_categories = (
                {
                    str(category): _progress_int(count)
                    for category, count in failure_categories_raw.items()
                    if category and _progress_int(count) > 0
                }
                if isinstance(failure_categories_raw, Mapping)
                else {}
            )
            current_batch_status = progress.get("current_batch_status")
            current_batch_started_at_utc = progress.get("current_batch_started_at_utc")
            elapsed_seconds = max(0.0, (datetime.now(timezone.utc) - started_at).total_seconds())
            rate = completed / elapsed_seconds if completed > 0 and elapsed_seconds > 0 else None
            eta = remaining / rate if rate and remaining > 0 else (0.0 if remaining == 0 else None)
            self._runtime_update(
                detail=f"seeding REST ask books: {reason} ({completed}/{progress_total} tokens)",
                book_seed_reason=reason,
                book_seed_started_at_utc=started_at_utc,
                book_seed_total_tokens=progress_total,
                book_seed_completed_tokens=completed,
                book_seed_remaining_tokens=remaining,
                book_seed_received_books=received_books,
                book_seed_failed_tokens=failed_tokens,
                book_seed_elapsed_seconds=elapsed_seconds,
                book_seed_rate_tokens_per_second=rate,
                book_seed_eta_seconds=eta,
                book_seed_batch_number=_progress_int(progress.get("current_batch_number")),
                book_seed_total_batches=_progress_int(progress.get("total_batches")),
                book_seed_batch_start_token=_progress_int(progress.get("current_batch_start_token")),
                book_seed_batch_end_token=_progress_int(progress.get("current_batch_end_token")),
                book_seed_batch_status=str(current_batch_status) if current_batch_status is not None else None,
                book_seed_batch_started_at_utc=(
                    str(current_batch_started_at_utc) if current_batch_started_at_utc is not None else None
                ),
                book_seed_failed_token_sample=failed_token_sample,
                book_seed_failure_categories=failure_categories,
            )

        update(
            {
                "total_tokens": total,
                "completed_tokens": 0,
                "remaining_tokens": total,
                "received_books": 0,
                "failed_tokens": 0,
            }
        )
        return update

    def _book_seed_token_chunks(self, token_ids: list[str]) -> list[list[str]]:
        unique_token_ids = list(dict.fromkeys(str(token_id) for token_id in token_ids if token_id))
        chunk_limit = max(1, int(getattr(self.client, "batch_book_limit", config.CLOB_BATCH_BOOK_LIMIT)))
        return [unique_token_ids[index : index + chunk_limit] for index in range(0, len(unique_token_ids), chunk_limit)]

    def _fetch_ask_books_chunk(self, token_ids: list[str]) -> _BookChunkResult:
        captured_progress: dict[str, Any] = {}

        def capture(progress: Mapping[str, Any]) -> None:
            captured_progress.update(dict(progress))

        books = self.client.fetch_ask_books(token_ids, on_progress=capture)
        failed_tokens = _progress_int(
            captured_progress.get("failed_tokens"),
            max(0, len(token_ids) - len(books)),
        )
        failed_token_sample_raw = captured_progress.get("failed_token_sample")
        failed_token_sample = (
            tuple(str(token_id) for token_id in failed_token_sample_raw if token_id)
            if isinstance(failed_token_sample_raw, (list, tuple))
            else ()
        )
        failure_categories_raw = captured_progress.get("failure_categories")
        failure_categories = (
            {
                str(category): _progress_int(count)
                for category, count in failure_categories_raw.items()
                if category and _progress_int(count) > 0
            }
            if isinstance(failure_categories_raw, Mapping)
            else {}
        )
        return _BookChunkResult(
            books=books,
            failed_tokens=failed_tokens,
            failed_token_sample=failed_token_sample,
            failure_categories=failure_categories,
        )

    @staticmethod
    def _merge_failure_categories(
        target: dict[str, int],
        source: Mapping[str, int],
    ) -> None:
        for category, count in source.items():
            normalized = str(category)
            amount = _progress_int(count)
            if normalized and amount > 0:
                target[normalized] = target.get(normalized, 0) + amount

    def _seed_rest_books_incrementally_sync(
        self,
        cache: MarketDataCache,
        token_ids: list[str],
        *,
        reason: str,
        on_chunk_seeded: Callable[[set[str]], Any] | None = None,
    ) -> set[str]:
        chunks = self._book_seed_token_chunks(token_ids)
        if not chunks:
            return set()

        total_tokens = sum(len(chunk) for chunk in chunks)
        total_batches = len(chunks)
        progress = self._book_seed_progress_callback(reason=reason, total_tokens=total_tokens)
        completed_tokens = 0
        received_books = 0
        failed_tokens = 0
        failed_token_sample: list[str] = []
        failure_categories: dict[str, int] = {}
        all_updated: set[str] = set()

        for batch_number, chunk in enumerate(chunks, start=1):
            self._ensure_running()
            batch_start_token = completed_tokens + 1
            batch_end_token = completed_tokens + len(chunk)
            batch_started_at_utc = utc_iso()
            progress(
                {
                    "total_tokens": total_tokens,
                    "completed_tokens": completed_tokens,
                    "remaining_tokens": total_tokens - completed_tokens,
                    "received_books": received_books,
                    "failed_tokens": failed_tokens,
                    "current_batch_number": batch_number,
                    "total_batches": total_batches,
                    "current_batch_start_token": batch_start_token,
                    "current_batch_end_token": batch_end_token,
                    "current_batch_status": "in_flight",
                    "current_batch_started_at_utc": batch_started_at_utc,
                    "failed_token_sample": failed_token_sample,
                    "failure_categories": failure_categories,
                }
            )
            try:
                chunk_result = self._run_with_retries(
                    "rest_book_fetch",
                    lambda chunk=chunk: self._fetch_ask_books_chunk(chunk),
                    summary=lambda result: {"reason": reason, "tokens": len(result.books)},
                )
            except Exception as exc:
                if isinstance(exc, ScannerStopped) or not self.running:
                    raise
                completed_tokens += len(chunk)
                failed_tokens += len(chunk)
                category = f"chunk:{type(exc).__name__}"
                failure_categories[category] = failure_categories.get(category, 0) + len(chunk)
                for token_id in chunk:
                    if len(failed_token_sample) < 5:
                        failed_token_sample.append(str(token_id))
                self.logger.warning(
                    "rest_book_seed_chunk_failed reason=%s batch=%s tokens=%s error=%r",
                    reason,
                    batch_number,
                    len(chunk),
                    exc,
                )
                progress(
                    {
                        "total_tokens": total_tokens,
                        "completed_tokens": completed_tokens,
                        "remaining_tokens": total_tokens - completed_tokens,
                        "received_books": received_books,
                        "failed_tokens": failed_tokens,
                        "current_batch_number": batch_number,
                        "total_batches": total_batches,
                        "current_batch_start_token": batch_start_token,
                        "current_batch_end_token": batch_end_token,
                        "current_batch_status": "failed",
                        "current_batch_started_at_utc": batch_started_at_utc,
                        "failed_token_sample": failed_token_sample,
                        "failure_categories": failure_categories,
                    }
                )
                continue

            completed_tokens += len(chunk)
            received_books += len(chunk_result.books)
            failed_tokens += chunk_result.failed_tokens
            for token_id in chunk_result.failed_token_sample:
                if len(failed_token_sample) < 5:
                    failed_token_sample.append(str(token_id))
            self._merge_failure_categories(failure_categories, chunk_result.failure_categories)
            updated = cache.seed_ask_books(chunk_result.books)
            all_updated.update(updated)
            progress(
                {
                    "total_tokens": total_tokens,
                    "completed_tokens": completed_tokens,
                    "remaining_tokens": total_tokens - completed_tokens,
                    "received_books": received_books,
                    "failed_tokens": failed_tokens,
                    "current_batch_number": batch_number,
                    "total_batches": total_batches,
                    "current_batch_start_token": batch_start_token,
                    "current_batch_end_token": batch_end_token,
                    "current_batch_status": "complete",
                    "current_batch_started_at_utc": batch_started_at_utc,
                    "failed_token_sample": failed_token_sample,
                    "failure_categories": failure_categories,
                }
            )
            if updated and on_chunk_seeded is not None:
                self._ensure_running()
                on_chunk_seeded(set(updated))

        self._ensure_running()
        self._runtime_update(
            detail=f"REST ask books seeded: {reason}",
            book_seed_completed_tokens=total_tokens,
            book_seed_remaining_tokens=0,
            book_seed_received_books=received_books,
            book_seed_failed_tokens=failed_tokens,
            book_seed_eta_seconds=0.0,
        )
        self._log_rest_book_seed_failures(reason=reason)
        self.logger.info("rest_books_seeded reason=%s tokens=%s", reason, len(all_updated))
        if total_tokens > 0 and not all_updated:
            raise RuntimeError(f"failed to seed any REST ask books for {reason}")
        return all_updated

    async def _seed_rest_books_incrementally_async(
        self,
        cache: MarketDataCache,
        token_ids: list[str],
        *,
        reason: str,
        on_chunk_seeded: Callable[[set[str]], Any] | None = None,
        on_progress_update: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> set[str]:
        chunks = self._book_seed_token_chunks(token_ids)
        if not chunks:
            return set()

        total_tokens = sum(len(chunk) for chunk in chunks)
        total_batches = len(chunks)
        progress = self._book_seed_progress_callback(reason=reason, total_tokens=total_tokens)
        completed_tokens = 0
        received_books = 0
        failed_tokens = 0
        failed_token_sample: list[str] = []
        failure_categories: dict[str, int] = {}
        all_updated: set[str] = set()

        for batch_number, chunk in enumerate(chunks, start=1):
            self._ensure_running()
            batch_start_token = completed_tokens + 1
            batch_end_token = completed_tokens + len(chunk)
            batch_started_at_utc = utc_iso()
            progress_payload = {
                "total_tokens": total_tokens,
                "completed_tokens": completed_tokens,
                "remaining_tokens": total_tokens - completed_tokens,
                "received_books": received_books,
                "failed_tokens": failed_tokens,
                "current_batch_number": batch_number,
                "total_batches": total_batches,
                "current_batch_start_token": batch_start_token,
                "current_batch_end_token": batch_end_token,
                "current_batch_status": "in_flight",
                "current_batch_started_at_utc": batch_started_at_utc,
                "failed_token_sample": failed_token_sample,
                "failure_categories": failure_categories,
            }
            progress(progress_payload)
            if on_progress_update is not None:
                on_progress_update(dict(progress_payload))
            try:
                chunk_result = await self._run_async_with_retries(
                    "rest_book_seed",
                    lambda chunk=chunk: asyncio.to_thread(self._fetch_ask_books_chunk, chunk),
                    summary=lambda result: {"reason": reason, "tokens": len(result.books)},
                )
            except Exception as exc:
                if isinstance(exc, ScannerStopped) or not self.running:
                    raise
                completed_tokens += len(chunk)
                failed_tokens += len(chunk)
                category = f"chunk:{type(exc).__name__}"
                failure_categories[category] = failure_categories.get(category, 0) + len(chunk)
                for token_id in chunk:
                    if len(failed_token_sample) < 5:
                        failed_token_sample.append(str(token_id))
                self.logger.warning(
                    "rest_book_seed_chunk_failed reason=%s batch=%s tokens=%s error=%r",
                    reason,
                    batch_number,
                    len(chunk),
                    exc,
                )
                progress_payload = {
                    "total_tokens": total_tokens,
                    "completed_tokens": completed_tokens,
                    "remaining_tokens": total_tokens - completed_tokens,
                    "received_books": received_books,
                    "failed_tokens": failed_tokens,
                    "current_batch_number": batch_number,
                    "total_batches": total_batches,
                    "current_batch_start_token": batch_start_token,
                    "current_batch_end_token": batch_end_token,
                    "current_batch_status": "failed",
                    "current_batch_started_at_utc": batch_started_at_utc,
                    "failed_token_sample": failed_token_sample,
                    "failure_categories": failure_categories,
                }
                progress(progress_payload)
                if on_progress_update is not None:
                    on_progress_update(dict(progress_payload))
                continue

            completed_tokens += len(chunk)
            received_books += len(chunk_result.books)
            failed_tokens += chunk_result.failed_tokens
            for token_id in chunk_result.failed_token_sample:
                if len(failed_token_sample) < 5:
                    failed_token_sample.append(str(token_id))
            self._merge_failure_categories(failure_categories, chunk_result.failure_categories)
            updated = cache.seed_ask_books(chunk_result.books)
            all_updated.update(updated)
            progress_payload = {
                "total_tokens": total_tokens,
                "completed_tokens": completed_tokens,
                "remaining_tokens": total_tokens - completed_tokens,
                "received_books": received_books,
                "failed_tokens": failed_tokens,
                "current_batch_number": batch_number,
                "total_batches": total_batches,
                "current_batch_start_token": batch_start_token,
                "current_batch_end_token": batch_end_token,
                "current_batch_status": "complete",
                "current_batch_started_at_utc": batch_started_at_utc,
                "failed_token_sample": failed_token_sample,
                "failure_categories": failure_categories,
            }
            progress(progress_payload)
            if on_progress_update is not None:
                on_progress_update(dict(progress_payload))
            if updated and on_chunk_seeded is not None:
                self._ensure_running()
                callback_result = on_chunk_seeded(set(updated))
                if hasattr(callback_result, "__await__"):
                    await callback_result

        self._ensure_running()
        self._runtime_update(
            detail=f"REST ask books seeded: {reason}",
            book_seed_completed_tokens=total_tokens,
            book_seed_remaining_tokens=0,
            book_seed_received_books=received_books,
            book_seed_failed_tokens=failed_tokens,
            book_seed_eta_seconds=0.0,
        )
        self._log_rest_book_seed_failures(reason=reason)
        self.logger.info("rest_books_seeded reason=%s tokens=%s", reason, len(all_updated))
        if total_tokens > 0 and not all_updated:
            raise RuntimeError(f"failed to seed any REST ask books for {reason}")
        return all_updated

    def _run_with_retries(
        self,
        operation: str,
        func: Callable[[], Any],
        *,
        summary: Callable[[Any], Any] | None = None,
    ) -> Any:
        attempt = 1
        while True:
            self._ensure_running()
            try:
                result = func()
            except Exception as exc:
                if not self._should_retry(attempt):
                    raise
                backoff = self.retry_policy.backoff_seconds(attempt)
                self.logger.warning(
                    "scanner_retry operation=%s attempt=%s error=%r backoff_seconds=%.3f",
                    operation,
                    attempt,
                    exc,
                    backoff,
                )
                self._sleep_retry_backoff(backoff)
                if not self.running:
                    raise
                attempt += 1
                continue

            if attempt > 1:
                self.logger.info(
                    "scanner_recovered operation=%s attempts=%s summary=%s",
                    operation,
                    attempt,
                    summary(result) if summary is not None else {},
                )
            self._ensure_running()
            return result

    async def _run_async_with_retries(
        self,
        operation: str,
        func: Callable[[], Any],
        *,
        summary: Callable[[Any], Any] | None = None,
    ) -> Any:
        attempt = 1
        while True:
            self._ensure_running()
            try:
                result = await func()
            except Exception as exc:
                if not self._should_retry(attempt):
                    raise
                backoff = self.retry_policy.backoff_seconds(attempt)
                self.logger.warning(
                    "scanner_retry operation=%s attempt=%s error=%r backoff_seconds=%.3f",
                    operation,
                    attempt,
                    exc,
                    backoff,
                )
                await self._sleep_async_retry_backoff(backoff)
                if not self.running:
                    raise
                attempt += 1
                continue

            if attempt > 1:
                self.logger.info(
                    "scanner_recovered operation=%s attempts=%s summary=%s",
                    operation,
                    attempt,
                    summary(result) if summary is not None else {},
                )
            self._ensure_running()
            return result

    def bootstrap(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.portfolio.load()
        latency_calibration = self._apply_startup_latency_calibration()
        self.portfolio.append_event(
            "paper_portfolio_instance_started",
            {
                "mode": "paper_portfolio_instance",
                "clob_host": self.config.clob_host,
                "market_limit": self.config.market_limit,
                "include_neg_risk": False,
                "market_ws_enabled": self.config.market_ws_enabled,
                "market_ws_endpoint": self.config.market_ws_endpoint,
                "startup_gate": "fresh_full_cache_or_ws_priority_slice_with_background_coverage",
                "market_universe_cache_path": str(self.config.market_universe_cache_path),
                "legacy_fast_start_enabled": self.config.fast_start_enabled,
                "universe_cache_max_age_seconds": self.config.universe_cache_max_age_seconds,
                "min_net_profit_usd": self.params.min_net_profit_usd,
                "min_net_return_bps": self.params.min_net_return_bps,
                "starting_capital_usd": self.params.starting_capital_usd,
                "trade_ceiling_usd": self.params.trade_ceiling_usd,
                "startup_latency_calibration": (
                    latency_calibration.event_payload()
                    if latency_calibration is not None
                    else None
                ),
            },
        )

    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            self.logger.info("shutdown_signal signal=%s", signum)
            self.running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    def run_once(self) -> dict[str, Any]:
        self.bootstrap()
        self._start_runtime(detail="starting one-shot warmup")
        try:
            return self._run_startup_rest_cycle()
        except Exception as exc:
            self._runtime_error(exc)
            raise
        finally:
            self._stop_runtime(detail="one-shot run stopped")
            self.portfolio.save()

    def run_forever(self) -> None:
        self.bootstrap()
        self.install_signal_handlers()
        self._start_runtime(detail="starting warmup")
        try:
            if self.config.market_ws_enabled:
                asyncio.run(self._run_market_ws_forever())
            else:
                self._run_rest_forever()
        except ScannerStopped:
            self.logger.info("scanner_stopped")
        except Exception as exc:
            self._runtime_error(exc)
            raise
        finally:
            self._stop_runtime()
            self.portfolio.save()

    def _run_rest_forever(self) -> None:
        first_cycle = True
        while self.running:
            try:
                if first_cycle:
                    self._run_startup_rest_cycle()
                    first_cycle = False
                else:
                    self.run_one_cycle()
            except ScannerStopped:
                break
            except Exception as exc:
                self.logger.warning("rest_cycle_failed_after_retries error=%r", exc)
                self._runtime_error(exc)
            if self.running:
                time.sleep(self.config.poll_interval_seconds)

    def _run_startup_rest_cycle(self) -> dict[str, Any]:
        universe = self._fetch_startup_market_universe_with_retry_sync()
        result = self._run_incremental_rest_evaluation(
            universe,
            reason="rest_bootstrap",
        )
        self._runtime_update(phase="online", detail="online", last_error=None)
        return result

    async def _run_market_ws_forever(self) -> None:
        cache = MarketDataCache()
        dirty_updates = _DirtyTokenAccumulator()
        ws_params = replace(self.params, max_book_age_seconds=self.config.ws_stale_seconds)
        loop = asyncio.get_running_loop()
        last_dirty_runtime_update_at = 0.0
        ws_reconnect_count = 0
        ws_error_count = 0
        ws_last_error: str | None = None
        ws_stale_token_batches = 0
        ws_stale_tokens = 0

        def _publish_dirty_runtime_status(*, force: bool = False) -> None:
            nonlocal last_dirty_runtime_update_at
            now = loop.time()
            if not force and now - last_dirty_runtime_update_at < 10.0:
                return
            last_dirty_runtime_update_at = now
            self._runtime_update(**dirty_updates.runtime_fields())

        def _record_ws_error(exc: Exception) -> None:
            nonlocal ws_error_count, ws_last_error
            ws_error_count += 1
            ws_last_error = f"{type(exc).__name__}: {exc}"
            _publish_ws_runtime_status(force=True)

        def _record_ws_reconnect() -> None:
            nonlocal ws_reconnect_count
            ws_reconnect_count += 1
            _publish_ws_runtime_status(force=True)

        def _mark_dirty(token_ids: set[str]) -> None:
            if dirty_updates.mark(token_ids):
                _publish_dirty_runtime_status()

        def _mark_disconnected_stale(token_ids: set[str]) -> None:
            nonlocal ws_stale_token_batches, ws_stale_tokens
            if not token_ids:
                return
            ws_stale_token_batches += 1
            ws_stale_tokens += len(token_ids)
            stale_at = datetime.now(timezone.utc) - timedelta(seconds=self.config.ws_stale_seconds + 1.0)
            cache.mark_tokens_stale(token_ids, stale_at=stale_at)
            _publish_ws_runtime_status(force=True)
            if dirty_updates.mark(token_ids):
                _publish_dirty_runtime_status()

        manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(
                endpoint=self.config.market_ws_endpoint,
                heartbeat_seconds=self.config.market_ws_heartbeat_seconds,
                max_assets_per_connection=self.config.market_ws_max_assets_per_connection,
                max_message_size_bytes=self.config.market_ws_max_message_size_bytes,
            ),
            cache=cache,
            logger=self.logger,
            on_dirty_tokens=_mark_dirty,
            on_connection_lost=_mark_disconnected_stale,
            on_connection_error=_record_ws_error,
            on_reconnect=_record_ws_reconnect,
        )

        def _ws_runtime_fields() -> dict[str, Any]:
            return {
                "market_ws_connection_count": manager.connection_count,
                "market_ws_reconnect_count": ws_reconnect_count,
                "market_ws_error_count": ws_error_count,
                "market_ws_last_error": ws_last_error,
                "market_ws_stale_token_batches": ws_stale_token_batches,
                "market_ws_stale_tokens": ws_stale_tokens,
            }

        def _publish_ws_runtime_status(*, force: bool = False) -> None:
            _ = force
            self._runtime_update(**_ws_runtime_fields())

        refresh_task: asyncio.Task[MarketUniverse] | None = None
        reconcile_task: asyncio.Task[float] | None = None
        targeted_backfill_task: asyncio.Task[None] | None = None
        targeted_backfill_tokens: set[str] = set()
        targeted_backfill_stall_monitor = _RestBookSeedBatchStallMonitor(
            reason="dirty_pair_backfill",
            stall_seconds=self.config.rest_book_seed_batch_stall_seconds,
            logger=self.logger,
            runtime_update=self._runtime_update,
            runtime_snapshot=self.runtime.snapshot,
        )

        def _finish_refresh_task_if_ready(current_universe: MarketUniverse) -> MarketUniverse:
            nonlocal refresh_task
            if refresh_task is None or not refresh_task.done():
                return current_universe
            try:
                refreshed_universe = refresh_task.result()
            except Exception as exc:
                self.logger.warning("market_universe_refresh_failed error=%s", exc)
                refreshed_universe = current_universe
            else:
                self._runtime_update(
                    coverage_status="full",
                    coverage_complete=True,
                    coverage_source="full_background_refresh",
                )
            refresh_task = None
            return refreshed_universe

        def _start_refresh_task(current_universe: MarketUniverse, *, reason: str) -> bool:
            nonlocal refresh_task
            if refresh_task is not None and not refresh_task.done():
                return False
            refresh_task = asyncio.create_task(
                self._refresh_market_universe(
                    current_universe,
                    cache,
                    manager,
                    dirty_updates,
                    reason=reason,
                )
            )
            self.logger.info("market_universe_refresh_scheduled reason=%s", reason)
            return True

        def _mark_backfill_seeded(updated: set[str]) -> None:
            if dirty_updates.mark(updated, reason="dirty_pair_backfill"):
                _publish_dirty_runtime_status(force=True)

        async def _run_targeted_backfill() -> None:
            while self.running and targeted_backfill_tokens:
                token_ids = sorted(targeted_backfill_tokens)
                targeted_backfill_tokens.clear()
                await self._seed_rest_books_incrementally_async(
                    cache,
                    token_ids,
                    reason="dirty_pair_backfill",
                    on_chunk_seeded=_mark_backfill_seeded,
                    on_progress_update=lambda progress: targeted_backfill_stall_monitor.note_progress(
                        progress,
                        loop_time=loop.time(),
                    ),
                )

        def _schedule_targeted_backfill(token_ids: set[str]) -> None:
            nonlocal targeted_backfill_task
            if not token_ids:
                return
            targeted_backfill_tokens.update(token_ids)
            if targeted_backfill_task is None or targeted_backfill_task.done():
                targeted_backfill_task = asyncio.create_task(_run_targeted_backfill())

        def _finish_targeted_backfill_if_ready() -> None:
            nonlocal targeted_backfill_task
            if targeted_backfill_task is None or not targeted_backfill_task.done():
                return
            try:
                targeted_backfill_task.result()
            except Exception as exc:
                self.logger.warning("dirty_pair_backfill_failed error=%s", exc)
                self._runtime_error(exc)
            finally:
                targeted_backfill_stall_monitor.reset()
            targeted_backfill_task = None

        def _warn_targeted_backfill_stall_if_needed() -> None:
            if targeted_backfill_task is None or targeted_backfill_task.done():
                return
            targeted_backfill_stall_monitor.maybe_warn(loop_time=loop.time())

        def _evaluate_dirty_batch(
            current_universe: MarketUniverse,
            dirty_batch: _DirtyTokenBatch,
        ) -> None:
            ready_tokens, backfill_tokens = self._split_ready_and_backfill_dirty_tokens(
                current_universe,
                cache,
                dirty_batch.token_ids,
                params=ws_params,
            )
            if ready_tokens is None or ready_tokens:
                self._evaluate_from_cache(
                    current_universe,
                    cache,
                    dirty_token_ids=ready_tokens,
                    evaluation_reason=dirty_batch.evaluation_reason,
                    params=ws_params,
                )
            if backfill_tokens:
                _schedule_targeted_backfill(backfill_tokens)

        try:
            startup_selection = await self._fetch_ws_startup_market_universe_with_retry()
            universe = startup_selection.universe
            self._runtime_update(
                detail="starting market websocket subscriptions",
                events_fetched=universe.events_fetched,
                raw_markets=universe.raw_markets,
                tradable_markets=len(universe.markets),
                tokens=len(universe.token_ids),
                coverage_status=startup_selection.coverage_status,
                coverage_complete=startup_selection.coverage_complete,
            )
            await self._run_async_with_retries(
                "market_ws_start",
                lambda: manager.start(universe.token_ids),
                summary=lambda _result: {
                    "tokens": len(universe.token_ids),
                    "connections": manager.connection_count,
                },
            )
            _publish_ws_runtime_status(force=True)

            startup_evaluated = False
            startup_coverage_refresh_started = False

            def evaluate_startup_chunk(updated: set[str]) -> None:
                nonlocal startup_evaluated, startup_coverage_refresh_started
                ready_tokens, backfill_tokens = self._split_ready_and_backfill_dirty_tokens(
                    universe,
                    cache,
                    updated,
                    params=ws_params,
                )
                if ready_tokens:
                    result = self._evaluate_from_cache(
                        universe,
                        cache,
                        dirty_token_ids=ready_tokens,
                        evaluation_reason="ws_bootstrap",
                        params=ws_params,
                    )
                    if not startup_evaluated and result["summary"]["evaluated_standard_binary_markets"] > 0:
                        startup_evaluated = True
                        self._runtime_update(
                            phase="online",
                            detail="online",
                            executor_status="online",
                            last_error=None,
                            coverage_status=startup_selection.coverage_status,
                            coverage_complete=startup_selection.coverage_complete,
                        )
                        if not startup_selection.coverage_complete and not startup_coverage_refresh_started:
                            startup_coverage_refresh_started = _start_refresh_task(
                                universe,
                                reason="startup_full_coverage",
                            )
                if backfill_tokens:
                    _schedule_targeted_backfill(backfill_tokens)

            self._runtime_update(detail="seeding startup REST ask books")
            await self._seed_rest_books_incrementally_async(
                cache,
                universe.token_ids,
                reason="ws_bootstrap",
                on_chunk_seeded=evaluate_startup_chunk,
            )
            if not startup_evaluated:
                self._evaluate_from_cache(
                    universe,
                    cache,
                    dirty_token_ids=None,
                    evaluation_reason="ws_bootstrap",
                    params=ws_params,
                )
                self._runtime_update(
                    phase="online",
                    detail="online",
                    executor_status="online",
                    last_error=None,
                    coverage_status=startup_selection.coverage_status,
                    coverage_complete=startup_selection.coverage_complete,
                )
            if not startup_selection.coverage_complete and not startup_coverage_refresh_started:
                startup_coverage_refresh_started = _start_refresh_task(universe, reason="startup_full_coverage")

            next_refresh = loop.time() + self.config.market_refresh_interval_seconds
            next_reconcile = loop.time() + self.config.rest_reconcile_interval_seconds

            while self.running:
                _finish_targeted_backfill_if_ready()
                _warn_targeted_backfill_stall_if_needed()
                universe = _finish_refresh_task_if_ready(universe)
                if reconcile_task is not None and reconcile_task.done():
                    try:
                        next_reconcile = reconcile_task.result()
                    except Exception as exc:
                        self.logger.warning("rest_reconcile_failed error=%s", exc)
                        self._runtime_error(exc)
                        next_reconcile = loop.time() + self.config.rest_reconcile_interval_seconds
                    reconcile_task = None
                    _publish_dirty_runtime_status(force=True)
                deadlines = [next_refresh]
                if reconcile_task is None:
                    deadlines.append(next_reconcile)
                timeout = max(0.0, min(deadlines) - loop.time())
                timeout = min(timeout, 1.0)
                dirty_batch = await dirty_updates.wait(timeout=timeout)
                _finish_targeted_backfill_if_ready()
                _warn_targeted_backfill_stall_if_needed()
                universe = _finish_refresh_task_if_ready(universe)

                if dirty_batch is not None:
                    _publish_dirty_runtime_status(force=True)
                    _evaluate_dirty_batch(universe, dirty_batch)

                now = loop.time()
                if now >= next_refresh:
                    if _start_refresh_task(universe, reason="periodic_market_refresh"):
                        next_refresh = now + self.config.market_refresh_interval_seconds
                    else:
                        next_refresh = now + 1.0

                now = loop.time()
                if reconcile_task is None and now >= next_reconcile:
                    dirty_batch = dirty_updates.take_nowait()
                    if dirty_batch is not None:
                        _publish_dirty_runtime_status(force=True)
                        _evaluate_dirty_batch(universe, dirty_batch)
                        continue
                    try:
                        reconcile_task = asyncio.create_task(
                            self._seed_rest_reconcile_and_schedule_next(
                                cache,
                                universe.token_ids,
                                dirty_updates,
                            )
                        )
                    except Exception as exc:
                        self.logger.warning("rest_reconcile_failed error=%s", exc)
                        self._runtime_error(exc)
                        _publish_dirty_runtime_status(force=True)
                        next_reconcile = loop.time() + self.config.rest_reconcile_interval_seconds
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await refresh_task
            if reconcile_task is not None:
                reconcile_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await reconcile_task
            if targeted_backfill_task is not None:
                targeted_backfill_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await targeted_backfill_task
            await manager.stop()

    async def _seed_rest_books(self, cache: MarketDataCache, token_ids: list[str], *, reason: str) -> set[str]:
        return await self._seed_rest_books_incrementally_async(cache, token_ids, reason=reason)

    async def _seed_rest_books_with_retry(
        self,
        cache: MarketDataCache,
        token_ids: list[str],
        *,
        reason: str,
    ) -> set[str]:
        return await self._seed_rest_books(cache, token_ids, reason=reason)

    async def _seed_rest_reconcile_and_schedule_next(
        self,
        cache: MarketDataCache,
        token_ids: list[str],
        dirty_updates: _DirtyTokenAccumulator,
    ) -> float:
        def mark_seeded_chunk(updated: set[str]) -> None:
            if dirty_updates.mark(updated, reason="rest_reconcile"):
                self._runtime_update(**dirty_updates.runtime_fields())

        try:
            await self._seed_rest_books_incrementally_async(
                cache,
                token_ids,
                reason="rest_reconcile",
                on_chunk_seeded=mark_seeded_chunk,
            )
        except Exception as exc:
            self._runtime_error(exc)
            self._runtime_update(**dirty_updates.runtime_fields())
            raise
        self._runtime_update(**dirty_updates.runtime_fields(), last_error=None)
        return asyncio.get_running_loop().time() + self.config.rest_reconcile_interval_seconds

    async def _refresh_market_universe(
        self,
        old_universe: MarketUniverse,
        cache: MarketDataCache,
        manager: MarketWebSocketManager,
        dirty_updates: _DirtyTokenAccumulator,
        *,
        reason: str = "market_refresh",
    ) -> MarketUniverse:
        self._runtime_update(detail=f"refreshing market universe: {reason}")
        fetched_universe = await self._fetch_market_universe_with_retry()
        new_universe = self._priority_merged_universe(old_universe, fetched_universe)
        old_tokens = set(old_universe.token_ids)
        new_tokens = set(new_universe.token_ids)
        added_tokens = sorted(new_tokens - old_tokens)
        removed_tokens = sorted(old_tokens - new_tokens)

        seeded: set[str] = set()
        if added_tokens:
            seeded = await self._seed_rest_books_incrementally_async(
                cache,
                added_tokens,
                reason="market_refresh_added",
            )
        await manager.update_tokens(new_universe.token_ids)
        connection_count = getattr(manager, "connection_count", None)
        if connection_count is not None:
            self._runtime_update(market_ws_connection_count=connection_count)
        if removed_tokens:
            cache.remove_tokens(removed_tokens)

        self._write_market_universe_cache(
            new_universe,
            gamma_query={
                "closed": "false",
                "discovery": "full_active_events",
                "priority_source": "existing_startup_universe",
            },
        )
        self.portfolio.append_event(
            "paper_portfolio_market_universe_refreshed",
            {
                "reason": reason,
                "events_fetched": new_universe.events_fetched,
                "raw_markets": new_universe.raw_markets,
                "tradable_markets": len(new_universe.markets),
                "tokens_added": len(added_tokens),
                "tokens_removed": len(removed_tokens),
            },
        )
        self.logger.info(
            "market_universe_refreshed reason=%s tradable=%s added_tokens=%s removed_tokens=%s",
            reason,
            len(new_universe.markets),
            len(added_tokens),
            len(removed_tokens),
        )
        if seeded:
            dirty_updates.mark(seeded)
        return new_universe

    def run_one_cycle(self) -> dict[str, Any]:
        self._runtime_update(detail="running REST cycle")
        universe = self._fetch_market_universe_with_retry_sync()
        result = self._run_incremental_rest_evaluation(
            universe,
            reason="rest_cycle",
        )
        self._runtime_update(phase="online", detail="online", last_error=None)
        return result

    def _apply_market_limits(
        self,
        markets: list[BinaryMarket],
        *,
        token_limit: int | None = None,
    ) -> list[BinaryMarket]:
        limited = list(markets)
        if self.config.market_limit is not None:
            limited = limited[: self.config.market_limit]
        return _cap_markets_by_token_limit(limited, token_limit)

    def _fetch_market_universe_with_retry_sync(self) -> MarketUniverse:
        return self._run_with_retries(
            "market_universe_fetch",
            self._fetch_market_universe,
            summary=self._universe_retry_summary,
        )

    def _fetch_startup_market_universe_with_retry_sync(self) -> MarketUniverse:
        return self._run_with_retries(
            "market_universe_startup_fetch",
            self._fetch_startup_market_universe,
            summary=self._universe_retry_summary,
        )

    def _fetch_ask_books_with_retry(
        self,
        token_ids: list[str],
        *,
        reason: str,
    ) -> Mapping[str, OrderBookSide]:
        cache = MarketDataCache()
        self._seed_rest_books_incrementally_sync(cache, token_ids, reason=reason)
        return cache.ask_books_snapshot(token_ids)

    def _run_incremental_rest_evaluation(
        self,
        universe: MarketUniverse,
        *,
        reason: str,
    ) -> dict[str, Any]:
        cache = MarketDataCache()
        results: list[dict[str, Any]] = []

        def evaluate_seeded_chunk(updated_token_ids: set[str]) -> None:
            results.append(
                self._evaluate_from_cache(
                    universe,
                    cache,
                    dirty_token_ids=updated_token_ids,
                    evaluation_reason=reason,
                    params=self.params,
                )
            )

        self._seed_rest_books_incrementally_sync(
            cache,
            universe.token_ids,
            reason=reason,
            on_chunk_seeded=evaluate_seeded_chunk,
        )
        if not results:
            results.append(
                self._evaluate_from_cache(
                    universe,
                    cache,
                    dirty_token_ids=None,
                    evaluation_reason=reason,
                    params=self.params,
                )
            )
        combined = self._combine_evaluation_results(results)
        settlement_summary = self._reconcile_paper_settlements(
            universe=universe,
            cache=cache,
            resolution_events_by_market={},
        )
        summary = combined.get("summary", {})
        if isinstance(summary, dict):
            summary.update(settlement_summary)
        skip_counts = summary.get("skip_counts") if isinstance(summary.get("skip_counts"), Mapping) else {}
        simulation_failure_counts = (
            summary.get("simulation_failure_counts")
            if isinstance(summary.get("simulation_failure_counts"), Mapping)
            else {}
        )
        self._runtime_update(
            last_cycle_completed_at_utc=utc_iso(),
            last_evaluation_reason=reason,
            last_cycle_evaluated_markets=_progress_int(summary.get("evaluated_standard_binary_markets")),
            last_cycle_executions=_progress_int(summary.get("executions")),
            last_cycle_skips=sum(_progress_int(count) for count in skip_counts.values()),
            last_cycle_skip_counts=dict(skip_counts),
            last_cycle_simulation_failure_counts=dict(simulation_failure_counts),
            last_simulated_execution_failure_reason=summary.get("last_simulated_execution_failure_reason"),
            pending_settlement_count=_progress_int(settlement_summary.get("pending_settlement_count")),
            settlements_applied_count=_progress_int(settlement_summary.get("settlements_applied_count")),
            last_settlement_at_utc=settlement_summary.get("last_settlement_at_utc"),
            detail=f"completed {reason}",
        )
        return combined

    @staticmethod
    def _combine_evaluation_results(results: list[dict[str, Any]]) -> dict[str, Any]:
        if len(results) == 1:
            return results[0]
        combined_executions: list[dict[str, Any]] = []
        combined_skip_counts: dict[str, int] = {}
        combined_simulation_failure_counts: dict[str, int] = {}
        last_simulation_failure_reason: str | None = None
        evaluated_markets = 0
        for result in results:
            summary = result.get("summary", {})
            combined_executions.extend(result.get("executions", []))
            evaluated_markets += _progress_int(summary.get("evaluated_standard_binary_markets"))
            skip_counts = summary.get("skip_counts")
            if isinstance(skip_counts, Mapping):
                for reason, count in skip_counts.items():
                    combined_skip_counts[str(reason)] = combined_skip_counts.get(str(reason), 0) + _progress_int(count)
            simulation_failure_counts = summary.get("simulation_failure_counts")
            if isinstance(simulation_failure_counts, Mapping):
                for reason, count in simulation_failure_counts.items():
                    combined_simulation_failure_counts[str(reason)] = (
                        combined_simulation_failure_counts.get(str(reason), 0) + _progress_int(count)
                    )
            if summary.get("last_simulated_execution_failure_reason"):
                last_simulation_failure_reason = str(summary["last_simulated_execution_failure_reason"])
        summary = dict(results[-1].get("summary", {}))
        summary["evaluated_standard_binary_markets"] = evaluated_markets
        summary["executions"] = len(combined_executions)
        summary["skip_counts"] = combined_skip_counts
        summary["simulation_failure_counts"] = combined_simulation_failure_counts
        summary["last_simulated_execution_failure_reason"] = last_simulation_failure_reason
        return {"summary": summary, "executions": combined_executions}

    def _market_universe_from_events(
        self,
        events: list[dict[str, Any]],
        *,
        token_limit: int | None = None,
    ) -> MarketUniverse:
        raw_markets = self.client.flatten_event_markets(events)
        tradable_markets = self.client.tradable_binary_markets(raw_markets)
        tradable_markets = self._apply_market_limits(tradable_markets, token_limit=token_limit)
        return _build_market_universe(
            events_fetched=len(events),
            raw_markets=len(raw_markets),
            markets=tradable_markets,
        )

    def _fetch_market_universe(self) -> MarketUniverse:
        self._runtime_update(detail="fetching full Gamma active universe")
        self.logger.info("market_universe_fetch_start market_limit=%s", self.config.market_limit)
        events = self.client.fetch_active_events(
            on_page=self._log_market_event_page,
            should_continue=self._ensure_running,
        )
        universe = self._market_universe_from_events(events)
        self.logger.info(
            "market_universe_fetch_complete events=%s raw_markets=%s tradable_markets=%s tokens=%s",
            universe.events_fetched,
            universe.raw_markets,
            len(universe.markets),
            len(universe.token_ids),
        )
        self._runtime_update(
            events_fetched=universe.events_fetched,
            raw_markets=universe.raw_markets,
            tradable_markets=len(universe.markets),
            tokens=len(universe.token_ids),
            detail="full Gamma active universe fetched",
        )
        return universe

    def _market_universe_from_cache_record(self, record: MarketUniverseCacheRecord) -> MarketUniverse:
        markets = self._apply_market_limits(
            list(record.markets),
        )
        universe = _build_market_universe(
            events_fetched=record.events_fetched,
            raw_markets=record.raw_markets,
            markets=markets,
        )
        self.logger.info(
            "market_universe_cache_loaded events=%s raw_markets=%s tradable_markets=%s tokens=%s",
            universe.events_fetched,
            universe.raw_markets,
            len(universe.markets),
            len(universe.token_ids),
        )
        self._runtime_update(
            detail="loaded fresh full market-universe cache",
            events_fetched=universe.events_fetched,
            raw_markets=universe.raw_markets,
            tradable_markets=len(universe.markets),
            tokens=len(universe.token_ids),
            cache_fetched_at_utc=utc_iso(record.fetched_at),
        )
        return universe

    def _load_cached_market_universe(self) -> MarketUniverse | None:
        record = load_market_universe_cache(
            self.config.market_universe_cache_path,
            max_age_seconds=self.config.universe_cache_max_age_seconds,
            logger=self.logger,
        )
        if record is None:
            return None
        if record.gamma_query.get("discovery") != "full_active_events":
            self.logger.warning(
                "market_universe_cache_ignored reason=not_full path=%s discovery=%r",
                self.config.market_universe_cache_path,
                record.gamma_query.get("discovery"),
            )
            return None
        return self._market_universe_from_cache_record(record)

    def _fetch_startup_market_universe(self) -> MarketUniverse:
        cached = self._load_cached_market_universe()
        if cached is not None:
            return cached
        self._runtime_update(detail="building full market-universe cache")
        universe = self._fetch_market_universe()
        if self._write_market_universe_cache(
            universe,
            gamma_query={"closed": "false", "discovery": "full_active_events"},
        ) is None:
            raise RuntimeError(f"failed to write startup market universe cache: {self.config.market_universe_cache_path}")
        return universe

    def _fetch_ws_startup_market_universe(self) -> _StartupUniverseSelection:
        cached = self._load_cached_market_universe()
        if cached is not None:
            self._runtime_update(
                executor_status="warming",
                coverage_status="full",
                coverage_complete=True,
                coverage_source="fresh_full_cache",
            )
            return _StartupUniverseSelection(
                universe=cached,
                coverage_status="full",
                coverage_complete=True,
            )

        self._runtime_update(
            detail="fetching priority Gamma active universe",
            executor_status="warming",
            coverage_status="priority",
            coverage_complete=False,
            coverage_source="priority_volume24hr_slice",
        )
        self.logger.info(
            "market_universe_priority_fetch_start event_limit=%s token_limit=%s",
            self.config.fast_start_event_limit,
            self.config.fast_start_token_limit,
        )
        events = self.client.fetch_active_events_slice(
            limit=self.config.fast_start_event_limit,
            order="volume24hr",
            ascending=False,
            on_page=self._log_market_event_page,
            should_continue=self._ensure_running,
        )
        universe = self._market_universe_from_events(
            events,
            token_limit=self.config.fast_start_token_limit,
        )
        self.logger.info(
            "market_universe_priority_fetch_complete events=%s raw_markets=%s tradable_markets=%s tokens=%s",
            universe.events_fetched,
            universe.raw_markets,
            len(universe.markets),
            len(universe.token_ids),
        )
        self._runtime_update(
            events_fetched=universe.events_fetched,
            raw_markets=universe.raw_markets,
            tradable_markets=len(universe.markets),
            tokens=len(universe.token_ids),
            detail="priority Gamma active universe fetched",
            executor_status="warming",
            coverage_status="priority",
            coverage_complete=False,
            coverage_source="priority_volume24hr_slice",
        )
        return _StartupUniverseSelection(
            universe=universe,
            coverage_status="priority",
            coverage_complete=False,
        )

    def _write_market_universe_cache(
        self,
        universe: MarketUniverse,
        *,
        gamma_query: Mapping[str, Any],
    ) -> datetime | None:
        fetched_at = datetime.now(timezone.utc)
        try:
            write_market_universe_cache(
                self.config.market_universe_cache_path,
                markets=universe.markets,
                events_fetched=universe.events_fetched,
                raw_markets=universe.raw_markets,
                gamma_query=gamma_query,
                fetched_at=fetched_at,
            )
        except Exception as exc:
            self.logger.warning("market_universe_cache_write_failed path=%s error=%r", self.config.market_universe_cache_path, exc)
            return None
        self.logger.info(
            "market_universe_cache_written path=%s tradable_markets=%s tokens=%s",
            self.config.market_universe_cache_path,
            len(universe.markets),
            len(universe.token_ids),
        )
        self._runtime_update(
            detail="market-universe cache written",
            events_fetched=universe.events_fetched,
            raw_markets=universe.raw_markets,
            tradable_markets=len(universe.markets),
            tokens=len(universe.token_ids),
            cache_fetched_at_utc=utc_iso(fetched_at),
        )
        return fetched_at

    def _priority_merged_universe(self, priority_universe: MarketUniverse, backfill_universe: MarketUniverse) -> MarketUniverse:
        merged_markets = _merge_markets_by_priority(
            list(priority_universe.markets),
            list(backfill_universe.markets),
        )
        return _build_market_universe(
            events_fetched=backfill_universe.events_fetched,
            raw_markets=backfill_universe.raw_markets,
            markets=merged_markets,
        )

    def _log_market_event_page(self, offset: int, rows: int, total_events: int) -> None:
        self.logger.info(
            "market_events_page_fetched offset=%s rows=%s total_events=%s",
            offset,
            rows,
            total_events,
        )
        self._runtime_update(
            detail=f"fetching Gamma active events offset={offset} rows={rows}",
            events_fetched=total_events,
        )

    async def _fetch_startup_market_universe_with_retry(self) -> MarketUniverse:
        return await self._run_async_with_retries(
            "market_universe_startup_fetch",
            lambda: asyncio.to_thread(self._fetch_startup_market_universe),
            summary=self._universe_retry_summary,
        )

    async def _fetch_ws_startup_market_universe_with_retry(self) -> _StartupUniverseSelection:
        return await self._run_async_with_retries(
            "market_universe_startup_fetch",
            lambda: asyncio.to_thread(self._fetch_ws_startup_market_universe),
            summary=lambda selection: {
                **self._universe_retry_summary(selection.universe),
                "coverage_complete": int(selection.coverage_complete),
            },
        )

    async def _fetch_market_universe_with_retry(self) -> MarketUniverse:
        return await self._run_async_with_retries(
            "market_universe_fetch",
            lambda: asyncio.to_thread(self._fetch_market_universe),
            summary=self._universe_retry_summary,
        )

    @staticmethod
    def _universe_retry_summary(universe: MarketUniverse) -> dict[str, int]:
        return {
            "events_fetched": universe.events_fetched,
            "raw_markets": universe.raw_markets,
            "tradable_markets": len(universe.markets),
            "tokens": len(universe.token_ids),
        }

    def _evaluate_from_cache(
        self,
        universe: MarketUniverse,
        cache: MarketDataCache,
        *,
        dirty_token_ids: set[str] | None,
        evaluation_reason: str,
        params: PaperPortfolioParams,
    ) -> dict[str, Any]:
        def fill_time_book_reader(
            market: BinaryMarket,
            fill_time: datetime,
        ) -> FillTimeBookEvidence:
            tokens = [market.yes_token_id, market.no_token_id]
            public_snapshot = cache.public_evidence_snapshot(tokens, until=fill_time)
            yes_public = public_snapshot.get(market.yes_token_id, {})
            no_public = public_snapshot.get(market.no_token_id, {})
            return FillTimeBookEvidence(
                source="ws_cache",
                yes_book=cache.book_side(market.yes_token_id, "ask"),
                no_book=cache.book_side(market.no_token_id, "ask"),
                observed_at=fill_time,
                snapshot_ready={
                    market.yes_token_id: cache.is_snapshot_ready(market.yes_token_id),
                    market.no_token_id: cache.is_snapshot_ready(market.no_token_id),
                },
                snapshot_generation={
                    market.yes_token_id: cache.snapshot_generation(market.yes_token_id),
                    market.no_token_id: cache.snapshot_generation(market.no_token_id),
                },
                public_price_changes={
                    market.yes_token_id: tuple(yes_public.get("recent_price_changes") or ()),
                    market.no_token_id: tuple(no_public.get("recent_price_changes") or ()),
                },
                public_trade_prints={
                    market.yes_token_id: tuple(yes_public.get("recent_trade_prints") or ()),
                    market.no_token_id: tuple(no_public.get("recent_trade_prints") or ()),
                },
                tick_size_changes={
                    market.yes_token_id: tuple(yes_public.get("recent_tick_size_changes") or ()),
                    market.no_token_id: tuple(no_public.get("recent_tick_size_changes") or ()),
                },
                best_bid_asks={
                    market.yes_token_id: tuple(yes_public.get("recent_best_bid_asks") or ()),
                    market.no_token_id: tuple(no_public.get("recent_best_bid_asks") or ()),
                },
            )

        resolution_events_by_market = cache.market_resolution_snapshot()
        return self._evaluate_universe(
            universe,
            cache.ask_books_snapshot(universe.token_ids),
            dirty_token_ids=dirty_token_ids,
            evaluation_reason=evaluation_reason,
            params=params,
            fill_time_book_reader=fill_time_book_reader,
            settlement_cache=cache,
            resolution_events_by_market=resolution_events_by_market,
        )

    def _fetch_open_inventory_markets(self, market_ids: set[str]) -> dict[str, BinaryMarket]:
        if not market_ids:
            return {}
        request_records: list[dict[str, Any]] = []
        try:
            fetcher = getattr(self.client, "fetch_binary_markets_by_ids")
        except AttributeError:
            return {}
        try:
            markets = fetcher(sorted(market_ids), request_records=request_records)
        except Exception as exc:
            self.logger.warning("paper_settlement_market_refresh_failed markets=%s error=%r", len(market_ids), exc)
            return {}
        return dict(markets) if isinstance(markets, Mapping) else {}

    def _reconcile_paper_settlements(
        self,
        *,
        universe: MarketUniverse,
        cache: MarketDataCache | None,
        resolution_events_by_market: Mapping[str, Sequence[Mapping[str, Any]]] | None,
    ) -> dict[str, Any]:
        open_market_ids = self.portfolio.open_inventory_market_ids()
        universe_markets = {market.market_id: market for market in universe.markets if market.market_id in open_market_ids}
        missing_market_ids = open_market_ids - set(universe_markets)
        refreshed_markets = self._fetch_open_inventory_markets(missing_market_ids)
        markets_by_id = {**universe_markets, **refreshed_markets}
        token_ids = sorted(
            {
                token_id
                for market in markets_by_id.values()
                for token_id in (market.yes_token_id, market.no_token_id)
            }
        )
        valuation_snapshot = cache.public_evidence_snapshot(token_ids) if cache is not None and token_ids else {}
        summary = self.portfolio.reconcile_public_markets(
            markets_by_id=markets_by_id,
            resolution_events_by_market=resolution_events_by_market or {},
            valuation_snapshots_by_token=valuation_snapshot,
        )
        return {
            "pending_settlement_count": _progress_int(summary.get("pending_settlement_count")),
            "settlements_applied_count": _progress_int(summary.get("settlements_applied")),
            "last_settlement_at_utc": summary.get("last_settlement_at_utc"),
        }

    @staticmethod
    def _ensure_aware_datetime(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    def _cached_ask_book_is_ready(
        self,
        cache: MarketDataCache,
        token_id: str,
        *,
        params: PaperPortfolioParams,
        now: datetime,
    ) -> bool:
        book = cache.book_side(token_id, "ask")
        if book is None or not cache.is_snapshot_ready(token_id):
            return False
        if book.updated_at is None:
            return True
        age = (
            self._ensure_aware_datetime(now)
            - self._ensure_aware_datetime(book.updated_at)
        ).total_seconds()
        return age <= params.max_book_age_seconds

    def _split_ready_and_backfill_dirty_tokens(
        self,
        universe: MarketUniverse,
        cache: MarketDataCache,
        dirty_token_ids: set[str] | None,
        *,
        params: PaperPortfolioParams,
    ) -> tuple[set[str] | None, set[str]]:
        if dirty_token_ids is None:
            return None, set()
        now = datetime.now(timezone.utc)
        ready_tokens: set[str] = set()
        backfill_tokens: set[str] = set()
        seen_markets: set[str] = set()
        for token_id in dirty_token_ids:
            for market in universe.markets_by_token.get(token_id, ()):
                if market.market_id in seen_markets:
                    continue
                seen_markets.add(market.market_id)
                if market.neg_risk:
                    continue
                pair_tokens = {market.yes_token_id, market.no_token_id}
                if all(
                    self._cached_ask_book_is_ready(cache, pair_token, params=params, now=now)
                    for pair_token in pair_tokens
                ):
                    ready_tokens.update(pair_tokens)
                else:
                    backfill_tokens.update(pair_tokens)
        return ready_tokens, backfill_tokens

    def _evaluate_universe(
        self,
        universe: MarketUniverse,
        books_by_token: Mapping[str, OrderBookSide],
        *,
        dirty_token_ids: set[str] | None,
        evaluation_reason: str,
        params: PaperPortfolioParams,
        fill_time_book_reader: Callable[[BinaryMarket, datetime], FillTimeBookEvidence]
        | None = None,
        settlement_cache: MarketDataCache | None = None,
        resolution_events_by_market: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        cycle_started = datetime.now(timezone.utc)
        self._runtime_update(
            last_cycle_started_at_utc=utc_iso(cycle_started),
            last_evaluation_reason=evaluation_reason,
            detail=f"evaluating {evaluation_reason}",
            events_fetched=universe.events_fetched,
            raw_markets=universe.raw_markets,
            tradable_markets=len(universe.markets),
            tokens=len(universe.token_ids),
        )
        self.logger.info("cycle_start reason=%s at=%s", evaluation_reason, cycle_started.isoformat())
        self.portfolio.append_event(
            "paper_portfolio_cycle_started",
            {
                "cycle_started_at_utc": utc_iso(cycle_started),
                "evaluation_reason": evaluation_reason,
                "dirty_tokens": len(dirty_token_ids) if dirty_token_ids is not None else None,
            },
        )

        skip_counts: dict[str, int] = {}
        simulation_failure_counts: dict[str, int] = {}
        last_simulation_failure_reason: str | None = None
        standard_markets = self._evaluation_targets(
            universe,
            dirty_token_ids=dirty_token_ids,
            skip_counts=skip_counts,
        )
        executions: list[dict[str, Any]] = []

        for market in standard_markets:
            self._ensure_running()
            yes_book = books_by_token.get(market.yes_token_id)
            no_book = books_by_token.get(market.no_token_id)
            if yes_book is None or no_book is None:
                skip_counts["missing_ask_book"] = skip_counts.get("missing_ask_book", 0) + 1
                continue
            market_fill_time_reader = fill_time_book_reader
            if market_fill_time_reader is None and not params.simulation.is_zero_friction:
                market_fill_time_reader = self._rest_fill_time_book_reader(market)
            decision = self.portfolio.execute_binary_complete_set(
                market,
                yes_book,
                no_book,
                as_of=cycle_started,
                params=params,
                fill_time_book_reader=market_fill_time_reader,
            )
            last_reason = self._handle_decision(
                decision,
                executions,
                skip_counts,
                simulation_failure_counts=simulation_failure_counts,
            )
            if last_reason is not None:
                last_simulation_failure_reason = last_reason

        neg_risk_markets = sum(1 for market in universe.markets if market.neg_risk)
        settlement_summary = self._reconcile_paper_settlements(
            universe=universe,
            cache=settlement_cache,
            resolution_events_by_market=resolution_events_by_market or {},
        )
        summary = {
            "cycle_started_at_utc": utc_iso(cycle_started),
            "evaluation_reason": evaluation_reason,
            "dirty_tokens": len(dirty_token_ids) if dirty_token_ids is not None else None,
            "events_fetched": universe.events_fetched,
            "raw_markets": universe.raw_markets,
            "tradable_markets": len(universe.markets),
            "standard_binary_markets": sum(1 for market in universe.markets if not market.neg_risk),
            "neg_risk_markets_skipped": neg_risk_markets,
            "evaluated_standard_binary_markets": len(standard_markets),
            "executions": len(executions),
            "skip_counts": skip_counts,
            "simulation_failure_counts": simulation_failure_counts,
            "last_simulated_execution_failure_reason": last_simulation_failure_reason,
            **settlement_summary,
        }
        self.portfolio.append_event("paper_portfolio_cycle_completed", summary)
        self._runtime_update(
            last_cycle_completed_at_utc=utc_iso(),
            last_evaluation_reason=evaluation_reason,
            last_cycle_evaluated_markets=len(standard_markets),
            last_cycle_executions=len(executions),
            last_cycle_skips=sum(skip_counts.values()),
            last_cycle_skip_counts=skip_counts,
            last_cycle_simulation_failure_counts=simulation_failure_counts,
            last_simulated_execution_failure_reason=last_simulation_failure_reason,
            pending_settlement_count=_progress_int(settlement_summary.get("pending_settlement_count")),
            settlements_applied_count=_progress_int(settlement_summary.get("settlements_applied_count")),
            last_settlement_at_utc=settlement_summary.get("last_settlement_at_utc"),
            events_fetched=universe.events_fetched,
            raw_markets=universe.raw_markets,
            tradable_markets=len(universe.markets),
            tokens=len(universe.token_ids),
            detail=f"completed {evaluation_reason}",
        )
        self.logger.info(
            "cycle_end reason=%s events=%s raw_markets=%s tradable=%s evaluated_binary=%s executions=%s skipped=%s",
            evaluation_reason,
            universe.events_fetched,
            universe.raw_markets,
            len(universe.markets),
            len(standard_markets),
            len(executions),
            skip_counts,
        )
        return {
            "summary": summary,
            "executions": executions,
        }

    def _rest_fill_time_book_reader(
        self,
        market: BinaryMarket,
    ) -> Callable[[BinaryMarket, datetime], FillTimeBookEvidence]:
        def read(_market: BinaryMarket, fill_time: datetime) -> FillTimeBookEvidence:
            try:
                payload = self.client.fetch_ask_books_with_evidence([market.yes_token_id, market.no_token_id])
            except AttributeError:
                try:
                    books = self.client.fetch_ask_books([market.yes_token_id, market.no_token_id])
                except Exception as exc:
                    return FillTimeBookEvidence(
                        source="error",
                        observed_at=fill_time,
                        public_error=f"{type(exc).__name__}: {exc}",
                    )
                return FillTimeBookEvidence(
                    source="rest_snapshot",
                    yes_book=books.get(market.yes_token_id),
                    no_book=books.get(market.no_token_id),
                    observed_at=fill_time,
                    snapshot_ready={
                        market.yes_token_id: market.yes_token_id in books,
                        market.no_token_id: market.no_token_id in books,
                    },
                )
            except Exception as exc:
                return FillTimeBookEvidence(
                    source="error",
                    observed_at=fill_time,
                    public_error=f"{type(exc).__name__}: {exc}",
                )

            books = payload.get("books") if isinstance(payload, Mapping) else {}
            errors = payload.get("errors") if isinstance(payload, Mapping) else {}
            request_records = payload.get("request_records") if isinstance(payload, Mapping) else ()
            return FillTimeBookEvidence(
                source="rest_snapshot",
                yes_book=books.get(market.yes_token_id) if isinstance(books, Mapping) else None,
                no_book=books.get(market.no_token_id) if isinstance(books, Mapping) else None,
                observed_at=fill_time,
                snapshot_ready={
                    market.yes_token_id: isinstance(books, Mapping) and market.yes_token_id in books,
                    market.no_token_id: isinstance(books, Mapping) and market.no_token_id in books,
                },
                request_records=tuple(row for row in request_records if isinstance(row, Mapping))
                if isinstance(request_records, (list, tuple))
                else (),
                errors={str(key): str(value) for key, value in errors.items()} if isinstance(errors, Mapping) else {},
            )

        return read

    def _evaluation_targets(
        self,
        universe: MarketUniverse,
        *,
        dirty_token_ids: set[str] | None,
        skip_counts: dict[str, int],
    ) -> list[BinaryMarket]:
        if dirty_token_ids is None:
            return [market for market in universe.markets if not market.neg_risk]

        standard_by_market_id: dict[str, BinaryMarket] = {}
        neg_risk_seen: set[str] = set()
        for token_id in dirty_token_ids:
            for market in universe.markets_by_token.get(token_id, ()):
                if market.neg_risk:
                    neg_risk_seen.add(market.market_id)
                    continue
                standard_by_market_id[market.market_id] = market

        if neg_risk_seen:
            skip_counts["neg_risk_not_supported"] = len(neg_risk_seen)
        return list(standard_by_market_id.values())

    def _handle_decision(
        self,
        decision: PaperPortfolioDecision,
        executions: list[dict[str, Any]],
        skip_counts: dict[str, int],
        *,
        simulation_failure_counts: dict[str, int] | None = None,
    ) -> str | None:
        if decision.action == "EXECUTE" and decision.execution is not None:
            execution = decision.execution
            executions.append(execution)
            self.logger.info(
                "paper_execution market_id=%s question=%r quantity=%.4f yes_vwap=%.4f no_vwap=%.4f "
                "gross_cost=%.4f fees=%.4f slippage=%.4f tax=%.4f merge=%.4f net_pnl=%.4f "
                "return_bps=%.2f ceiling_used=%.4f stop_reason=%s",
                execution["market_id"],
                execution.get("question"),
                execution["quantity_redeemed"],
                execution["yes_vwap"],
                execution["no_vwap"],
                execution["gross_cost"],
                execution["estimated_fees"],
                execution["slippage_buffer"],
                execution["tax_cost"],
                execution["merge_cost"],
                execution["net_profit"],
                execution["net_return_bps"],
                execution["ceiling_used_usd"],
                execution["stop_reason"],
            )
            return None
        reason = decision.reason or "unknown"
        skip_counts[reason] = skip_counts.get(reason, 0) + 1
        if decision.details.get("simulation_failure") and simulation_failure_counts is not None:
            simulation_failure_counts[reason] = simulation_failure_counts.get(reason, 0) + 1
            return reason
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local paper Polymarket arbitrage portfolio")
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("run", help="Run the continuous paper portfolio instance")
    status_parser = subparsers.add_parser("status", help="Watch paper portfolio runtime status")
    status_parser.add_argument("--once", action="store_true", help="Print one status snapshot and exit")
    status_parser.add_argument(
        "--refresh-seconds",
        type=float,
        default=2.0,
        help="Refresh cadence for watch mode",
    )
    status_parser.add_argument(
        "--show-log",
        action="store_true",
        help="Show backend status history when the runtime payload includes it",
    )
    latency_parser = subparsers.add_parser("latency", help="Measure public Polymarket endpoint latency")
    latency_parser.add_argument(
        "--samples",
        type=int,
        default=5,
        help="REST samples per endpoint",
    )
    latency_parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=10.0,
        help="Timeout for each public endpoint probe",
    )
    latency_parser.add_argument(
        "--pause-seconds",
        type=float,
        default=0.25,
        help="Pause between samples for the same endpoint",
    )
    latency_parser.add_argument(
        "--discovery-limit",
        type=int,
        default=20,
        help="Gamma events to fetch while discovering a probe market",
    )
    latency_parser.add_argument(
        "--include-websocket",
        action="store_true",
        help="Also measure market WebSocket connect and first-message latency",
    )
    latency_parser.add_argument(
        "--ws-samples",
        type=int,
        default=1,
        help="WebSocket samples when --include-websocket is set",
    )
    latency_parser.add_argument(
        "--ws-first-message-timeout-seconds",
        type=float,
        default=5.0,
        help="Time to wait for a subscribed market WebSocket message",
    )
    latency_parser.add_argument(
        "--save",
        action="store_true",
        help="Write the JSON report to data/polymarket_latency_report.json",
    )
    latency_parser.add_argument(
        "--json",
        action="store_true",
        help="Print the raw JSON report instead of a table",
    )
    reset_parser = subparsers.add_parser("reset", help="Reset the local paper portfolio state")
    reset_parser.add_argument("--yes", action="store_true", help="Confirm resetting the paper portfolio")
    return parser


def _config_from_args(args: argparse.Namespace) -> config.ScanConfig:
    _ = args
    return replace(config.load_scan_config(), include_neg_risk=False)


def _money(value: float) -> str:
    return f"${value:,.2f}"


def _render_status_dashboard(
    scan_config: config.ScanConfig,
    portfolio: PaperPortfolio,
    *,
    show_log: bool = False,
) -> str:
    runtime, portfolio_status = read_runtime_and_portfolio_status(
        runtime_path=scan_config.paper_portfolio_runtime_path,
        portfolio_status=portfolio.status,
    )
    return format_status_dashboard(runtime=runtime, portfolio=portfolio_status, show_log=show_log)


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    scan_config = _config_from_args(args)
    command = args.command or "run"

    if command == "status":
        params = PaperPortfolioParams.from_config(scan_config)
        portfolio = PaperPortfolio(
            scan_config.paper_portfolio_instance_path,
            events_path=scan_config.paper_portfolio_events_path,
            params=params,
        )
        refresh_seconds = max(0.1, float(getattr(args, "refresh_seconds", 2.0)))
        show_log = bool(getattr(args, "show_log", False))

        def render() -> str:
            return _render_status_dashboard(scan_config, portfolio, show_log=show_log)

        try:
            if getattr(args, "once", False):
                print(render())
            else:
                run_status_watch(render=render, refresh_seconds=refresh_seconds)
        except KeyboardInterrupt:
            return
        except PaperPortfolioLoadError as exc:
            parser.exit(2, f"{exc}\n")
        return

    if command == "latency":
        settings = LatencyProbeSettings(
            rest_samples=max(1, int(getattr(args, "samples", 5))),
            ws_samples=max(1, int(getattr(args, "ws_samples", 1))),
            timeout_seconds=max(0.1, float(getattr(args, "timeout_seconds", 10.0))),
            pause_seconds=max(0.0, float(getattr(args, "pause_seconds", 0.25))),
            discovery_limit=max(1, int(getattr(args, "discovery_limit", 20))),
            include_websocket=bool(getattr(args, "include_websocket", False)),
            ws_first_message_timeout_seconds=max(
                0.1,
                float(getattr(args, "ws_first_message_timeout_seconds", 5.0)),
            ),
        )
        try:
            report = measure_polymarket_latency(scan_config=scan_config, settings=settings)
        except Exception as exc:
            parser.exit(2, f"{type(exc).__name__}: {exc}\n")
        if getattr(args, "save", False):
            write_latency_report(scan_config.latency_report_path, report)
            print(f"Wrote latency report to {scan_config.latency_report_path}")
        if getattr(args, "json", False):
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(format_latency_report(report))
        return

    if command == "reset":
        params = PaperPortfolioParams.from_config(scan_config)
        portfolio = PaperPortfolio(
            scan_config.paper_portfolio_instance_path,
            events_path=scan_config.paper_portfolio_events_path,
            params=params,
        )
        if not getattr(args, "yes", False):
            parser.error("reset requires --yes")
        try:
            with PortfolioDataLock(scan_config.paper_portfolio_instance_path):
                portfolio.reset(yes=True)
        except PortfolioLockError as exc:
            parser.exit(2, f"{exc}\n")
        print(f"Paper portfolio reset to {_money(params.starting_capital_usd)}")
        return

    params = PaperPortfolioParams.from_config(scan_config)
    portfolio = PaperPortfolio(
        scan_config.paper_portfolio_instance_path,
        events_path=scan_config.paper_portfolio_events_path,
        params=params,
    )

    try:
        with PortfolioDataLock(scan_config.paper_portfolio_instance_path):
            logger = setup_logging(scan_config)
            scanner = ConditionalArbScanner(
                scan_config=scan_config,
                portfolio=portfolio,
                logger=logger,
                params=params,
            )
            scanner.run_forever()
    except PortfolioLockError as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    main()
