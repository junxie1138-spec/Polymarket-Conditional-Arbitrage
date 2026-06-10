from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import signal
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from . import config
from .arb_models import BinaryMarket, OrderBookSide
from .event_log import utc_iso
from .fetcher import GammaClobClient
from .market_data import MarketDataCache, MarketWebSocketManager, MarketWebSocketSettings
from .market_universe_cache import (
    MarketUniverseCacheRecord,
    load_market_universe_cache,
    write_market_universe_cache,
)
from .paper import PaperPortfolio, PaperPortfolioDecision, PaperPortfolioLoadError, PaperPortfolioParams
from .portfolio_lock import PortfolioDataLock, PortfolioLockError
from .runtime_status import (
    RuntimeStatusWriter,
    format_status_dashboard,
    read_runtime_and_portfolio_status,
    run_status_watch,
)

DIRTY_EVALUATION_DEBOUNCE_SECONDS = 0.1


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
        self.running = True
        self.runtime = RuntimeStatusWriter(
            self.config.paper_portfolio_runtime_path,
            cache_path=self.config.market_universe_cache_path,
        )
        self._runtime_started = False

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

    def _stop_runtime(self, *, detail: str = "stopping") -> None:
        if not self._runtime_started:
            return
        self.runtime.stop(detail=detail)
        self._runtime_started = False

    def _runtime_update(self, **fields: Any) -> None:
        if self._runtime_started:
            self.runtime.update(**fields)

    def _runtime_error(self, exc: Exception) -> None:
        self._runtime_update(last_error=f"{type(exc).__name__}: {exc}")

    def _run_with_retries(
        self,
        operation: str,
        func: Callable[[], Any],
        *,
        summary: Callable[[Any], Any] | None = None,
    ) -> Any:
        attempt = 1
        while True:
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
            return result

    def bootstrap(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.portfolio.load()
        self.portfolio.append_event(
            "paper_portfolio_instance_started",
            {
                "mode": "paper_portfolio_instance",
                "clob_host": self.config.clob_host,
                "market_limit": self.config.market_limit,
                "include_neg_risk": False,
                "market_ws_enabled": self.config.market_ws_enabled,
                "market_ws_endpoint": self.config.market_ws_endpoint,
                "startup_gate": "fresh_full_cache_or_full_gamma_rebuild",
                "market_universe_cache_path": str(self.config.market_universe_cache_path),
                "legacy_fast_start_enabled": self.config.fast_start_enabled,
                "universe_cache_max_age_seconds": self.config.universe_cache_max_age_seconds,
                "min_net_profit_usd": self.params.min_net_profit_usd,
                "min_net_return_bps": self.params.min_net_return_bps,
                "starting_capital_usd": self.params.starting_capital_usd,
                "trade_ceiling_usd": self.params.trade_ceiling_usd,
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
        books_by_token = self._fetch_ask_books_with_retry(universe.token_ids)
        result = self._evaluate_universe(
            universe,
            books_by_token,
            dirty_token_ids=None,
            evaluation_reason="rest_bootstrap",
            params=self.params,
        )
        self._runtime_update(phase="online", detail="online", last_error=None)
        return result

    async def _run_market_ws_forever(self) -> None:
        cache = MarketDataCache()
        dirty_queue: asyncio.Queue[set[str]] = asyncio.Queue()
        ws_params = replace(self.params, max_book_age_seconds=self.config.ws_stale_seconds)

        def _mark_dirty(token_ids: set[str]) -> None:
            if token_ids:
                dirty_queue.put_nowait(set(token_ids))

        def _mark_disconnected_stale(token_ids: set[str]) -> None:
            if not token_ids:
                return
            stale_at = datetime.now(timezone.utc) - timedelta(seconds=self.config.ws_stale_seconds + 1.0)
            cache.mark_tokens_stale(token_ids, stale_at=stale_at)
            dirty_queue.put_nowait(set(token_ids))

        manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(
                endpoint=self.config.market_ws_endpoint,
                heartbeat_seconds=self.config.market_ws_heartbeat_seconds,
                max_assets_per_connection=self.config.market_ws_max_assets_per_connection,
            ),
            cache=cache,
            logger=self.logger,
            on_dirty_tokens=_mark_dirty,
            on_connection_lost=_mark_disconnected_stale,
        )

        refresh_task: asyncio.Task[MarketUniverse] | None = None

        def _finish_refresh_task_if_ready(current_universe: MarketUniverse) -> MarketUniverse:
            nonlocal refresh_task
            if refresh_task is None or not refresh_task.done():
                return current_universe
            try:
                refreshed_universe = refresh_task.result()
            except Exception as exc:
                self.logger.warning("market_universe_refresh_failed error=%s", exc)
                refreshed_universe = current_universe
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
                    dirty_queue,
                    reason=reason,
                )
            )
            self.logger.info("market_universe_refresh_scheduled reason=%s", reason)
            return True

        try:
            universe = await self._fetch_startup_market_universe_with_retry()
            self._runtime_update(
                detail="seeding startup REST ask books",
                events_fetched=universe.events_fetched,
                raw_markets=universe.raw_markets,
                tradable_markets=len(universe.markets),
                tokens=len(universe.token_ids),
            )
            await self._seed_rest_books_with_retry(cache, universe.token_ids, reason="ws_bootstrap")
            self._runtime_update(detail="starting market websocket subscriptions")
            await self._run_async_with_retries(
                "market_ws_start",
                lambda: manager.start(universe.token_ids),
                summary=lambda _result: {
                    "tokens": len(universe.token_ids),
                    "connections": manager.connection_count,
                },
            )
            self._evaluate_from_cache(
                universe,
                cache,
                dirty_token_ids=None,
                evaluation_reason="ws_bootstrap",
                params=ws_params,
            )
            self._runtime_update(phase="online", detail="online", last_error=None)

            loop = asyncio.get_running_loop()
            next_refresh = loop.time() + self.config.market_refresh_interval_seconds
            next_reconcile = loop.time() + self.config.rest_reconcile_interval_seconds

            while self.running:
                universe = _finish_refresh_task_if_ready(universe)
                timeout = max(0.0, min(next_refresh, next_reconcile) - loop.time())
                timeout = min(timeout, 1.0)
                dirty_tokens: set[str] | None = None
                try:
                    first_dirty = await asyncio.wait_for(dirty_queue.get(), timeout=timeout)
                    dirty_tokens = await self._collect_dirty_tokens(dirty_queue, first_dirty)
                except TimeoutError:
                    pass
                universe = _finish_refresh_task_if_ready(universe)

                if dirty_tokens:
                    self._evaluate_from_cache(
                        universe,
                        cache,
                        dirty_token_ids=dirty_tokens,
                        evaluation_reason="ws_dirty_update",
                        params=ws_params,
                    )

                now = loop.time()
                if now >= next_refresh:
                    if _start_refresh_task(universe, reason="periodic_market_refresh"):
                        next_refresh = now + self.config.market_refresh_interval_seconds
                    else:
                        next_refresh = now + 1.0

                now = loop.time()
                if now >= next_reconcile:
                    try:
                        await self._seed_rest_books_with_retry(
                            cache,
                            universe.token_ids,
                            reason="rest_reconcile",
                        )
                        dirty_queue.put_nowait(set(universe.token_ids))
                    except Exception as exc:
                        self.logger.warning("rest_reconcile_failed error=%s", exc)
                    next_reconcile = now + self.config.rest_reconcile_interval_seconds
        finally:
            if refresh_task is not None:
                refresh_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await refresh_task
            await manager.stop()

    async def _collect_dirty_tokens(
        self,
        dirty_queue: asyncio.Queue[set[str]],
        first_dirty: set[str],
    ) -> set[str]:
        dirty_tokens = set(first_dirty)
        await asyncio.sleep(DIRTY_EVALUATION_DEBOUNCE_SECONDS)
        while True:
            try:
                dirty_tokens.update(dirty_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return dirty_tokens

    async def _seed_rest_books(self, cache: MarketDataCache, token_ids: list[str], *, reason: str) -> set[str]:
        if not token_ids:
            return set()
        self._runtime_update(detail=f"seeding REST ask books: {reason}", tokens=len(token_ids))
        books = await asyncio.to_thread(self.client.fetch_ask_books, token_ids)
        updated = cache.seed_ask_books(books)
        self.logger.info("rest_books_seeded reason=%s tokens=%s", reason, len(updated))
        return updated

    async def _seed_rest_books_with_retry(
        self,
        cache: MarketDataCache,
        token_ids: list[str],
        *,
        reason: str,
    ) -> set[str]:
        return await self._run_async_with_retries(
            "rest_book_seed",
            lambda: self._seed_rest_books(cache, token_ids, reason=reason),
            summary=lambda updated: {
                "reason": reason,
                "tokens": len(updated),
            },
        )

    async def _refresh_market_universe(
        self,
        old_universe: MarketUniverse,
        cache: MarketDataCache,
        manager: MarketWebSocketManager,
        dirty_queue: asyncio.Queue[set[str]],
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
            seeded = await self._seed_rest_books_with_retry(
                cache,
                added_tokens,
                reason="market_refresh_added",
            )
        await manager.update_tokens(new_universe.token_ids)
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
            dirty_queue.put_nowait(seeded)
        return new_universe

    def run_one_cycle(self) -> dict[str, Any]:
        self._runtime_update(detail="running REST cycle")
        universe = self._fetch_market_universe_with_retry_sync()
        books_by_token = self._fetch_ask_books_with_retry(universe.token_ids)
        result = self._evaluate_universe(
            universe,
            books_by_token,
            dirty_token_ids=None,
            evaluation_reason="rest_cycle",
            params=self.params,
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

    def _fetch_ask_books_with_retry(self, token_ids: list[str]) -> Mapping[str, OrderBookSide]:
        return self._run_with_retries(
            "rest_book_fetch",
            lambda: self.client.fetch_ask_books(token_ids),
            summary=lambda books: {"tokens": len(books)},
        )

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
        return self._evaluate_universe(
            universe,
            cache.ask_books_snapshot(universe.token_ids),
            dirty_token_ids=dirty_token_ids,
            evaluation_reason=evaluation_reason,
            params=params,
        )

    def _evaluate_universe(
        self,
        universe: MarketUniverse,
        books_by_token: Mapping[str, OrderBookSide],
        *,
        dirty_token_ids: set[str] | None,
        evaluation_reason: str,
        params: PaperPortfolioParams,
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
        standard_markets = self._evaluation_targets(
            universe,
            dirty_token_ids=dirty_token_ids,
            skip_counts=skip_counts,
        )
        executions: list[dict[str, Any]] = []

        for market in standard_markets:
            yes_book = books_by_token.get(market.yes_token_id)
            no_book = books_by_token.get(market.no_token_id)
            if yes_book is None or no_book is None:
                skip_counts["missing_ask_book"] = skip_counts.get("missing_ask_book", 0) + 1
                continue
            decision = self.portfolio.execute_binary_complete_set(
                market,
                yes_book,
                no_book,
                as_of=cycle_started,
                params=params,
            )
            self._handle_decision(decision, executions, skip_counts)

        neg_risk_markets = sum(1 for market in universe.markets if market.neg_risk)
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
        }
        self.portfolio.append_event("paper_portfolio_cycle_completed", summary)
        self._runtime_update(
            last_cycle_completed_at_utc=utc_iso(),
            last_evaluation_reason=evaluation_reason,
            last_cycle_evaluated_markets=len(standard_markets),
            last_cycle_executions=len(executions),
            last_cycle_skips=sum(skip_counts.values()),
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
    ) -> None:
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
            return
        reason = decision.reason or "unknown"
        skip_counts[reason] = skip_counts.get(reason, 0) + 1


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
    params = PaperPortfolioParams.from_config(scan_config)
    portfolio = PaperPortfolio(
        scan_config.paper_portfolio_instance_path,
        events_path=scan_config.paper_portfolio_events_path,
        params=params,
    )

    if command == "status":
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

    if command == "reset":
        if not getattr(args, "yes", False):
            parser.error("reset requires --yes")
        try:
            with PortfolioDataLock(scan_config.paper_portfolio_instance_path):
                portfolio.reset(yes=True)
        except PortfolioLockError as exc:
            parser.exit(2, f"{exc}\n")
        print(f"Paper portfolio reset to {_money(params.starting_capital_usd)}")
        return

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
