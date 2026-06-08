from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from weather_arb_live.arb_models import BinaryMarket
from weather_arb_live.arb_strategy import ArbStrategyParams, evaluate_binary_merge_arbitrage
from weather_arb_live.order_book import asks_from_book
from weather_arb_live.paper import PaperMergeLedger, PaperTradingEngine


AS_OF = datetime(2026, 6, 8, 12, tzinfo=timezone.utc)


def opportunity():
    market = BinaryMarket.from_gamma_market(
        {
            "id": "m1",
            "conditionId": "c1",
            "question": "Will X happen?",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
        }
    )
    assert market is not None
    params = ArbStrategyParams(
        min_net_profit_usd=0.0,
        min_net_return_bps=0.0,
        max_paper_position_usd=10.0,
        slippage_buffer_bps=0.0,
        gas_cost_usd=0.0,
        taker_fee_bps=0.0,
        max_book_age_seconds=20.0,
    )
    decision = evaluate_binary_merge_arbitrage(
        market,
        asks_from_book({"asks": [{"price": "0.48", "size": "10"}]}, token_id="yes-token", updated_at=AS_OF),
        asks_from_book({"asks": [{"price": "0.49", "size": "10"}]}, token_id="no-token", updated_at=AS_OF),
        as_of=AS_OF,
        params=params,
    )
    assert decision.opportunity is not None
    return decision.opportunity


def test_paper_engine_records_paired_fills_and_immediate_merge():
    path = Path("data/test_paper_positions.json")
    path.unlink(missing_ok=True)
    try:
        ledger = PaperMergeLedger(path).load()
        row = PaperTradingEngine(ledger).execute(opportunity(), as_of=AS_OF)

        assert row["status"] == "merged"
        assert row["merged_quantity"] == 10.0
        assert row["unmerged_quantity"] == 0.0
        assert row["paired_fills"][0]["outcome"] == "YES"
        assert row["paired_fills"][1]["outcome"] == "NO"
        assert row["realized_pnl"] == pytest.approx(10 - 4.8 - 4.9)

        loaded = PaperMergeLedger(path).load()
        assert loaded.positions["m1"]["condition_id"] == "c1"
    finally:
        path.unlink(missing_ok=True)


def test_paper_ledger_rejects_duplicate_market():
    path = Path("data/test_paper_duplicate_positions.json")
    path.unlink(missing_ok=True)
    try:
        ledger = PaperMergeLedger(path).load()
        engine = PaperTradingEngine(ledger)
        engine.execute(opportunity(), as_of=AS_OF)

        with pytest.raises(ValueError, match="already exists"):
            engine.execute(opportunity(), as_of=AS_OF)
    finally:
        path.unlink(missing_ok=True)

