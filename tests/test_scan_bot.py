from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from polymarket_conditional_arb import config
from polymarket_conditional_arb.arb_strategy import ArbStrategyParams
from polymarket_conditional_arb.event_log import ConditionalArbEventLog
from polymarket_conditional_arb.fetcher import GammaClobClient
from polymarket_conditional_arb.order_book import asks_from_book
from polymarket_conditional_arb.paper import PaperConditionalArbLedger
from polymarket_conditional_arb.scan_bot import ConditionalArbScanner, main


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
        slippage_buffer_bps=0.0,
        gas_cost_usd=0.0,
        taker_fee_bps=0.0,
        max_book_age_seconds=20.0,
        include_neg_risk=True,
    )


def null_logger():
    logger = logging.getLogger("test_scanner")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    return logger


def test_scanner_writes_json_snapshot_event_log_and_paper_ledger(tmp_path):
    cfg = scan_config(tmp_path)
    scanner = ConditionalArbScanner(
        scan_config=cfg,
        client=FakeClient(),
        ledger=PaperConditionalArbLedger(cfg.paper_ledger_path),
        event_log=ConditionalArbEventLog(cfg.event_log_path),
        logger=null_logger(),
        params=ArbStrategyParams.from_config(cfg),
    )

    result = scanner.run_once()

    assert result["summary"]["opportunities_detected"] == 1
    assert cfg.opportunities_path.exists()
    assert cfg.event_log_path.exists()
    assert cfg.paper_ledger_path.exists()

    snapshot = json.loads(cfg.opportunities_path.read_text(encoding="utf-8"))
    ledger = json.loads(cfg.paper_ledger_path.read_text(encoding="utf-8"))
    assert snapshot["opportunities"][0]["opportunity_id"] == "binary:m1"
    assert ledger["binary:m1"]["status"] == "paper_alert_recorded"


def test_cli_help_smoke(capsys):
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])

    assert excinfo.value.code == 0
    assert "Scan Polymarket" in capsys.readouterr().out


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
