from __future__ import annotations

import asyncio
import json
import logging
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polymarket_conditional_arb import config
from polymarket_conditional_arb.fetcher import GammaClobClient
from polymarket_conditional_arb.market_data import MarketDataCache
from polymarket_conditional_arb.order_book import asks_from_book
from polymarket_conditional_arb.paper import PaperPortfolio, PaperPortfolioParams
from polymarket_conditional_arb.portfolio_lock import PortfolioDataLock, PortfolioLockError
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
    def fetch_ask_books(_token_ids):
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

    def fetch_ask_books(self, token_ids):
        self.fetch_ask_books_calls += 1
        return profitable_books(token_ids, updated_at=datetime.now(timezone.utc))


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

    def fetch_ask_books(self, token_ids):
        self.fetch_ask_books_calls += 1
        if self.failures > 0:
            self.failures -= 1
            raise RuntimeError("books unavailable")
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


def scanner_for(tmp_path: Path, client):
    cfg = scan_config(tmp_path)
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
        main(["status"])
    finally:
        lock.release()
    after = state_path.read_text(encoding="utf-8")

    assert "Paper Portfolio Status" in capsys.readouterr().out
    assert before == after


def test_cli_status_reports_corrupt_state_without_traceback(tmp_path, monkeypatch, capsys):
    data_dir = tmp_path / "data"
    monkeypatch.setenv("COND_ARB_DATA_DIR", str(data_dir))
    monkeypatch.setenv("COND_ARB_LOG_DIR", str(tmp_path / "logs"))
    state_path = config.paper_portfolio_instance_path(data_dir)
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{not json", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        main(["status"])

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
