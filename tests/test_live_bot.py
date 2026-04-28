from __future__ import annotations

import logging
import json
from datetime import datetime, timezone
from pathlib import Path

import requests
import pytest

from weather_arb_live.event_log import LiveEventLog
from weather_arb_live.ledger import PositionLedger
from weather_arb_live.live_fetcher import LiveQuote
from weather_arb_live.live_bot import LiveBot
from weather_arb_live.order_placer import BalancePreflightError, OrderResult, build_order_intent
from weather_arb_live.strategy import TradePlan, Decision
from weather_arb_live.ws_stream import unique_market_condition_ids, unique_market_token_ids


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


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def isolated_event_log(request):
    name = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in request.node.name)
    base_path = Path("data/test_event_logs") / name
    paths = {
        "event_path": base_path / "live_events.jsonl",
        "market_snapshot_path": base_path / "market_snapshots.jsonl",
        "forecast_snapshot_path": base_path / "forecast_snapshots.jsonl",
    }
    for path in paths.values():
        path.unlink(missing_ok=True)
    yield LiveEventLog(**paths)
    for path in paths.values():
        path.unlink(missing_ok=True)
    try:
        base_path.rmdir()
    except FileNotFoundError:
        pass


def test_run_one_cycle_records_dry_run_position(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "true")
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
        event_log=isolated_event_log,
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


def test_record_entry_saves_position_immediately(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "true")
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
        event_log=isolated_event_log,
    )

    try:
        bot._record_entry(Decision.enter(plan), {})

        loaded = PositionLedger(path).load()
        assert loaded.positions["m1"]["dry_run"] is True
    finally:
        if path.exists():
            path.unlink()


def test_record_entry_appends_signal_submit_and_ack_events(monkeypatch):
    monkeypatch.setattr("weather_arb_live.live_bot.flush_cache", lambda: None)
    events_path = Path("data/test_bot_events.jsonl")
    market_path = Path("data/test_bot_market_snapshots.jsonl")
    forecast_path = Path("data/test_bot_forecast_snapshots.jsonl")
    path = Path("data/test_bot_event_positions.json")
    for cleanup_path in (events_path, market_path, forecast_path, path):
        cleanup_path.unlink(missing_ok=True)
    event_log = LiveEventLog(
        event_path=events_path,
        market_snapshot_path=market_path,
        forecast_snapshot_path=forecast_path,
    )
    plan = TradePlan(
        market_id="m1",
        token_id="yes-token",
        side="YES",
        question="Will the highest temperature in New York be above 70F on April 27, 2026?",
        city="New York",
        target_date="2026-04-27",
        market_price=0.30,
        entry_price=0.3015,
        shares=165.837,
        position_usd=50.0,
        forecast_prob=0.80,
        edge=0.4985,
        lead_days=3,
        entry_time=__import__("datetime").datetime(2026, 4, 24, 12, tzinfo=__import__("datetime").timezone.utc),
        condition_id="c1",
        bracket_low=70.0,
        bracket_high=None,
        bracket_unit="F",
        metric="max",
    )
    logger = logging.getLogger("test_live_bot_events")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    try:
        bot = LiveBot(
            fetcher=FakeFetcher(),
            order_placer=FakeOrderPlacer(),
            ledger=PositionLedger(path).load(),
            logger=logger,
            market_stream=False,
            user_stream=False,
            event_log=event_log,
        )
        bot._quote_context[("m1", "YES")] = {
            "token_id": "yes-token",
            "best_bid": 0.29,
            "best_ask": 0.31,
            "midpoint": 0.30,
            "quote_source": "test",
        }

        bot._record_entry(Decision.enter(plan), {}, market={"id": "m1", "conditionId": "c1"})

        rows = _jsonl(events_path)
        assert [row["event_type"] for row in rows] == [
            "signal_generated",
            "order_submitted",
            "order_acknowledged",
        ]
        assert rows[0]["city"] == "New York"
        assert rows[0]["bracket"]["low"] == 70.0
        assert rows[0]["best_bid"] == 0.29
        assert round(rows[1]["submitted_limit_price"], 4) == 0.3015
        assert rows[2]["raw_order_response"] == {"dry_run": True}
    finally:
        for cleanup_path in (events_path, market_path, forecast_path, path):
            cleanup_path.unlink(missing_ok=True)


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


def test_run_one_cycle_falls_back_to_no_side(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "true")
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
        event_log=isolated_event_log,
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


