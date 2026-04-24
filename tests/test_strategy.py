from __future__ import annotations

from datetime import datetime, timezone

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
    assert decision.plan.entry_price == 0.30 * 1.005
    assert decision.plan.edge == 0.50
    assert decision.plan.lead_days == 3


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
