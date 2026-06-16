from __future__ import annotations

import json
from dataclasses import replace

import pytest

from polymarket_conditional_arb import config
from polymarket_conditional_arb.latency import (
    LatencyProbeSettings,
    format_latency_report,
    measure_polymarket_rest_latency,
    summarize_latency_samples,
    write_latency_report,
)


class Response:
    def __init__(self, data, status_code=200):
        self._data = data
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"status={self.status_code}")

    def json(self):
        return self._data


class Session:
    def __init__(self, *, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


class StepClock:
    def __init__(self, increments):
        self.value = 1000.0
        self.increments = list(increments)

    def __call__(self):
        if self.increments:
            self.value += self.increments.pop(0)
        return self.value


def scan_config(tmp_path):
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
        market_ws_endpoint="wss://ws.example/ws/market",
        paper_simulation=config.PaperExecutionSimulationConfig.zero_friction(),
    )


def event_payload():
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


def test_summarize_latency_samples_groups_successes_and_errors():
    summary = summarize_latency_samples(
        [
            {"endpoint_family": "gamma_events", "latency_ms": 10.0, "status_code": 200, "error": None},
            {"endpoint_family": "gamma_events", "latency_ms": 20.0, "status_code": 200, "error": None},
            {"endpoint_family": "gamma_events", "latency_ms": 99.0, "status_code": 500, "error": "HTTPError"},
        ]
    )

    assert summary["gamma_events"]["sample_count"] == 3
    assert summary["gamma_events"]["success_count"] == 2
    assert summary["gamma_events"]["error_count"] == 1
    assert summary["gamma_events"]["p50_latency_ms"] == pytest.approx(10.0)
    assert summary["gamma_events"]["p95_latency_ms"] == pytest.approx(20.0)


def test_measure_rest_latency_discovers_market_and_recommends_clob_p95(tmp_path):
    session = Session(
        get_responses=[Response(event_payload()), Response(event_payload())],
        post_responses=[
            Response(
                [
                    {"asset_id": "yes-token", "asks": [{"price": "0.48", "size": "10"}]},
                    {"asset_id": "no-token", "asks": [{"price": "0.49", "size": "10"}]},
                ]
            ),
            Response(
                [
                    {"asset_id": "yes-token", "asks": [{"price": "0.48", "size": "10"}]},
                    {"asset_id": "no-token", "asks": [{"price": "0.49", "size": "10"}]},
                ]
            ),
        ],
    )
    clock = StepClock([0.0, 0.010, 0.0, 0.020, 0.0, 0.050, 0.0, 0.070])

    report = measure_polymarket_rest_latency(
        scan_config=scan_config(tmp_path),
        settings=LatencyProbeSettings(rest_samples=2, pause_seconds=0.0),
        session=session,
        clock=clock,
        sleep=lambda _seconds: None,
    )

    assert len(session.get_calls) == 2
    assert len(session.post_calls) == 2
    assert session.get_calls[0][1]["params"]["order"] == "volume24hr"
    assert session.post_calls[0][1]["json"] == [{"token_id": "yes-token"}, {"token_id": "no-token"}]
    assert report["probe_market"]["market_id"] == "m1"
    assert report["summaries"]["gamma_events"]["p95_latency_ms"] == pytest.approx(20.0)
    assert report["summaries"]["clob_books"]["p95_latency_ms"] == pytest.approx(70.0)
    assert report["recommendation"]["source"] == "clob_books"
    assert report["recommendation"]["latency_ms"] == pytest.approx(70.0)
    assert report["recommendation"]["latency_jitter_ms"] == pytest.approx(20.0)


def test_measure_rest_latency_skips_clob_without_probe_market(tmp_path):
    session = Session(get_responses=[Response([{"id": "e1", "markets": []}])])

    report = measure_polymarket_rest_latency(
        scan_config=scan_config(tmp_path),
        settings=LatencyProbeSettings(rest_samples=1, pause_seconds=0.0),
        session=session,
        clock=StepClock([0.0, 0.010]),
        sleep=lambda _seconds: None,
    )

    assert session.post_calls == []
    assert report["probe_market"] is None
    assert "clob_books" not in report["summaries"]
    assert report["recommendation"]["source"] == "gamma_events"


def test_format_and_write_latency_report(tmp_path):
    report = {
        "schema_version": 1,
        "measured_at_utc": "2026-06-16T00:00:00Z",
        "probe_market": {"market_id": "m1", "yes_token_id": "yes", "no_token_id": "no"},
        "summaries": {
            "clob_books": {
                "sample_count": 2,
                "success_count": 2,
                "error_count": 0,
                "p50_latency_ms": 10.0,
                "p95_latency_ms": 20.0,
                "max_latency_ms": 20.0,
            }
        },
        "recommendation": {
            "source": "clob_books",
            "latency_ms": 20.0,
            "latency_jitter_ms": 10.0,
            "env": ["COND_ARB_PAPER_LATENCY_MS=20.000"],
        },
    }

    rendered = format_latency_report(report)
    output_path = tmp_path / "latency.json"
    write_latency_report(output_path, report)

    assert "Polymarket public latency probe" in rendered
    assert "clob_books" in rendered
    assert "COND_ARB_PAPER_LATENCY_MS=20.000" in rendered
    saved = json.loads(output_path.read_text(encoding="utf-8"))
    assert saved["probe_market"]["market_id"] == "m1"


def test_scan_config_exposes_latency_report_path(tmp_path):
    cfg = replace(scan_config(tmp_path), data_dir=tmp_path / "state")

    assert cfg.latency_report_path == tmp_path / "state" / "polymarket_latency_report.json"
