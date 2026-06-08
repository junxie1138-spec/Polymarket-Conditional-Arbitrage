from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

import pytest

from weather_arb_live.arb_strategy import ArbStrategyParams
from weather_arb_live.event_log import LiveEventLog
from weather_arb_live.merge_arb_bot import BinaryMergeArbBot, LiveTradingDisabledError
from weather_arb_live.order_book import asks_from_book
from weather_arb_live.paper import PaperMergeLedger


class FakeFetcher:
    def __init__(self):
        self.requested_tag_slug = "unset"
        self.book_fetches = 0

    def fetch_active_markets(self, *, tag_slug=None, limit=None):
        self.requested_tag_slug = tag_slug
        return [
            {
                "id": "m1",
                "conditionId": "c1",
                "question": "Will X happen?",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
            }
        ]

    def fetch_binary_ask_books(self, market):
        self.book_fetches += 1
        now = datetime.now(timezone.utc)
        return (
            asks_from_book({"asks": [{"price": "0.48", "size": "10"}]}, token_id=market.yes_token_id, updated_at=now),
            asks_from_book({"asks": [{"price": "0.49", "size": "10"}]}, token_id=market.no_token_id, updated_at=now),
        )


def event_log_for(name: str) -> LiveEventLog:
    base = Path("data/test_merge_arb_events") / name
    for path in (base / "events.jsonl", base / "markets.jsonl", base / "forecasts.jsonl"):
        path.unlink(missing_ok=True)
    return LiveEventLog(
        event_path=base / "events.jsonl",
        market_snapshot_path=base / "markets.jsonl",
        forecast_snapshot_path=base / "forecasts.jsonl",
    )


def test_merge_arb_bot_scans_all_markets_and_records_paper_position(monkeypatch):
    monkeypatch.setenv("MERGE_ARB_LIVE_TRADING_ENABLED", "false")
    ledger_path = Path("data/test_merge_arb_bot_positions.json")
    ledger_path.unlink(missing_ok=True)
    logger = logging.getLogger("test_merge_arb_bot")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    fetcher = FakeFetcher()
    try:
        bot = BinaryMergeArbBot(
            fetcher=fetcher,
            ledger=PaperMergeLedger(ledger_path),
            logger=logger,
            event_log=event_log_for("paper_flow"),
            params=ArbStrategyParams(
                min_net_profit_usd=0.0,
                min_net_return_bps=0.0,
                max_paper_position_usd=10.0,
                slippage_buffer_bps=0.0,
                gas_cost_usd=0.0,
                taker_fee_bps=0.0,
                max_book_age_seconds=20.0,
            ),
        )

        assert bot.run_once() is True

        assert fetcher.requested_tag_slug is None
        assert fetcher.book_fetches == 1
        loaded = PaperMergeLedger(ledger_path).load()
        assert loaded.positions["m1"]["status"] == "merged"
    finally:
        ledger_path.unlink(missing_ok=True)


def test_merge_arb_live_mode_fails_closed(monkeypatch):
    monkeypatch.setenv("MERGE_ARB_LIVE_TRADING_ENABLED", "true")
    logger = logging.getLogger("test_merge_arb_live_disabled")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = BinaryMergeArbBot(
        fetcher=FakeFetcher(),
        ledger=PaperMergeLedger(Path("data/test_merge_arb_live_disabled.json")),
        logger=logger,
        event_log=event_log_for("live_disabled"),
        params=ArbStrategyParams(
            min_net_profit_usd=0.0,
            min_net_return_bps=0.0,
            max_paper_position_usd=10.0,
            slippage_buffer_bps=0.0,
            gas_cost_usd=0.0,
            taker_fee_bps=0.0,
            max_book_age_seconds=20.0,
        ),
    )

    with pytest.raises(LiveTradingDisabledError, match="paper mode only"):
        bot.bootstrap()
