from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from polymarket_conditional_arb import config
from polymarket_conditional_arb.fetcher import GammaClobClient
from polymarket_conditional_arb.market_data import MarketDataCache
from polymarket_conditional_arb.order_book import asks_from_book
from polymarket_conditional_arb.paper import PaperPortfolio, PaperPortfolioParams
from polymarket_conditional_arb.scan_bot import ConditionalArbScanner, _config_from_args, build_parser, main


class FakeClient:
    def fetch_active_events(self):
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

    def fetch_active_events(self):
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
