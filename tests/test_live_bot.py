from __future__ import annotations

import logging
from pathlib import Path

import requests

from weather_arb_live.ledger import PositionLedger
from weather_arb_live.live_bot import LiveBot
from weather_arb_live.order_placer import BalancePreflightError, OrderResult, build_order_intent
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


def test_record_entry_saves_position_immediately(monkeypatch):
    path = Path("data/test_bot_immediate_positions.json")
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
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)

    logger = logging.getLogger("test_live_bot_immediate")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(path).load(),
        logger=logger,
    )

    try:
        bot._record_entry(Decision.enter(plan), {})

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


class FakeMissingYesBookFetcher:
    def __init__(self):
        self.tokens: list[str] = []

    def fetch_active_markets(self, **_kwargs):
        return [{"id": "m2", "conditionId": "c2"}]

    def fetch_midpoint(self, token_id):
        self.tokens.append(token_id)
        return {"yes-token": None, "no-token": 0.30}[token_id]


def test_run_one_cycle_falls_back_to_no_side_when_yes_book_missing(monkeypatch):
    path = Path("data/test_bot_missing_yes_book_positions.json")
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
            raise AssertionError("YES should not be evaluated after a missing book")
        return Decision.enter(no_plan)

    monkeypatch.setattr("weather_arb_live.live_bot.evaluate_market", fake_evaluate)
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)

    logger = logging.getLogger("test_live_bot_missing_yes_book")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    fetcher = FakeMissingYesBookFetcher()
    bot = LiveBot(
        fetcher=fetcher,
        order_placer=FakeNoSideOrderPlacer(),
        ledger=PositionLedger(path).load(),
        logger=logger,
    )
    bot.calibration = None

    try:
        bot.run_one_cycle()

        assert fetcher.tokens == ["yes-token", "no-token"]
        assert "m2" in bot.ledger.positions
        assert bot.ledger.positions["m2"]["side"] == "NO"
    finally:
        if path.exists():
            path.unlink()


class OfflineFetcher:
    def fetch_active_markets(self, **_kwargs):
        raise ConnectionError("offline")


def test_run_one_cycle_reports_fetch_outage_without_crashing():
    logger = logging.getLogger("test_live_bot_offline")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=OfflineFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(Path("data/test_offline_positions.json")).load(),
        logger=logger,
    )

    assert bot.run_one_cycle() is False


class InterruptedOrderPlacer:
    def place_order(self, **_kwargs):
        raise requests.ConnectionError("response lost")


class BalancePreflightFailingOrderPlacer:
    def place_order(self, **_kwargs):
        raise BalancePreflightError("balance preflight failed")


def test_live_order_connection_loss_records_unknown_local_guard(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RECONCILE_ON_STARTUP", "false")
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)
    path = Path("data/test_unknown_order_positions.json")
    if path.exists():
        path.unlink()
    plan = TradePlan(
        market_id="m3",
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
        edge=0.4985,
        lead_days=3,
        entry_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def fake_evaluate(_market, current_price, **_kwargs):
        if current_price is None:
            return Decision.skip("missing_live_price", market_id="m3", token_id="yes-token")
        return Decision.enter(plan)

    monkeypatch.setattr("weather_arb_live.live_bot.evaluate_market", fake_evaluate)
    logger = logging.getLogger("test_live_bot_unknown_order")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=InterruptedOrderPlacer(),
        ledger=PositionLedger(path).load(),
        logger=logger,
    )
    bot.calibration = None

    try:
        assert bot.run_one_cycle() is True
        row = bot.ledger.positions["m3"]
        assert row["dry_run"] is False
        assert row["order_response"]["posted"] == "unknown"
        assert row["order_response"]["reason"] == "order_submission_interrupted"
    finally:
        if path.exists():
            path.unlink()


def test_live_balance_preflight_failure_does_not_record_unknown_guard(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RECONCILE_ON_STARTUP", "false")
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)
    path = Path("data/test_balance_preflight_positions.json")
    if path.exists():
        path.unlink()
    plan = TradePlan(
        market_id="m4",
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
        edge=0.4985,
        lead_days=3,
        entry_time=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
    )

    def fake_evaluate(_market, current_price, **_kwargs):
        if current_price is None:
            return Decision.skip("missing_live_price", market_id="m4", token_id="yes-token")
        return Decision.enter(plan)

    monkeypatch.setattr("weather_arb_live.live_bot.evaluate_market", fake_evaluate)
    logger = logging.getLogger("test_live_bot_balance_preflight")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=BalancePreflightFailingOrderPlacer(),
        ledger=PositionLedger(path).load(),
        logger=logger,
    )
    bot.calibration = None

    try:
        assert bot.run_one_cycle() is True
        assert "m4" not in bot.ledger.positions
    finally:
        if path.exists():
            path.unlink()


class UncalledFetcher:
    def fetch_active_markets(self, **_kwargs):
        raise AssertionError("market fetch should wait for reconciliation")


class FailingReconciler:
    def reconcile(self, **_kwargs):
        raise requests.ConnectionError("data api offline")


def test_live_mode_blocks_cycle_until_startup_reconcile_succeeds(monkeypatch):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RECONCILE_ON_STARTUP", "true")
    logger = logging.getLogger("test_live_bot_reconcile_blocks")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=UncalledFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(Path("data/test_reconcile_block_positions.json")).load(),
        logger=logger,
    )
    bot.reconciler = FailingReconciler()

    assert bot.run_one_cycle() is False
