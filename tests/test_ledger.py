from datetime import datetime, timezone
from pathlib import Path

from weather_arb_live.ledger import PositionLedger
from weather_arb_live.strategy import TradePlan


def plan():
    return TradePlan(
        market_id="m1",
        token_id="yes-token",
        question="Q",
        city="New York",
        target_date="2026-04-27",
        market_price=0.30,
        entry_price=0.3015,
        shares=165.837,
        position_usd=50.0,
        forecast_prob=0.80,
        edge=0.50,
        lead_days=3,
        entry_time=datetime(2026, 4, 24, 12, tzinfo=timezone.utc),
    )


def test_ledger_persists_and_reloads():
    path = Path("data/test_live_positions.json")
    try:
        if path.exists():
            path.unlink()
        ledger = PositionLedger(path).load()
        ledger.record(plan(), dry_run=True, order_response={"dry_run": True})
        ledger.save()

        loaded = PositionLedger(path).load()

        assert "m1" in loaded.positions
        assert loaded.positions["m1"]["token_id"] == "yes-token"
        assert loaded.entered_positions(include_dry_run=True)
        assert loaded.entered_positions(include_dry_run=False) == {}
    finally:
        if path.exists():
            path.unlink()
