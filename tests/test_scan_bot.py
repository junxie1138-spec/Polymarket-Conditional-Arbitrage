from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polymarket_conditional_arb import config, portfolio_lock, runtime_status
from polymarket_conditional_arb.event_log import utc_iso
from polymarket_conditional_arb.fetcher import GammaClobClient
from polymarket_conditional_arb.market_data import MarketDataCache
from polymarket_conditional_arb.market_universe_cache import write_market_universe_cache
from polymarket_conditional_arb.order_book import asks_from_book
from polymarket_conditional_arb.paper import PaperPortfolio, PaperPortfolioParams
from polymarket_conditional_arb.portfolio_lock import PortfolioDataLock, PortfolioLockError
from polymarket_conditional_arb.runtime_status import (
    RuntimeStatusWriter,
    derive_runtime_state,
    format_status_dashboard,
    run_status_watch,
)
from polymarket_conditional_arb.scan_bot import (
    ConditionalArbScanner,
    ScannerRetryPolicy,
    ScannerStopped,
    _config_from_args,
    build_parser,
    main,
)


class FakeClient:
    def fetch_active_events(self, *, on_page=None, should_continue=None):
        if on_page is not None:
            on_page(0, 1, 1)
        if should_continue is not None:
            should_continue()
        return [
            {
                "id": "e1",
                "title": "Event",
                "markets": [
                    {
                        "id": "m1",
                        "conditionId": "c1",
                        "question": "Will X happen?",
                        "outcomes": '["Yes", "No"]',
                        "clobTokenIds": '["yes-token", "no-token"]',
                        "active": True,
                        "closed": False,
                        "acceptingOrders": True,
                        "enableOrderBook": True,
                    }
                ],
            }
        ]

    @staticmethod
    def flatten_event_markets(events):
        return GammaClobClient.flatten_event_markets(events)

    @staticmethod
    def tradable_binary_markets(markets):
        return GammaClobClient.tradable_binary_markets(markets)

    @staticmethod
    def fetch_ask_books(_token_ids, *, on_progress=None):
        token_ids = list(_token_ids)
        if on_progress is not None:
            on_progress(
                {
                    "total_tokens": len(token_ids),
                    "completed_tokens": len(token_ids),
                    "remaining_tokens": 0,
                    "received_books": len(token_ids),
                    "failed_tokens": 0,
                }
            )
        return {
            "yes-token": asks_from_book(
                {"asks": [{"price": "0.48", "size": "10"}]},
                token_id="yes-token",
            ),
            "no-token": asks_from_book(
                {"asks": [{"price": "0.49", "size": "10"}]},
                token_id="no-token",
            ),
        }


class TwoMarketClient:
    def __init__(self):
        self.fetch_ask_books_calls = 0

    def fetch_active_events(self, *, on_page=None, should_continue=None):
        if on_page is not None:
            on_page(0, 1, 1)
        if should_continue is not None:
            should_continue()
        return [
            {
                "id": "e1",
                "title": "Event",
                "markets": [
                    self._market_row("m1", "yes-1", "no-1"),
                    self._market_row("m2", "yes-2", "no-2"),
                ],
            }
        ]

    @staticmethod
    def _market_row(market_id, yes_token_id, no_token_id):
        return {
            "id": market_id,
            "conditionId": f"c-{market_id}",
            "question": f"Will {market_id} happen?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": json.dumps([yes_token_id, no_token_id]),
            "active": True,
            "closed": False,
            "acceptingOrders": True,
            "enableOrderBook": True,
        }

    @staticmethod
    def flatten_event_markets(events):
        return GammaClobClient.flatten_event_markets(events)

    @staticmethod
    def tradable_binary_markets(markets):
        return GammaClobClient.tradable_binary_markets(markets)

    def fetch_ask_books(self, token_ids, *, on_progress=None):
        token_ids = list(token_ids)
        self.fetch_ask_books_calls += 1
        if on_progress is not None:
            on_progress(
                {
                    "total_tokens": len(token_ids),
                    "completed_tokens": len(token_ids),
                    "remaining_tokens": 0,
                    "received_books": len(token_ids),
                    "failed_tokens": 0,
                }
            )
        return profitable_books(token_ids, updated_at=datetime.now(timezone.utc))


class RecordingDiscoveryClient:
    def __init__(
        self,
        *,
        startup_rows=None,
        full_rows=None,
        fail_on_discovery: bool = False,
    ):
        self.startup_rows = list(startup_rows or [])
        self.full_rows = list(full_rows or self.startup_rows)
        self.fail_on_discovery = fail_on_discovery
        self.slice_calls = []
        self.full_calls = 0
        self.fetch_ask_books_calls = 0

    def fetch_active_events_slice(self, *, limit, order=None, ascending=None, on_page=None, should_continue=None):
        if self.fail_on_discovery:
            raise AssertionError("live discovery should not be called")
        self.slice_calls.append({"limit": limit, "order": order, "ascending": ascending})
        if on_page is not None:
            on_page(0, 1, 1)
        if should_continue is not None:
            should_continue()
        return [{"id": "startup-event", "title": "Startup", "markets": self.startup_rows}]

    def fetch_active_events(self, *, on_page=None, should_continue=None):
        if self.fail_on_discovery:
            raise AssertionError("live discovery should not be called")
        self.full_calls += 1
        if on_page is not None:
            on_page(0, 1, 1)
        if should_continue is not None:
            should_continue()
        return [{"id": "full-event", "title": "Full", "markets": self.full_rows}]

    @staticmethod
    def flatten_event_markets(events):
        return GammaClobClient.flatten_event_markets(events)

    @staticmethod
    def tradable_binary_markets(markets):
        return GammaClobClient.tradable_binary_markets(markets)

    def fetch_ask_books(self, token_ids, *, on_progress=None):
        token_ids = list(token_ids)
        self.fetch_ask_books_calls += 1
        if on_progress is not None:
            on_progress(
                {
                    "total_tokens": len(token_ids),
                    "completed_tokens": len(token_ids),
                    "remaining_tokens": 0,
                    "received_books": len(token_ids),
                    "failed_tokens": 0,
                }
            )
        return profitable_books(token_ids, updated_at=datetime.now(timezone.utc))


class RecordingManager:
    def __init__(self):
        self.updated_token_ids = []

    async def update_tokens(self, token_ids):
        self.updated_token_ids.append(list(token_ids))


class FlakyEventClient(FakeClient):
    def __init__(self, *, failures: int):
        self.failures = failures
        self.fetch_active_events_calls = 0

    def fetch_active_events(self, **kwargs):
        self.fetch_active_events_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("events unavailable")
        return super().fetch_active_events(**kwargs)


