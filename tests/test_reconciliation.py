from __future__ import annotations

import json
from pathlib import Path

import pytest

from weather_arb_live.event_log import LiveEventLog
from weather_arb_live.ledger import PositionLedger
from weather_arb_live.reconciliation import Reconciler


class FakeFetcher:
    def fetch_active_markets(self, *, limit=None):
        markets = [
            {
                "id": "gamma-1",
                "conditionId": "0xabc",
                "question": "Will the highest temperature in New York be above 70F on April 27, 2026?",
                "clobTokenIds": json.dumps(["yes-token", "no-token"]),
            }
        ]
        return markets[:limit] if limit else markets


class FakeOrderPlacer:
    def __init__(self, open_orders=None):
        self.open_orders = open_orders or []

    def get_client_address(self):
        return "0x0000000000000000000000000000000000000001"

    def fetch_open_orders(self):
        return self.open_orders


class StaticPositionsReconciler(Reconciler):
    def __init__(self, *args, positions=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._positions = positions or []

    def _fetch_user_positions(self, _user_address):
        return self._positions


def _ledger(path_name: str) -> PositionLedger:
    path = Path("data") / path_name
    if path.exists():
        path.unlink()
    return PositionLedger(path).load()


def _cleanup(ledger: PositionLedger) -> None:
    if ledger.path.exists():
        ledger.path.unlink()


def _jsonl(path: Path):
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def test_reconcile_adds_guard_for_exchange_position():
    ledger = _ledger("test_reconcile_position.json")
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[
            {
                "asset": "yes-token",
                "conditionId": "0xabc",
                "size": 10,
                "avgPrice": 0.4,
                "curPrice": 0.55,
                "cashPnl": 1.5,
                "outcome": "Yes",
                "title": "Weather",
            }
        ],
    )

    try:
        result = reconciler.reconcile()

        assert result.added_guards == 1
        assert "gamma-1" in ledger.positions
        row = ledger.positions["gamma-1"]
        assert row["token_id"] == "yes-token"
        assert row["condition_id"] == "0xabc"
        assert row["side"] == "YES"
        assert row["dry_run"] is False
        assert row["order_response"]["posted"] == "reconciled"
        assert row["order_response"]["reason"] == "exchange_position"
        assert row["order_response"]["exchange"]["curPrice"] == 0.55
        assert row["order_response"]["exchange"]["cashPnl"] == 1.5
    finally:
        _cleanup(ledger)


def test_reconcile_appends_exchange_position_fill_event():
    ledger = _ledger("test_reconcile_position_event.json")
    event_path = Path("data/test_reconcile_position_events.jsonl")
    market_path = Path("data/test_reconcile_position_market.jsonl")
    forecast_path = Path("data/test_reconcile_position_forecast.jsonl")
    for path in (event_path, market_path, forecast_path):
        path.unlink(missing_ok=True)
    event_log = LiveEventLog(
        event_path=event_path,
        market_snapshot_path=market_path,
        forecast_snapshot_path=forecast_path,
    )
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        event_log=event_log,
        positions=[
            {
                "asset": "yes-token",
                "conditionId": "0xabc",
                "size": 10,
                "avgPrice": 0.4,
                "curPrice": 0.55,
                "cashPnl": 1.5,
                "outcome": "Yes",
                "title": "Weather",
            }
        ],
    )

    try:
        reconciler.reconcile()

        rows = _jsonl(event_path)
        assert rows[0]["event_type"] == "order_filled"
        assert rows[0]["market_id"] == "gamma-1"
        assert rows[0]["city"] == "New York"
        assert rows[0]["filled_price"] == 0.4
        assert rows[0]["fill_quantity"] == 10
        assert rows[0]["mark_to_market_pnl"] == 1.5
    finally:
        _cleanup(ledger)
        for path in (event_path, market_path, forecast_path):
            path.unlink(missing_ok=True)


def test_reconcile_matches_existing_unknown_order_guard():
    ledger = _ledger("test_reconcile_match.json")
    ledger.positions["gamma-1"] = {
        "market_id": "gamma-1",
        "condition_id": "0xabc",
        "token_id": "yes-token",
        "dry_run": False,
        "order_response": {"posted": "unknown"},
    }
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[{"asset": "yes-token", "conditionId": "0xabc", "size": 10, "avgPrice": 0.4}],
    )

    try:
        result = reconciler.reconcile()

        assert result.matched_local == 1
        assert result.added_guards == 0
        assert ledger.positions["gamma-1"]["reconciliation"]["status"] == "matched_position"
        assert ledger.positions["gamma-1"]["reconciliation"]["requires_manual_review"] is False
    finally:
        _cleanup(ledger)


def test_reconcile_matches_existing_ledger_row_outside_active_market_slice():
    ledger = _ledger("test_reconcile_limited_market_slice.json")
    ledger.positions["gamma-outside"] = {
        "market_id": "gamma-outside",
        "condition_id": "0xoutside",
        "token_id": "outside-token",
        "dry_run": False,
        "order_response": {"posted": "unknown"},
    }
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[
            {
                "asset": "outside-token",
                "conditionId": "0xoutside",
                "size": 10,
                "avgPrice": 0.4,
            }
        ],
    )

    try:
        result = reconciler.reconcile()

        assert result.matched_local == 1
        assert result.missing_local == 0
        assert result.added_guards == 0
        assert ledger.positions["gamma-outside"]["reconciliation"]["status"] == "matched_position"
    finally:
        _cleanup(ledger)


