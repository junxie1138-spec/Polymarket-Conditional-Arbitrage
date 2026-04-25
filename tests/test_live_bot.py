from __future__ import annotations

import logging
from pathlib import Path

from weather_arb_live.ledger import PositionLedger
from weather_arb_live.live_bot import LiveBot
from weather_arb_live.order_placer import OrderResult, build_order_intent
from weather_arb_live.strategy import TradePlan, Decision


class FakeFetcher:
    def fetch_active_markets(self, **_kwargs):
        return [{"id": "m1", "conditionId": "c1"}]

    def fetch_midpoint(self, token_id):
        assert token_id == "yes-token"
        return 0.30


class FakeOrderPlacer:
    def place_order(self, *, token_id, market_price, position_usd=None):
        intent = build_order_intent(
            token_id=token_id,
            market_price=market_price,
            position_usd=position_usd,
            dry_run=True,
        )
        return OrderResult(intent=intent, posted=False, response={"dry_run": True})


def test_run_one_cycle_records_dry_run_position(monkeypatch):
    path = Path("data/test_bot_positions.json")
    if path.exists():
        path.unlink()
    plan = TradePlan(
        market_id="m1",
        token_id="yes-token",
        side="YES",
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
        entry_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def fake_evaluate(_market, current_price, **_kwargs):
        if current_price is None:
            return Decision.skip("missing_live_price", market_id="m1", token_id="yes-token")
        return Decision.enter(plan)

    monkeypatch.setattr("weather_arb_live.live_bot.evaluate_market", fake_evaluate)
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)

    logger = logging.getLogger("test_live_bot")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(path).load(),
        logger=logger,
    )
    bot.calibration = None

    try:
        bot.run_one_cycle()

        assert "m1" in bot.ledger.positions
        loaded = PositionLedger(path).load()
        assert loaded.positions["m1"]["dry_run"] is True
    finally:
        if path.exists():
            path.unlink()


class FakeNoSideFetcher:
    def fetch_active_markets(self, **_kwargs):
        return [{"id": "m2", "conditionId": "c2"}]

    def fetch_midpoint(self, token_id):
        return {"yes-token": 0.60, "no-token": 0.30}[token_id]


class FakeNoSideOrderPlacer:
    def place_order(self, *, token_id, market_price, position_usd=None):
        intent = build_order_intent(
            token_id=token_id,
            market_price=market_price,
            position_usd=position_usd,
            dry_run=True,
        )
        return OrderResult(intent=intent, posted=False, response={"dry_run": True})


def test_run_one_cycle_falls_back_to_no_side(monkeypatch):
    path = Path("data/test_bot_no_positions.json")
    if path.exists():
        path.unlink()
    no_plan = TradePlan(
        market_id="m2",
        token_id="no-token",
        side="NO",
        question="Q",
        city="New York",
        target_date="2026-04-27",
        market_price=0.30,
        entry_price=0.3015,
        shares=165.837,
        position_usd=50.0,
        forecast_prob=0.80,
        edge=0.4985,
        lead_days=3,
        entry_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def fake_evaluate(_market, current_price, **kwargs):
        side = kwargs.get("side", "YES")
        if current_price is None:
            token_id = "yes-token" if side == "YES" else "no-token"
            return Decision.skip("missing_live_price", market_id="m2", token_id=token_id, side=side)
        if side == "YES":
            return Decision.skip("below_min_edge", market_id="m2", side="YES")
        return Decision.enter(no_plan)

    monkeypatch.setattr("weather_arb_live.live_bot.evaluate_market", fake_evaluate)
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)

    logger = logging.getLogger("test_live_bot_no")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=FakeNoSideFetcher(),
        order_placer=FakeNoSideOrderPlacer(),
        ledger=PositionLedger(path).load(),
        logger=logger,
    )
    bot.calibration = None

    try:
        bot.run_one_cycle()

        assert "m2" in bot.ledger.positions
        assert bot.ledger.positions["m2"]["side"] == "NO"
        assert bot.ledger.positions["m2"]["token_id"] == "no-token"
    finally:
        if path.exists():
            path.unlink()
