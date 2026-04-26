from __future__ import annotations

import json
from pathlib import Path

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