class FlakyBookClient(TwoMarketClient):
    def __init__(self, *, failures: int):
        super().__init__()
        self.failures = failures

    def fetch_ask_books(self, token_ids, *, on_progress=None):
        token_ids = list(token_ids)
        self.fetch_ask_books_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("books unavailable")
        if on_progress is not None:
            on_progress(
                {
                    "total_tokens": len(token_ids),
                    "completed_tokens": len(token_ids),
                    "remaining_tokens": 0,
                    "received_books": len(token_ids),
                    "failed_tokens": 0,
                }
            )
        return profitable_books(token_ids, updated_at=datetime.now(timezone.utc))


def scan_config(tmp_path: Path):
    return config.ScanConfig(
        data_dir=tmp_path / "data",
        log_dir=tmp_path / "logs",
        clob_host="https://clob.example",
        market_limit=None,
        poll_interval_seconds=60,
        min_net_profit_usd=0.0,
        min_net_return_bps=0.0,
        max_capital_usd=20.0,
        starting_capital_usd=1000.0,
        trade_ceiling_usd=20.0,
        slippage_buffer_bps=0.0,
        gas_cost_usd=0.0,
        merge_cost_usd=0.0,
        taker_fee_bps=0.0,
        tax_bps=0.0,
        max_book_age_seconds=20.0,
        include_neg_risk=True,
    )


def scanner_for(tmp_path: Path, client, cfg=None):
    cfg = cfg or scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=client,
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=null_logger(),
        params=params,
    )
    scanner.bootstrap()
    return scanner


def null_logger():
    logger = logging.getLogger("test_scanner")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def dashboard_rows(dashboard: str) -> list[str]:
    lines = dashboard.splitlines()
    assert len(lines) >= 4
    width = len(lines[0])
    assert all(len(line) == width for line in lines)
    assert lines[0] == lines[2] == lines[-1]
    assert lines[0].startswith("+") and lines[0].endswith("+")
    assert set(lines[0][1:-1]) == {"-"}
    content_indexes = set(range(len(lines))) - {0, 2, len(lines) - 1}
    assert all(lines[index].startswith("| ") and lines[index].endswith(" |") for index in content_indexes)
    return [lines[index][2:-2].rstrip() for index in sorted(content_indexes)]


def profitable_books(token_ids, *, updated_at):
    books = {}
    for token_id in token_ids:
        price = "0.48" if "yes" in token_id else "0.49"
        books[token_id] = asks_from_book(
            {"asks": [{"price": price, "size": "10"}]},
            token_id=token_id,
            updated_at=updated_at,
        )
    return books


def tradable_markets_for_rows(rows):
    raw_markets = GammaClobClient.flatten_event_markets([{"id": "cached-event", "markets": rows}])
    return GammaClobClient.tradable_binary_markets(raw_markets)


def test_market_universe_fetch_logs_startup_progress(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=FakeClient(),
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=logging.getLogger("test_market_universe_progress"),
        params=params,
    )

    with caplog.at_level(logging.INFO, logger="test_market_universe_progress"):
        universe = scanner._fetch_market_universe()

    assert len(universe.markets) == 1
    assert "market_universe_fetch_start market_limit=None" in caplog.text
    assert "market_events_page_fetched offset=0 rows=1 total_events=1" in caplog.text
    assert "market_universe_fetch_complete events=1 raw_markets=1 tradable_markets=1 tokens=2" in caplog.text


def test_market_universe_fetch_stops_between_event_pages(tmp_path):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=FakeClient(),
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=null_logger(),
        params=params,
    )
    scanner.running = False

    with pytest.raises(ScannerStopped, match="scanner stopped"):
        scanner._fetch_market_universe()


def test_missing_startup_cache_rebuilds_full_universe_before_first_evaluation(tmp_path):
    cfg = replace(
        scan_config(tmp_path),
        fast_start_enabled=True,
        fast_start_event_limit=20,
        fast_start_token_limit=2,
    )
    client = RecordingDiscoveryClient(
        startup_rows=[
            TwoMarketClient._market_row("m1", "yes-1", "no-1"),
            TwoMarketClient._market_row("m2", "yes-2", "no-2"),
        ],
        full_rows=[
            TwoMarketClient._market_row("m3", "yes-3", "no-3"),
            TwoMarketClient._market_row("m4", "yes-4", "no-4"),
        ],
    )
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    universe = scanner._fetch_startup_market_universe()
    result = scanner._evaluate_universe(
        universe,
        client.fetch_ask_books(universe.token_ids),
        dirty_token_ids=None,
        evaluation_reason="ws_bootstrap",
        params=scanner.params,
    )

    assert client.slice_calls == []
    assert client.full_calls == 1
    assert [market.market_id for market in universe.markets] == ["m3", "m4"]
    assert universe.token_ids == ["yes-3", "no-3", "yes-4", "no-4"]
    assert result["summary"]["evaluated_standard_binary_markets"] == 2
    cache_payload = json.loads(cfg.market_universe_cache_path.read_text(encoding="utf-8"))
    assert cache_payload["gamma_query"]["discovery"] == "full_active_events"


def test_fresh_full_startup_cache_skips_gamma_discovery_without_token_cap(tmp_path):
    cfg = replace(
        scan_config(tmp_path),
        fast_start_enabled=True,
        fast_start_token_limit=2,
    )
    cached_markets = tradable_markets_for_rows(
        [
            TwoMarketClient._market_row("m1", "yes-1", "no-1"),
            TwoMarketClient._market_row("m2", "yes-2", "no-2"),
        ]
    )
    write_market_universe_cache(
        cfg.market_universe_cache_path,
        markets=cached_markets,
        events_fetched=2,
        raw_markets=2,
        gamma_query={"closed": "false", "discovery": "full_active_events"},
        fetched_at=datetime.now(timezone.utc),
    )
    client = RecordingDiscoveryClient(fail_on_discovery=True)
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    universe = scanner._fetch_startup_market_universe()

    assert client.slice_calls == []
    assert client.full_calls == 0
    assert [market.market_id for market in universe.markets] == ["m1", "m2"]
    assert universe.events_fetched == 2
    assert universe.raw_markets == 2


def test_corrupt_startup_cache_rebuilds_full_universe(tmp_path, caplog):
    cfg = replace(scan_config(tmp_path), fast_start_enabled=True)
    cfg.market_universe_cache_path.parent.mkdir(parents=True)
    cfg.market_universe_cache_path.write_text("{not-json", encoding="utf-8")
    client = RecordingDiscoveryClient(
        startup_rows=[TwoMarketClient._market_row("m1", "yes-1", "no-1")],
        full_rows=[TwoMarketClient._market_row("m2", "yes-2", "no-2")],
    )
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    with caplog.at_level(logging.WARNING, logger="test_scanner"):
        universe = scanner._fetch_startup_market_universe()

    assert [market.market_id for market in universe.markets] == ["m2"]
    assert client.slice_calls == []
    assert client.full_calls == 1
    assert "market_universe_cache_ignored reason=invalid" in caplog.text