def test_run_one_cycle_falls_back_to_no_side_when_yes_book_missing(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "true")
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
        event_log=isolated_event_log,
    )
    bot.calibration = None

    try:
        bot.run_one_cycle()

        assert fetcher.tokens[:2] == ["yes-token", "no-token"]
        assert "m2" in bot.ledger.positions
        assert bot.ledger.positions["m2"]["side"] == "NO"
    finally:
        if path.exists():
            path.unlink()


class OfflineFetcher:
    def fetch_active_markets(self, **_kwargs):
        raise ConnectionError("offline")


def test_run_one_cycle_reports_fetch_outage_without_crashing(isolated_event_log):
    logger = logging.getLogger("test_live_bot_offline")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=OfflineFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(Path("data/test_offline_positions.json")).load(),
        logger=logger,
        event_log=isolated_event_log,
    )

    assert bot.run_one_cycle() is False


class InterruptedOrderPlacer:
    def place_order(self, **_kwargs):
        raise requests.ConnectionError("response lost")


class BalancePreflightFailingOrderPlacer:
    def place_order(self, **_kwargs):
        raise BalancePreflightError("balance preflight failed")


def test_live_order_connection_loss_records_unknown_local_guard(monkeypatch, isolated_event_log):
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
        event_log=isolated_event_log,
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


def test_live_balance_preflight_failure_does_not_record_unknown_guard(monkeypatch, isolated_event_log):
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
        event_log=isolated_event_log,
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


def test_live_mode_blocks_cycle_until_startup_reconcile_succeeds(monkeypatch, isolated_event_log):
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
        event_log=isolated_event_log,
    )
    bot.reconciler = FailingReconciler()

    assert bot.run_one_cycle() is False


class RecordingMarketStream:
    reconnect_count = 0

    def __init__(self):
        self.tokens: list[str] = []
        self.warmups: list[float] = []

    def set_market_candidates(self, markets):
        self.tokens = unique_market_token_ids(markets)

    def warmup(self, seconds):
        self.warmups.append(seconds)


class RecordingUserStream:
    reconnect_count = 0

    def __init__(self):
        self.condition_ids: list[str] = []

    def set_market_candidates(self, markets):
        self.condition_ids = unique_market_condition_ids(markets)


