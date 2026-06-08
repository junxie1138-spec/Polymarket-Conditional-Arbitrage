from __future__ import annotations

from datetime import datetime, timezone

import pytest

from weather_arb_live.strategy import evaluate_market

AS_OF = datetime(2026, 4, 24, 12, tzinfo=timezone.utc)


def market(**overrides):
    row = {
        "id": "m1",
        "question": "Will the highest temperature in New York be above 70F on April 27, 2026?",
        "endDate": "2026-04-28T00:00:00Z",
        "clobTokenIds": '["yes-token", "no-token"]',
        "volumeNum": "1000",
    }
    row.update(overrides)
    return row


def forecast(value):
    def _fn(**_kwargs):
        return value

    return _fn


def test_enter_when_fixed_v1_gates_pass():
    decision = evaluate_market(
        market(),
        0.30,
        as_of=AS_OF,
        entered_positions={},
        forecast_probability_fn=forecast(0.80),
        max_position_usd=50.0,
    )

    assert decision.action == "ENTER"
    assert decision.plan is not None
    assert decision.plan.market_id == "m1"
    assert decision.plan.token_id == "yes-token"
    assert decision.plan.side == "YES"
    assert decision.plan.entry_price == 0.30 * 1.005
    assert decision.plan.edge == pytest.approx(0.80 - (0.30 * 1.005))
    assert decision.plan.lead_days == 3


def test_labeled_outcomes_determine_yes_no_tokens_even_when_token_array_is_reversed():
    reversed_market = market(
        outcomes='["No", "Yes"]',
        clobTokenIds='["no-token", "yes-token"]',
    )

    yes_decision = evaluate_market(
        reversed_market,
        0.30,
        as_of=AS_OF,
        entered_positions={},
        forecast_probability_fn=forecast(0.80),
        max_position_usd=50.0,
    )
    no_decision = evaluate_market(
        reversed_market,
        0.30,
        side="NO",
        as_of=AS_OF,
        entered_positions={},
        forecast_probability_fn=forecast(0.20),
        max_position_usd=50.0,
    )

    assert yes_decision.action == "ENTER"
    assert yes_decision.plan is not None
    assert yes_decision.plan.token_id == "yes-token"
    assert no_decision.action == "ENTER"
    assert no_decision.plan is not None
    assert no_decision.plan.token_id == "no-token"


def test_enter_uses_configured_max_position_when_not_overridden(monkeypatch):
    monkeypatch.setenv("MAX_POSITION_USD", "2.50")

    decision = evaluate_market(
        market(),
        0.30,
        as_of=AS_OF,
        entered_positions={},
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.action == "ENTER"
    assert decision.plan is not None
    assert decision.plan.position_usd == 2.5
    assert decision.plan.shares == pytest.approx(2.5 / decision.plan.entry_price)


def test_skip_already_entered_market():
    decision = evaluate_market(
        market(),
        0.30,
        as_of=AS_OF,
        entered_positions={"m1": {"entry_price": 0.30}},
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.action == "SKIP"
    assert decision.reason == "already_entered"


def test_skip_already_entered_by_reconciled_condition_id():
    decision = evaluate_market(
        market(id="gamma-1", conditionId="0xabc"),
        0.30,
        as_of=AS_OF,
        entered_positions={
            "0xabc": {
                "market_id": "0xabc",
                "condition_id": "0xabc",
                "token_id": "yes-token",
            }
        },
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "already_entered"


def test_skip_opposite_side_when_reconciled_token_is_in_same_market():
    decision = evaluate_market(
        market(id="gamma-1", conditionId="0xabc"),
        0.30,
        side="YES",
        as_of=AS_OF,
        entered_positions={
            "external-no-token": {
                "market_id": "external-no-token",
                "token_id": "no-token",
            }
        },
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "already_entered"


def test_skip_low_price():
    decision = evaluate_market(
        market(),
        0.24,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "below_min_entry_price"


def test_skip_low_forecast_probability():
    decision = evaluate_market(
        market(),
        0.30,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.64),
    )

    assert decision.reason == "below_min_forecast_probability"


def test_skip_low_edge():
    decision = evaluate_market(
        market(),
        0.60,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.70),
    )

    assert decision.reason == "below_min_edge"


def test_edge_gate_uses_slipped_entry_price():
    decision = evaluate_market(
        market(),
        0.60,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.722),
    )

    assert decision.reason == "below_min_edge"
    assert decision.details["edge"] == pytest.approx(0.722 - (0.60 * 1.005))


def test_no_side_enters_when_no_gates_pass():
    decision = evaluate_market(
        market(),
        0.30,
        side="NO",
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.20),
        max_position_usd=50.0,
    )

    assert decision.action == "ENTER"
    assert decision.plan is not None
    assert decision.plan.token_id == "no-token"
    assert decision.plan.side == "NO"
    assert decision.plan.forecast_prob == pytest.approx(0.80)
    assert decision.plan.edge == pytest.approx(0.80 - (0.30 * 1.005))


def test_no_side_rejects_overpriced_no_token():
    decision = evaluate_market(
        market(),
        0.75,
        side="NO",
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.20),
    )

    assert decision.reason == "above_max_no_entry_price"


def test_no_side_does_not_apply_yes_calibration():
    class RejectingCalibration:
        def passes(self, **_kwargs):
            return False

    decision = evaluate_market(
        market(),
        0.30,
        side="NO",
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.20),
        calibration=RejectingCalibration(),
    )

    assert decision.action == "ENTER"


def test_skip_low_volume():
    decision = evaluate_market(
        market(volumeNum="499"),
        0.30,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "low_volume"


def test_skip_unsupported_lead_time():
    decision = evaluate_market(
        market(
            question="Will the highest temperature in New York be above 70F on May 3, 2026?",
            endDate="2026-05-04T00:00:00Z",
        ),
        0.30,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "unsupported_lead_time"


def test_skip_within_24_hours_of_resolution():
    decision = evaluate_market(
        market(endDate="2026-04-25T00:00:00Z"),
        0.30,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "too_close_to_resolution"


def test_skip_when_calibration_rejects():
    class RejectingCalibration:
        def passes(self, **_kwargs):
            return False

    decision = evaluate_market(
        market(),
        0.30,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.80),
        calibration=RejectingCalibration(),
    )

    assert decision.reason == "calibration_rejected"


def test_preserves_backtest_target_date_nuance():
    decision = evaluate_market(
        market(
            question="Will the highest temperature in New York be above 70F on April 24, 2026?",
            endDate="2026-04-25T23:00:00Z",
        ),
        0.30,
        as_of=AS_OF,
        forecast_probability_fn=forecast(0.80),
    )

    assert decision.reason == "target_not_future"