def test_stale_startup_cache_rebuilds_full_universe(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    cached_markets = tradable_markets_for_rows([TwoMarketClient._market_row("old", "yes-old", "no-old")])
    write_market_universe_cache(
        cfg.market_universe_cache_path,
        markets=cached_markets,
        events_fetched=1,
        raw_markets=1,
        gamma_query={"closed": "false", "discovery": "full_active_events"},
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=7200),
    )
    client = RecordingDiscoveryClient(full_rows=[TwoMarketClient._market_row("new", "yes-new", "no-new")])
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    with caplog.at_level(logging.WARNING, logger="test_scanner"):
        universe = scanner._fetch_startup_market_universe()

    assert [market.market_id for market in universe.markets] == ["new"]
    assert client.full_calls == 1
    assert "market_universe_cache_ignored reason=stale" in caplog.text


def test_partial_discovery_cache_is_not_usable_for_startup_gate(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    cached_markets = tradable_markets_for_rows([TwoMarketClient._market_row("slice", "yes-s", "no-s")])
    write_market_universe_cache(
        cfg.market_universe_cache_path,
        markets=cached_markets,
        events_fetched=1,
        raw_markets=1,
        gamma_query={"closed": "false", "order": "volume24hr"},
        fetched_at=datetime.now(timezone.utc),
    )
    client = RecordingDiscoveryClient(full_rows=[TwoMarketClient._market_row("full", "yes-f", "no-f")])
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    with caplog.at_level(logging.WARNING, logger="test_scanner"):
        universe = scanner._fetch_startup_market_universe()

    assert [market.market_id for market in universe.markets] == ["full"]
    assert client.full_calls == 1
    assert "market_universe_cache_ignored reason=not_full" in caplog.text


def test_startup_cache_write_failure_blocks_book_seed_and_evaluation(tmp_path, monkeypatch):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    monkeypatch.setattr(scanner, "_write_market_universe_cache", lambda *_args, **_kwargs: None)

    with pytest.raises(RuntimeError, match="failed to write startup market universe cache"):
        scanner._run_startup_rest_cycle()

    assert client.fetch_ask_books_calls == 0
    assert not scanner.config.paper_portfolio_instance_path.exists()


def test_background_refresh_adds_tokens_updates_ws_and_writes_cache(tmp_path):
    cfg = replace(scan_config(tmp_path), fast_start_enabled=True, fast_start_token_limit=2)
    cached_markets = tradable_markets_for_rows([TwoMarketClient._market_row("m1", "yes-1", "no-1")])
    write_market_universe_cache(
        cfg.market_universe_cache_path,
        markets=cached_markets,
        events_fetched=1,
        raw_markets=1,
        gamma_query={"closed": "false", "discovery": "full_active_events"},
        fetched_at=datetime.now(timezone.utc),
    )
    client = RecordingDiscoveryClient(
        startup_rows=[TwoMarketClient._market_row("m1", "yes-1", "no-1")],
        full_rows=[
            TwoMarketClient._market_row("m1", "yes-1", "no-1"),
            TwoMarketClient._market_row("m2", "yes-2", "no-2"),
        ],
    )
    scanner = scanner_for(tmp_path, client, cfg=cfg)
    old_universe = scanner._fetch_startup_market_universe()
    cache = MarketDataCache()
    cache.seed_ask_books(profitable_books(old_universe.token_ids, updated_at=datetime.now(timezone.utc)))
    manager = RecordingManager()
    dirty_queue: asyncio.Queue[set[str]] = asyncio.Queue()

    new_universe = asyncio.run(
        scanner._refresh_market_universe(
            old_universe,
            cache,
            manager,
            dirty_queue,
            reason="periodic_market_refresh",
        )
    )

    assert client.full_calls == 1
    assert [market.market_id for market in new_universe.markets] == ["m1", "m2"]
    assert manager.updated_token_ids[-1] == ["yes-1", "no-1", "yes-2", "no-2"]
    assert dirty_queue.get_nowait() == {"yes-2", "no-2"}
    assert cache.book_side("yes-2", "ask") is not None
    assert cfg.market_universe_cache_path.exists()


def test_dirty_token_evaluation_runs_while_slow_full_refresh_is_in_progress(tmp_path):
    cfg = replace(scan_config(tmp_path), fast_start_enabled=True, fast_start_token_limit=2)
    cached_markets = tradable_markets_for_rows([TwoMarketClient._market_row("m1", "yes-1", "no-1")])
    write_market_universe_cache(
        cfg.market_universe_cache_path,
        markets=cached_markets,
        events_fetched=1,
        raw_markets=1,
        gamma_query={"closed": "false", "discovery": "full_active_events"},
        fetched_at=datetime.now(timezone.utc),
    )
    client = RecordingDiscoveryClient(
        startup_rows=[TwoMarketClient._market_row("m1", "yes-1", "no-1")],
        full_rows=[
            TwoMarketClient._market_row("m1", "yes-1", "no-1"),
            TwoMarketClient._market_row("m2", "yes-2", "no-2"),
        ],
    )
    scanner = scanner_for(tmp_path, client, cfg=cfg)
    old_universe = scanner._fetch_startup_market_universe()
    cache = MarketDataCache()
    cache.seed_ask_books(profitable_books(old_universe.token_ids, updated_at=datetime.now(timezone.utc)))
    manager = RecordingManager()
    dirty_queue: asyncio.Queue[set[str]] = asyncio.Queue()

    async def run_refresh_and_dirty_evaluation():
        async def slow_full_refresh():
            await asyncio.sleep(0.05)
            return scanner._fetch_market_universe()

        scanner._fetch_market_universe_with_retry = slow_full_refresh
        refresh_task = asyncio.create_task(
            scanner._refresh_market_universe(
                old_universe,
                cache,
                manager,
                dirty_queue,
                reason="periodic_market_refresh",
            )
        )
        await asyncio.sleep(0)
        assert not refresh_task.done()
        result = scanner._evaluate_from_cache(
            old_universe,
            cache,
            dirty_token_ids={"yes-1"},
            evaluation_reason="ws_dirty_update",
            params=scanner.params,
        )
        refreshed_universe = await refresh_task
        return result, refreshed_universe

    result, refreshed_universe = asyncio.run(run_refresh_and_dirty_evaluation())

    assert result["summary"]["evaluated_standard_binary_markets"] == 1
    assert result["executions"][0]["market_id"] == "m1"
    assert [market.market_id for market in refreshed_universe.markets] == ["m1", "m2"]


def test_runner_executes_and_persists_paper_portfolio_state(tmp_path):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=FakeClient(),
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=null_logger(),
        params=params,
    )

    result = scanner.run_once()

    assert result["summary"]["executions"] == 1
    assert cfg.paper_portfolio_instance_path.exists()
    assert cfg.paper_portfolio_events_path.exists()

    state = json.loads(cfg.paper_portfolio_instance_path.read_text(encoding="utf-8"))
    assert state["starting_capital_usd"] == 1000.0
    assert state["cash"] == pytest.approx(1000.3)
    assert state["realized_pnl"] == pytest.approx(0.3)
    assert state["executions"][0]["market_id"] == "m1"
    assert state["executions"][0]["quantity_redeemed"] == 10.0
    assert state["inventory"] == {}