def test_sync_stream_subscriptions_uses_token_and_condition_ids(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("POLYMARKET_WS_MARKET_WARMUP_SECONDS", "0")
    logger = logging.getLogger("test_live_bot_stream_sync")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    market_stream = RecordingMarketStream()
    user_stream = RecordingUserStream()
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(Path("data/test_stream_sync_positions.json")).load(),
        logger=logger,
        market_stream=market_stream,
        user_stream=user_stream,
        event_log=isolated_event_log,
    )
    markets = [
        {
            "id": "gamma-1",
            "conditionId": "0xabc",
            "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        }
    ]

    bot._sync_stream_subscriptions(markets)

    assert market_stream.tokens == ["yes-token", "no-token"]
    assert user_stream.condition_ids == ["0xabc"]
    assert market_stream.warmups == [0.0]


def test_market_stream_subscription_prioritizes_tradeable_candidates(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("POLYMARKET_WS_MARKET_WARMUP_SECONDS", "0")
    monkeypatch.setenv("POLYMARKET_WS_MARKET_MAX_TOKENS", "2")
    logger = logging.getLogger("test_live_bot_stream_priority")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    market_stream = RecordingMarketStream()
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(Path("data/test_stream_priority_positions.json")).load(),
        logger=logger,
        market_stream=market_stream,
        user_stream=False,
        event_log=isolated_event_log,
    )
    markets = [
        {
            "id": "off-topic",
            "conditionId": "0xoff",
            "question": "Who will win the championship?",
            "volumeNum": "100000",
            "endDate": "2026-04-28T12:00:00Z",
            "clobTokenIds": json.dumps(["off-yes", "off-no"]),
        },
        {
            "id": "weather-tradeable",
            "conditionId": "0xweather",
            "question": "Will the highest temperature in New York be above 70F on April 27, 2026?",
            "volumeNum": "1000",
            "endDate": "2026-04-28T12:00:00Z",
            "clobTokenIds": json.dumps(["weather-yes", "weather-no"]),
        },
    ]

    bot._sync_stream_subscriptions(
        markets,
        entered_positions={},
        as_of=datetime(2026, 4, 24, 12, tzinfo=timezone.utc),
    )

    assert bot._last_market_stream_tokens == ("weather-yes", "weather-no")
    assert market_stream.tokens[:2] == ["weather-yes", "weather-no"]
    assert market_stream.warmups == [0.0]


class RecordingReconciler:
    def __init__(self):
        self.calls: list[dict] = []

    def reconcile(self, **kwargs):
        self.calls.append(kwargs)


def test_periodic_safety_reconciliation_reuses_cycle_markets(monkeypatch, isolated_event_log):
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("RECONCILE_ON_STARTUP", "false")
    monkeypatch.setenv("SAFETY_RECONCILE_INTERVAL_MINUTES", "60")
    logger = logging.getLogger("test_live_bot_periodic_safety")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    bot = LiveBot(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=PositionLedger(Path("data/test_periodic_safety_positions.json")).load(),
        logger=logger,
        market_stream=None,
        user_stream=None,
        event_log=isolated_event_log,
    )
    reconciler = RecordingReconciler()
    bot.reconciler = reconciler
    bot._next_safety_reconcile_at = 0.0
    markets = [{"id": "gamma-1", "conditionId": "0xabc"}]

    assert bot._ensure_periodic_safety_reconcile(markets) is True

    assert len(reconciler.calls) == 1
    assert reconciler.calls[0]["active_markets"] is markets
    assert reconciler.calls[0]["reason"] == "periodic_safety"


class SnapshotFetcher:
    def fetch_quote(self, token_id):
        assert token_id == "yes-token"
        return LiveQuote(
            token_id="yes-token",
            best_bid=0.40,
            best_ask=0.44,
            midpoint=0.42,
            source="test_book",
        )


def test_due_snapshots_record_market_and_forecast_for_touched_markets(monkeypatch):
    monkeypatch.setenv("EVENT_SNAPSHOT_INTERVAL_MINUTES", "1")
    monkeypatch.setattr("weather_arb_live.live_bot.estimate_forecast_prob", lambda **_kwargs: 0.72)
    monkeypatch.setattr(
        "weather_arb_live.live_bot._fetch_forecast_window",
        lambda *_args, **_kwargs: {
            "target_date": "2026-04-27",
            "lead_days": 3,
            "model": "gfs_seamless",
            "temp_max": 74.0,
            "temp_min": 62.0,
        },
    )
    events_path = Path("data/test_snapshot_events.jsonl")
    market_path = Path("data/test_snapshot_market.jsonl")
    forecast_path = Path("data/test_snapshot_forecast.jsonl")
    positions_path = Path("data/test_snapshot_positions.json")
    for cleanup_path in (events_path, market_path, forecast_path, positions_path):
        cleanup_path.unlink(missing_ok=True)
    event_log = LiveEventLog(
        event_path=events_path,
        market_snapshot_path=market_path,
        forecast_snapshot_path=forecast_path,
    )
    logger = logging.getLogger("test_live_bot_snapshots")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    try:
        bot = LiveBot(
            fetcher=SnapshotFetcher(),
            order_placer=FakeOrderPlacer(),
            ledger=PositionLedger(positions_path).load(),
            logger=logger,
            market_stream=False,
            user_stream=False,
            event_log=event_log,
        )
        bot._touched_market_context["m1"] = {
            "market_id": "m1",
            "condition_id": "c1",
            "token_id": "yes-token",
            "city": "New York",
            "target_date": "2026-04-27",
            "bracket": {"low": 70.0, "high": None, "unit": "F", "metric": "max"},
            "side": "YES",
            "shares": 10.0,
            "position_usd": 4.0,
        }
        market = {
            "id": "m1",
            "conditionId": "c1",
            "question": "Will the highest temperature in New York be above 70F on April 27, 2026?",
            "endDate": "2026-04-28T00:00:00Z",
            "volumeNum": "1000",
            "clobTokenIds": json.dumps(["yes-token", "no-token"]),
        }

        bot._record_due_snapshots(
            markets=[market],
            as_of=__import__("datetime").datetime(2026, 4, 24, 12, tzinfo=__import__("datetime").timezone.utc),
        )

        market_rows = _jsonl(market_path)
        forecast_rows = _jsonl(forecast_path)
        assert market_rows[0]["market_id"] == "m1"
        assert market_rows[0]["best_bid"] == 0.40
        assert round(market_rows[0]["mark_to_market_pnl"], 2) == 0.2
        assert forecast_rows[0]["model_probability"] == 0.72
        assert forecast_rows[0]["forecast_temp"] == 74.0
    finally:
        for cleanup_path in (events_path, market_path, forecast_path, positions_path):
            cleanup_path.unlink(missing_ok=True)