def test_reconcile_does_not_add_guard_for_unrelated_exchange_position():
    ledger = _ledger("test_reconcile_unrelated_position.json")
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[
            {
                "asset": "outside-token",
                "conditionId": "0xoutside",
                "size": 10,
                "avgPrice": 0.4,
            }
        ],
    )

    try:
        result = reconciler.reconcile()

        assert result.added_guards == 0
        assert ledger.positions == {}
    finally:
        _cleanup(ledger)


def test_reconcile_rejects_address_override_that_differs_from_trading_funder(monkeypatch):
    ledger = _ledger("test_reconcile_address_mismatch.json")
    monkeypatch.setenv("POLYMARKET_RECONCILE_USER_ADDRESS", "0x0000000000000000000000000000000000000002")
    monkeypatch.setenv("POLYMARKET_FUNDER_ADDRESS", "0x0000000000000000000000000000000000000001")
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[],
    )

    try:
        with pytest.raises(ValueError, match="must match"):
            reconciler.reconcile()
    finally:
        _cleanup(ledger)


def test_reconcile_marks_local_live_row_missing_for_manual_review():
    ledger = _ledger("test_reconcile_missing.json")
    ledger.positions["gamma-1"] = {
        "market_id": "gamma-1",
        "condition_id": "0xabc",
        "token_id": "yes-token",
        "dry_run": False,
        "order_response": {"posted": "unknown"},
    }
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[],
    )

    try:
        result = reconciler.reconcile()

        assert result.missing_local == 1
        assert ledger.positions["gamma-1"]["reconciliation"]["status"] == "missing_exchange_match"
        assert ledger.positions["gamma-1"]["reconciliation"]["requires_manual_review"] is True
    finally:
        _cleanup(ledger)


def test_reconcile_appends_position_closed_event_for_missing_exchange_match():
    ledger = _ledger("test_reconcile_missing_event.json")
    ledger.positions["gamma-1"] = {
        "market_id": "gamma-1",
        "condition_id": "0xabc",
        "token_id": "yes-token",
        "side": "YES",
        "city": "New York",
        "target_date": "2026-04-27",
        "dry_run": False,
        "order_response": {"posted": "unknown"},
    }
    event_path = Path("data/test_reconcile_missing_events.jsonl")
    market_path = Path("data/test_reconcile_missing_market.jsonl")
    forecast_path = Path("data/test_reconcile_missing_forecast.jsonl")
    for path in (event_path, market_path, forecast_path):
        path.unlink(missing_ok=True)
    event_log = LiveEventLog(
        event_path=event_path,
        market_snapshot_path=market_path,
        forecast_snapshot_path=forecast_path,
    )
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        event_log=event_log,
        positions=[],
    )

    try:
        reconciler.reconcile()

        rows = _jsonl(event_path)
        assert rows[0]["event_type"] == "position_closed"
        assert rows[0]["requires_manual_review"] is True
        assert rows[0]["market_id"] == "gamma-1"
    finally:
        _cleanup(ledger)
        for path in (event_path, market_path, forecast_path):
            path.unlink(missing_ok=True)


def test_reconcile_adds_guard_for_open_order():
    ledger = _ledger("test_reconcile_order.json")
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(
            open_orders=[
                {
                    "asset_id": "no-token",
                    "market": "0xabc",
                    "remaining_size": "12",
                    "price": "0.35",
                    "side": "BUY",
                }
            ]
        ),
        ledger=ledger,
        positions=[],
    )

    try:
        result = reconciler.reconcile()

        assert result.added_guards == 1
        assert ledger.positions["gamma-1"]["token_id"] == "no-token"
        assert ledger.positions["gamma-1"]["side"] == "NO"
        assert ledger.positions["gamma-1"]["order_response"]["reason"] == "exchange_open_order"
    finally:
        _cleanup(ledger)


def test_reconcile_replaces_dry_run_row_with_live_exchange_guard():
    ledger = _ledger("test_reconcile_replaces_dry_run.json")
    ledger.positions["gamma-1"] = {
        "market_id": "gamma-1",
        "condition_id": "0xabc",
        "token_id": "yes-token",
        "dry_run": True,
    }
    reconciler = StaticPositionsReconciler(
        fetcher=FakeFetcher(),
        order_placer=FakeOrderPlacer(),
        ledger=ledger,
        positions=[{"asset": "yes-token", "conditionId": "0xabc", "size": 10, "avgPrice": 0.4}],
    )

    try:
        result = reconciler.reconcile()

        assert result.added_guards == 1
        assert ledger.positions["gamma-1"]["dry_run"] is False
        assert ledger.positions["gamma-1"]["order_response"]["reason"] == "exchange_position"
    finally:
        _cleanup(ledger)