def test_runtime_status_records_warmup_progress_before_startup_evaluation(tmp_path):
    cfg = scan_config(tmp_path)
    client = RecordingDiscoveryClient(
        full_rows=[
            TwoMarketClient._market_row("m1", "yes-1", "no-1"),
            TwoMarketClient._market_row("m2", "yes-2", "no-2"),
        ],
    )
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    scanner._start_runtime(detail="test warmup")
    try:
        universe = scanner._fetch_startup_market_universe()
        runtime = json.loads(cfg.paper_portfolio_runtime_path.read_text(encoding="utf-8"))
    finally:
        scanner._stop_runtime()

    assert [market.market_id for market in universe.markets] == ["m1", "m2"]
    assert client.fetch_ask_books_calls == 0
    assert runtime["phase"] == "warmup"
    assert runtime["status"] == "WARMUP"
    assert runtime["events_fetched"] == 1
    assert runtime["raw_markets"] == 2
    assert runtime["tradable_markets"] == 2
    assert runtime["tokens"] == 4
    assert runtime["cache_fetched_at_utc"] is not None
    assert runtime["last_cycle_started_at_utc"] is None


def test_runtime_status_records_online_after_startup_evaluation(tmp_path):
    cfg = scan_config(tmp_path)
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client, cfg=cfg)

    scanner._start_runtime(detail="test warmup")
    try:
        result = scanner._run_startup_rest_cycle()
        runtime = json.loads(cfg.paper_portfolio_runtime_path.read_text(encoding="utf-8"))
    finally:
        scanner._stop_runtime()

    assert result["summary"]["executions"] == 2
    assert client.fetch_ask_books_calls == 1
    assert runtime["phase"] == "online"
    assert runtime["status"] == "ONLINE"
    assert runtime["detail"] == "online"
    assert runtime["warmup_started_at_utc"] is not None
    assert runtime["warmup_completed_at_utc"] is not None
    assert runtime["book_seed_reason"] == "rest_bootstrap"
    assert runtime["book_seed_total_tokens"] == 4
    assert runtime["book_seed_completed_tokens"] == 4
    assert runtime["book_seed_remaining_tokens"] == 0
    assert runtime["book_seed_received_books"] == 4
    assert runtime["book_seed_failed_tokens"] == 0
    assert runtime["book_seed_eta_seconds"] == 0.0
    assert runtime["last_evaluation_reason"] == "rest_bootstrap"
    assert runtime["last_cycle_completed_at_utc"] is not None
    assert runtime["last_cycle_evaluated_markets"] == 2
    assert runtime["last_cycle_executions"] == 2


def test_runtime_status_records_book_seed_batch_progress(tmp_path):
    cfg = scan_config(tmp_path)
    scanner = scanner_for(tmp_path, TwoMarketClient(), cfg=cfg)
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)

    scanner._start_runtime(detail="test warmup")
    try:
        progress = scanner._book_seed_progress_callback(reason="ws_bootstrap", total_tokens=1000)
        progress(
            {
                "total_tokens": 1000,
                "completed_tokens": 500,
                "remaining_tokens": 500,
                "received_books": 498,
                "failed_tokens": 2,
                "current_batch_number": 2,
                "total_batches": 2,
                "current_batch_start_token": 501,
                "current_batch_end_token": 1000,
                "current_batch_status": "in_flight",
                "current_batch_started_at_utc": utc_iso(now),
            }
        )
        runtime = json.loads(cfg.paper_portfolio_runtime_path.read_text(encoding="utf-8"))
    finally:
        scanner._stop_runtime()

    assert runtime["book_seed_batch_number"] == 2
    assert runtime["book_seed_total_batches"] == 2
    assert runtime["book_seed_batch_start_token"] == 501
    assert runtime["book_seed_batch_end_token"] == 1000
    assert runtime["book_seed_batch_status"] == "in_flight"
    assert runtime["book_seed_batch_started_at_utc"] == "2026-06-10T12:00:00Z"


def test_runtime_status_writer_retries_transient_replace_permission_error(tmp_path, monkeypatch):
    path = tmp_path / "data" / "paper_portfolio_runtime.json"
    writer = RuntimeStatusWriter(
        path,
        cache_path=tmp_path / "data" / "market_universe_cache.json",
        write_retry_backoff_seconds=0.0,
    )
    path_type = type(path)
    original_replace = path_type.replace
    replace_calls = 0

    def flaky_replace(self, target):
        nonlocal replace_calls
        replace_calls += 1
        if replace_calls == 1:
            raise PermissionError("runtime file is temporarily locked")
        return original_replace(self, target)

    monkeypatch.setattr(path_type, "replace", flaky_replace)

    writer.update(detail="retry succeeded")

    runtime = json.loads(path.read_text(encoding="utf-8"))
    assert replace_calls == 2
    assert runtime["detail"] == "retry succeeded"
    assert runtime["runtime_status_write_failures"] == 0
    assert writer.snapshot()["runtime_status_write_failures"] == 0


def test_status_state_derives_online_warmup_and_dead(monkeypatch):
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now),
        "phase": "online",
    }

    assert derive_runtime_state(runtime, now=now) == "ONLINE"
    assert derive_runtime_state({**runtime, "phase": "warmup"}, now=now) == "WARMUP"
    assert derive_runtime_state(None, now=now) == "DEAD"
    assert derive_runtime_state(
        {**runtime, "heartbeat_at_utc": utc_iso(now - timedelta(seconds=16))},
        now=now,
    ) == "DEAD"

    monkeypatch.setattr(PortfolioDataLock, "_process_is_alive", staticmethod(lambda _pid: False))
    assert derive_runtime_state(runtime, now=now) == "DEAD"


def test_process_liveness_uses_win32_probe_on_windows(monkeypatch):
    pid = os.getpid() + 100_000
    kill_calls = []

    def fail_os_kill(checked_pid, signal):
        kill_calls.append((checked_pid, signal))
        raise OSError(87, "parameter incorrect")

    monkeypatch.setattr(portfolio_lock.os, "name", "nt")
    monkeypatch.setattr(portfolio_lock.os, "kill", fail_os_kill)
    monkeypatch.setattr(portfolio_lock, "_win32_process_is_alive", lambda checked_pid: checked_pid == pid)

    assert PortfolioDataLock._process_is_alive(pid) is True
    assert kill_calls == []


def test_status_dashboard_formats_online_warmup_and_dead():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now),
        "phase": "online",
        "detail": "online",
        "events_fetched": 3,
        "raw_markets": 5,
        "tradable_markets": 2,
        "tokens": 4,
        "cache_path": "data/market_universe_cache.json",
        "cache_fetched_at_utc": utc_iso(now),
        "last_evaluation_reason": "ws_bootstrap",
        "last_cycle_completed_at_utc": utc_iso(now),
        "last_cycle_evaluated_markets": 2,
        "last_cycle_executions": 1,
        "last_cycle_skips": 1,
    }
    portfolio_status = {
        "cash": 1001.0,
        "realized_pnl": 1.0,
        "total_equity": 1001.0,
        "return_pct": 0.1,
        "trade_count": 1,
        "win_rate_pct": 100.0,
        "costs": {"fees_usd": 0.0, "slippage_usd": 0.1, "tax_usd": 0.0, "merge_usd": 0.02},
        "last_execution_at_utc": utc_iso(now),
        "unmatched_inventory": [],
    }

    online = format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)
    warmup = format_status_dashboard(runtime={**runtime, "phase": "warmup"}, portfolio=portfolio_status, now=now)
    dead = format_status_dashboard(runtime=None, portfolio=portfolio_status, now=now)

    assert dashboard_rows(online)[:2] == ["Paper Portfolio Status", "Current: ONLINE"]
    assert "Last refreshed: 2026-06-10T12:00:00Z" in online
    assert "Last cycle: reason=ws_bootstrap; completed=2026-06-10T12:00:00Z; evaluated=2; executions=1; skips=1" in online
    assert dashboard_rows(warmup)[:2] == ["Paper Portfolio Status", "Current: WARMUP"]
    assert dashboard_rows(dead)[:2] == ["Paper Portfolio Status", "Current: DEAD"]


def test_status_dashboard_formats_warmup_progress_eta():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now),
        "warmup_started_at_utc": utc_iso(now - timedelta(seconds=600)),
        "phase": "warmup",
        "detail": "seeding REST ask books: ws_bootstrap (250/1000 tokens)",
        "book_seed_reason": "ws_bootstrap",
        "book_seed_total_tokens": 1000,
        "book_seed_completed_tokens": 250,
        "book_seed_remaining_tokens": 750,
        "book_seed_received_books": 240,
        "book_seed_failed_tokens": 10,
        "book_seed_rate_tokens_per_second": 50.0,
        "book_seed_eta_seconds": 15.0,
    }
    portfolio_status = {
        "cash": 1000.0,
        "realized_pnl": 0.0,
        "total_equity": 1000.0,
        "return_pct": 0.0,
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "costs": {},
        "last_execution_at_utc": None,
        "unmatched_inventory": [],
    }

    dashboard = format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)

    assert (
        "Warmup progress: elapsed=10.0m; "
        "book_seed=ws_bootstrap 250/1,000 tokens (25.0%); "
        "remaining=750; received_books=240; failed=10; rate=50.0 tokens/s; ETA=15.0s"
    ) in dashboard
    assert "Runtime status writes:" not in dashboard


def test_status_dashboard_surfaces_inflight_book_seed_batch():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now),
        "warmup_started_at_utc": utc_iso(now - timedelta(seconds=600)),
        "phase": "warmup",
        "detail": "seeding REST ask books: ws_bootstrap (250/1000 tokens)",
        "book_seed_reason": "ws_bootstrap",
        "book_seed_total_tokens": 1000,
        "book_seed_completed_tokens": 250,
        "book_seed_remaining_tokens": 750,
        "book_seed_received_books": 240,
        "book_seed_failed_tokens": 10,
        "book_seed_rate_tokens_per_second": 50.0,
        "book_seed_eta_seconds": 15.0,
        "book_seed_batch_number": 2,
        "book_seed_total_batches": 4,
        "book_seed_batch_start_token": 251,
        "book_seed_batch_end_token": 500,
        "book_seed_batch_status": "in_flight",
        "book_seed_batch_started_at_utc": utc_iso(now - timedelta(seconds=18)),
    }
    portfolio_status = {
        "cash": 1000.0,
        "realized_pnl": 0.0,
        "total_equity": 1000.0,
        "return_pct": 0.0,
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "costs": {},
        "last_execution_at_utc": None,
        "unmatched_inventory": [],
    }

    dashboard = format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)

    assert "batch=2/4 tokens 251-500 in_flight=18.0s" in dashboard


def test_status_dashboard_surfaces_runtime_status_write_degradation():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now),
        "phase": "warmup",
        "detail": "seeding REST ask books: ws_bootstrap (250/1000 tokens)",
        "runtime_status_write_failures": 2,
        "last_runtime_status_write_error": "PermissionError: temporarily locked",
    }
    portfolio_status = {
        "cash": 1000.0,
        "realized_pnl": 0.0,
        "total_equity": 1000.0,
        "return_pct": 0.0,
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "costs": {},
        "last_execution_at_utc": None,
        "unmatched_inventory": [],
    }

    dashboard = format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)

    assert "Runtime status writes: failures=2; last_error=PermissionError: temporarily locked" in dashboard


def test_status_dashboard_treats_win32_alive_pid_as_online(monkeypatch):
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    pid = os.getpid() + 100_000

    def fail_os_kill(_pid, _signal):
        raise OSError(87, "parameter incorrect")

    monkeypatch.setattr(portfolio_lock.os, "name", "nt")
    monkeypatch.setattr(portfolio_lock.os, "kill", fail_os_kill)
    monkeypatch.setattr(portfolio_lock, "_win32_process_is_alive", lambda checked_pid: checked_pid == pid)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": pid,
        "heartbeat_at_utc": utc_iso(now),
        "phase": "online",
        "detail": "online",
    }
    portfolio_status = {
        "cash": 1000.0,
        "realized_pnl": 0.0,
        "total_equity": 1000.0,
        "return_pct": 0.0,
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "costs": {},
        "last_execution_at_utc": None,
        "unmatched_inventory": [],
    }

    dashboard = format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)

    assert dashboard_rows(dashboard)[:2] == ["Paper Portfolio Status", "Current: ONLINE"]


def test_status_dashboard_labels_stale_runtime_phase_as_last_known():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now - timedelta(seconds=16)),
        "phase": "warmup",
        "detail": "warming cache",
    }
    portfolio_status = {
        "cash": 1000.0,
        "realized_pnl": 0.0,
        "total_equity": 1000.0,
        "return_pct": 0.0,
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "costs": {},
        "last_execution_at_utc": None,
        "unmatched_inventory": [],
    }

    dashboard = format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)

    assert dashboard_rows(dashboard)[:2] == ["Paper Portfolio Status", "Current: DEAD"]
    assert "Heartbeat: 16.0s ago (stale); last-known phase=warmup; last-known detail=warming cache" in dashboard
    assert "Heartbeat: 16.0s ago; phase=warmup; warming cache" not in dashboard


def test_status_dashboard_uses_one_live_status_value_and_hides_history_by_default():
    now = datetime(2026, 6, 10, 12, tzinfo=timezone.utc)
    portfolio_status = {
        "cash": 1000.0,
        "realized_pnl": 0.0,
        "total_equity": 1000.0,
        "return_pct": 0.0,
        "trade_count": 0,
        "win_rate_pct": 0.0,
        "costs": {},
        "last_execution_at_utc": None,
        "unmatched_inventory": [],
    }
    base_runtime = {
        "schema_version": 1,
        "host": socket.gethostname(),
        "pid": os.getpid(),
        "heartbeat_at_utc": utc_iso(now),
        "phase": "online",
        "detail": "online",
    }
    polls = [
        {**base_runtime, "statusEntries": ["warming cache"]},
        {**base_runtime, "statusEntries": ["warming cache", "online"]},
        {**base_runtime, "statusEntries": ["warming cache", "online", "reconciling"]},
    ]

    frames = [
        format_status_dashboard(runtime=runtime, portfolio=portfolio_status, now=now)
        for runtime in polls
    ]

    assert dashboard_rows(frames[0])[:2] == ["Paper Portfolio Status", "Current: warming cache"]
    assert dashboard_rows(frames[-1])[:2] == ["Paper Portfolio Status", "Current: reconciling"]
    assert all(frame.count("Current:") == 1 for frame in frames)
    assert all("Status Log" not in frame for frame in frames)
    assert "warming cache" not in frames[-1]
    assert "ONLINE" not in frames[-1]
    assert len({len(frame.splitlines()) for frame in frames}) == 1

    with_log = format_status_dashboard(
        runtime=polls[-1],
        portfolio=portfolio_status,
        now=now,
        show_log=True,
    )

    assert "Status Log" in with_log
    assert "- warming cache" in with_log
    assert "- ONLINE" in with_log
    assert "- reconciling" in with_log


def test_status_watch_loop_can_be_bounded():
    rendered: list[str] = []
    sleeps: list[float] = []

    run_status_watch(
        render=lambda: "snapshot",
        refresh_seconds=0.2,
        output=rendered.append,
        sleep=sleeps.append,
        iterations=2,
    )

    assert rendered == ["\x1b[2J\x1b[Hsnapshot", "\x1b[2J\x1b[Hsnapshot"]
    assert all(frame.count("snapshot") == 1 for frame in rendered)
    assert sleeps == [0.2]


def test_status_watch_windows_live_terminal_uses_cls_without_ansi(monkeypatch):
    class FakeStdout:
        def __init__(self):
            self.frames: list[str] = []
            self.flushed = 0

        def write(self, text: str) -> None:
            self.frames.append(text)

        def flush(self) -> None:
            self.flushed += 1

        @staticmethod
        def isatty() -> bool:
            return True

    fake_stdout = FakeStdout()
    clears: list[str] = []
    monkeypatch.setattr(runtime_status.os, "name", "nt")
    monkeypatch.setattr(runtime_status.sys, "stdout", fake_stdout)

    run_status_watch(
        render=lambda: "snapshot",
        refresh_seconds=0.2,
        sleep=lambda _seconds: None,
        iterations=1,
        clear_screen=clears.append,
    )

    assert clears == ["cls"]
    assert fake_stdout.frames == ["snapshot"]
    assert fake_stdout.flushed == 1


def test_rest_cycle_retries_and_recovers_after_book_failure(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    client = FlakyBookClient(failures=1)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=client,
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=logging.getLogger("test_rest_cycle_retry"),
        params=params,
        retry_policy=ScannerRetryPolicy(initial_backoff_seconds=0.0, max_attempts=3),
    )
    scanner.bootstrap()

    with caplog.at_level(logging.INFO, logger="test_rest_cycle_retry"):
        result = scanner.run_one_cycle()

    assert client.fetch_ask_books_calls == 2
    assert result["summary"]["executions"] == 2
    assert "scanner_retry operation=rest_book_fetch attempt=1" in caplog.text
    assert "scanner_recovered operation=rest_book_fetch attempts=2" in caplog.text


def test_rest_cycle_does_not_retry_after_portfolio_side_effect(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    original_append_event = scanner.portfolio.append_event

    def fail_cycle_completed(event_type, payload=None, **fields):
        if event_type == "paper_portfolio_cycle_completed":
            raise RuntimeError("cycle completion log failed")
        return original_append_event(event_type, payload, **fields)

    scanner.portfolio.append_event = fail_cycle_completed

    with pytest.raises(RuntimeError, match="cycle completion log failed"):
        scanner.run_one_cycle()

    assert client.fetch_ask_books_calls == 1
    assert len(scanner.portfolio.state["executions"]) == 2


def test_websocket_startup_market_fetch_retries(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    client = FlakyEventClient(failures=1)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=client,
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=logging.getLogger("test_market_universe_retry"),
        params=params,
        retry_policy=ScannerRetryPolicy(initial_backoff_seconds=0.0, max_attempts=3),
    )

    with caplog.at_level(logging.INFO, logger="test_market_universe_retry"):
        universe = asyncio.run(scanner._fetch_market_universe_with_retry())

    assert client.fetch_active_events_calls == 2
    assert len(universe.markets) == 1
    assert "scanner_retry operation=market_universe_fetch attempt=1" in caplog.text
    assert "scanner_recovered operation=market_universe_fetch attempts=2" in caplog.text


def test_websocket_bootstrap_rest_seed_retries(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    client = FlakyBookClient(failures=1)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=client,
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=logging.getLogger("test_rest_seed_retry"),
        params=params,
        retry_policy=ScannerRetryPolicy(initial_backoff_seconds=0.0, max_attempts=3),
    )
    cache = MarketDataCache()

    with caplog.at_level(logging.INFO, logger="test_rest_seed_retry"):
        updated = asyncio.run(
            scanner._seed_rest_books_with_retry(
                cache,
                ["yes-1", "no-1"],
                reason="ws_bootstrap",
            )
        )

    assert client.fetch_ask_books_calls == 2
    assert updated == {"yes-1", "no-1"}
    assert "scanner_retry operation=rest_book_seed attempt=1" in caplog.text
    assert "scanner_recovered operation=rest_book_seed attempts=2" in caplog.text


def test_websocket_bootstrap_rest_seed_ignores_runtime_progress_write_failure(tmp_path, caplog):
    cfg = scan_config(tmp_path)
    params = PaperPortfolioParams.from_config(cfg)
    client = TwoMarketClient()
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=client,
        portfolio=PaperPortfolio(
            cfg.paper_portfolio_instance_path,
            events_path=cfg.paper_portfolio_events_path,
            params=params,
        ),
        logger=logging.getLogger("test_runtime_status_seed"),
        params=params,
        retry_policy=ScannerRetryPolicy(initial_backoff_seconds=0.0, max_attempts=3),
    )
    scanner.bootstrap()
    scanner._runtime_started = True
    scanner.runtime.write_retry_attempts = 1
    scanner.runtime.write_retry_backoff_seconds = 0.0
    cache = MarketDataCache()
    original_write_once = scanner.runtime._write_once_locked
    write_calls = 0

    def flaky_write_once():
        nonlocal write_calls
        write_calls += 1
        if write_calls == 2:
            raise PermissionError("runtime file is temporarily locked")
        original_write_once()

    scanner.runtime._write_once_locked = flaky_write_once

    with caplog.at_level(logging.INFO, logger="test_runtime_status_seed"):
        updated = asyncio.run(
            scanner._seed_rest_books_with_retry(
                cache,
                ["yes-1", "no-1"],
                reason="ws_bootstrap",
            )
        )

    runtime = json.loads(cfg.paper_portfolio_runtime_path.read_text(encoding="utf-8"))
    assert client.fetch_ask_books_calls == 1
    assert updated == {"yes-1", "no-1"}
    assert cache.ask_books_snapshot(["yes-1", "no-1"]).keys() == {"yes-1", "no-1"}
    assert "scanner_retry operation=rest_book_seed" not in caplog.text
    assert "runtime_status_write_failed failures=1 error=PermissionError: runtime file is temporarily locked" in caplog.text
    assert scanner.runtime.snapshot()["runtime_status_write_failures"] == 1
    assert runtime["runtime_status_write_failures"] == 1
    assert runtime["last_runtime_status_write_error"] == "PermissionError: runtime file is temporarily locked"


def test_cli_help_smoke(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    output = capsys.readouterr().out
    assert "paper Polymarket" in output
    assert "--once" not in output
    assert "--limit" not in output
    assert "--json" not in output
    assert "--no-market-ws" not in output
    assert "--no-neg-risk" not in output


def test_portfolio_lock_rejects_second_active_holder(tmp_path):
    state_path = tmp_path / "paper_portfolio_instance.json"
    first = PortfolioDataLock(state_path).acquire()
    try:
        with pytest.raises(PortfolioLockError, match="paper portfolio data is locked"):
            PortfolioDataLock(state_path).acquire()
    finally:
        first.release()


def test_portfolio_lock_keeps_win32_alive_same_host_lock(tmp_path, monkeypatch):
    state_path = tmp_path / "paper_portfolio_instance.json"
    lock_path = state_path.with_name(state_path.name + ".lock")
    pid = os.getpid() + 100_000

    def fail_os_kill(_pid, _signal):
        raise OSError(87, "parameter incorrect")

    lock_path.write_text(
        json.dumps(
            {
                "host": socket.gethostname(),
                "pid": pid,
                "token": "active",
                "created_at_utc": "2026-06-08T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(portfolio_lock.os, "name", "nt")
    monkeypatch.setattr(portfolio_lock.os, "kill", fail_os_kill)
    monkeypatch.setattr(portfolio_lock, "_win32_process_is_alive", lambda checked_pid: checked_pid == pid)

    with pytest.raises(PortfolioLockError, match="paper portfolio data is locked"):
        PortfolioDataLock(state_path).acquire()

    assert json.loads(lock_path.read_text(encoding="utf-8"))["token"] == "active"


def test_portfolio_lock_repeated_contention_retries_transient_unlink_failures(tmp_path, monkeypatch):
    state_path = tmp_path / "paper_portfolio_instance.json"
    lock_path = state_path.with_name(state_path.name + ".lock")
    real_unlink = Path.unlink
    remaining_permission_errors = 4
    unlink_guard = threading.Lock()

    def flaky_unlink(path: Path, *args, **kwargs):
        nonlocal remaining_permission_errors
        if path == lock_path:
            with unlink_guard:
                if remaining_permission_errors > 0:
                    remaining_permission_errors -= 1
                    raise PermissionError("simulated transient lock contention")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    active_holders = 0
    max_active_holders = 0
    holder_guard = threading.Lock()
    start = threading.Barrier(4)

    def worker() -> None:
        nonlocal active_holders, max_active_holders
        start.wait()
        acquired = 0
        attempts = 0
        while acquired < 8:
            attempts += 1
            if attempts > 400:
                raise AssertionError("could not acquire portfolio lock under contention")
            lock = PortfolioDataLock(state_path)
            try:
                lock.acquire()
            except PortfolioLockError:
                time.sleep(0.001)
                continue
            try:
                with holder_guard:
                    active_holders += 1
                    max_active_holders = max(max_active_holders, active_holders)
                    if active_holders != 1:
                        raise AssertionError("overlapping portfolio lock holders")
                time.sleep(0.0005)
            finally:
                with holder_guard:
                    active_holders -= 1
                lock.release()
            acquired += 1

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(worker) for _ in range(4)]
        for future in futures:
            future.result(timeout=10)

    assert max_active_holders == 1
    assert remaining_permission_errors == 0
    assert not lock_path.exists()


def test_portfolio_lock_recovers_dead_same_host_lock(tmp_path, monkeypatch):
    state_path = tmp_path / "paper_portfolio_instance.json"
    lock_path = state_path.with_name(state_path.name + ".lock")
    lock_path.write_text(
        json.dumps(
            {
                "host": socket.gethostname(),
                "pid": 999999,
                "token": "old",
                "created_at_utc": "2026-06-08T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(PortfolioDataLock, "_process_is_alive", staticmethod(lambda _pid: False))

    with PortfolioDataLock(state_path):
        assert lock_path.exists()

    assert not lock_path.exists()


def test_cli_status_reads_state_without_lock_or_mutation(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COND_ARB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COND_ARB_LOG_DIR", str(tmp_path / "logs"))
    state_path = config.paper_portfolio_instance_path(data_dir)
    params = PaperPortfolioParams.from_config(scan_config(tmp_path))
    portfolio = PaperPortfolio(
        state_path,
        events_path=config.paper_portfolio_events_path(data_dir),
        params=params,
    )
    portfolio.reset(yes=True)
    before = state_path.read_text(encoding="utf-8")
    lock = PortfolioDataLock(state_path).acquire()
    try:
        main(["status", "--once"])
    finally:
        lock.release()
    after = state_path.read_text(encoding="utf-8")

    assert "Current: DEAD" in capsys.readouterr().out
    assert before == after


def test_cli_status_reports_corrupt_state_without_traceback(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COND_ARB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COND_ARB_LOG_DIR", str(tmp_path / "logs"))
    state_path = config.paper_portfolio_instance_path(data_dir)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["status", "--once"])

    captured = capsys.readouterr()
    assert excinfo.value.code == 2
    assert "failed to load paper portfolio" in captured.err
    assert "Traceback" not in captured.err
    assert captured.out == ""
    assert not state_path.with_name(state_path.name + ".lock").exists()


def test_cli_reset_acquires_lock_and_writes_clean_state(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COND_ARB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COND_ARB_LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setenv("COND_ARB_STARTING_CAPITAL_USD", "1234")

    main(["reset", "--yes"])

    state_path = config.paper_portfolio_instance_path(data_dir)
    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["cash"] == 1234.0
    assert state["executions"] == []
    assert not state_path.with_name(state_path.name + ".lock").exists()
    assert "Paper portfolio reset" in capsys.readouterr().out


def test_cli_run_fails_fast_when_portfolio_lock_exists(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COND_ARB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COND_ARB_LOG_DIR", str(tmp_path / "logs"))
    state_path = config.paper_portfolio_instance_path(data_dir)
    lock = PortfolioDataLock(state_path).acquire()
    try:
        with pytest.raises(SystemExit) as excinfo:
            main(["run"])
    finally:
        lock.release()

    assert excinfo.value.code == 2
    assert "paper portfolio data is locked" in capsys.readouterr().err


def test_scanner_package_has_no_live_order_or_auth_imports():
    package_root = Path(__file__).resolve().parents[1] / "polymarket_conditional_arb"
    text = "\n".join(path.read_text(encoding="utf-8") for path in package_root.glob("*.py"))

    banned = [
        "order_placer",
        "py_clob_client",
        "py-clob-client",
        "POLYMARKET_PRIVATE_KEY",
        "POLYMARKET_API_SECRET",
        "POLYMARKET_API_PASSPHRASE",
        "MERGE_ARB_LIVE_TRADING_ENABLED",
    ]
    for needle in banned:
        assert needle not in text


def test_dirty_token_update_evaluates_only_its_market(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    universe = scanner._fetch_market_universe()
    cache = MarketDataCache()
    cache.seed_ask_books(profitable_books(universe.token_ids, updated_at=datetime.now(timezone.utc)))

    result = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=scanner.params,
    )

    assert result["summary"]["evaluated_standard_binary_markets"] == 1
    assert result["executions"][0]["market_id"] == "m1"


def test_websocket_dirty_tick_does_not_fetch_rest_books(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    universe = scanner._fetch_market_universe()
    cache = MarketDataCache()
    cache.seed_ask_books(profitable_books(universe.token_ids, updated_at=datetime.now(timezone.utc)))
    client.fetch_ask_books_calls = 0

    scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=scanner.params,
    )

    assert client.fetch_ask_books_calls == 0


def test_stale_websocket_cache_skips_opportunities(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    universe = scanner._fetch_market_universe()
    cache = MarketDataCache()
    cache.seed_ask_books(
        profitable_books(universe.token_ids, updated_at=datetime.now(timezone.utc) - timedelta(seconds=6))
    )
    ws_params = PaperPortfolioParams(
        starting_capital_usd=1000.0,
        trade_ceiling_usd=20.0,
        slippage_buffer_bps=0.0,
        taker_fee_bps=0.0,
        tax_bps=0.0,
        merge_cost_usd=0.0,
        max_book_age_seconds=5.0,
    )

    result = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=ws_params,
    )

    assert result["summary"]["executions"] == 0
    assert result["summary"]["skip_counts"]["stale_book"] == 1


def test_rest_reconciliation_refreshes_stale_books_and_restores_evaluation(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    universe = scanner._fetch_market_universe()
    cache = MarketDataCache()
    cache.seed_ask_books(
        profitable_books(universe.token_ids, updated_at=datetime.now(timezone.utc) - timedelta(seconds=6))
    )
    ws_params = PaperPortfolioParams(
        starting_capital_usd=1000.0,
        trade_ceiling_usd=20.0,
        slippage_buffer_bps=0.0,
        taker_fee_bps=0.0,
        tax_bps=0.0,
        merge_cost_usd=0.0,
        max_book_age_seconds=5.0,
    )

    updated = asyncio.run(scanner._seed_rest_books(cache, universe.token_ids, reason="test_reconcile"))
    result = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids=set(updated),
        evaluation_reason="rest_reconcile",
        params=ws_params,
    )

    assert client.fetch_ask_books_calls == 1
    assert result["summary"]["executions"] == 2


def test_unchanged_book_fingerprint_does_not_duplicate_paper_execution(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    universe = scanner._fetch_market_universe()
    cache = MarketDataCache()
    updated_at = datetime.now(timezone.utc)
    cache.seed_ask_books(profitable_books(universe.token_ids, updated_at=updated_at))

    first = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=scanner.params,
    )
    second = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=scanner.params,
    )

    assert first["summary"]["executions"] == 1
    assert second["summary"]["executions"] == 0
    assert second["summary"]["skip_counts"]["unchanged_book_snapshot"] == 1


def test_changed_book_depth_allows_new_paper_execution(tmp_path):
    client = TwoMarketClient()
    scanner = scanner_for(tmp_path, client)
    universe = scanner._fetch_market_universe()
    cache = MarketDataCache()
    first_updated_at = datetime.now(timezone.utc)
    cache.seed_ask_books(profitable_books(universe.token_ids, updated_at=first_updated_at))

    first = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=scanner.params,
    )
    second_updated_at = datetime.now(timezone.utc)
    cache.seed_ask_books(
        {
            "yes-1": asks_from_book(
                {"asks": [{"price": "0.47", "size": "10"}]},
                token_id="yes-1",
                updated_at=second_updated_at,
            ),
            "no-1": asks_from_book(
                {"asks": [{"price": "0.49", "size": "10"}]},
                token_id="no-1",
                updated_at=second_updated_at,
            ),
        }
    )
    second = scanner._evaluate_from_cache(
        universe,
        cache,
        dirty_token_ids={"yes-1"},
        evaluation_reason="ws_dirty_update",
        params=scanner.params,
    )

    assert first["summary"]["executions"] == 1
    assert second["summary"]["executions"] == 1
    assert second["executions"][0]["yes_vwap"] == pytest.approx(0.47)


def test_parser_public_surface_uses_portfolio_commands():
    parser = build_parser()
    cfg = _config_from_args(parser.parse_args(["status"]))

    assert cfg.include_neg_risk is False
