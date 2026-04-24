from datetime import date

from weather_arb_live.market_parser import parse_market_question


def test_parse_above_temperature_question():
    parsed = parse_market_question(
        "Will the highest temperature in New York be above 70F on April 27, 2026?"
    )

    assert parsed is not None
    assert parsed["city"] == "New York"
    assert parsed["date"] == date(2026, 4, 27)
    assert parsed["unit"] == "F"
    assert parsed["bracket_low"] == 70.0
    assert parsed["bracket_high"] is None
    assert parsed["metric"] == "max"


def test_parse_between_temperature_question():
    parsed = parse_market_question(
        "Will the temperature in Chicago be between 50F and 60F on May 1, 2026?"
    )

    assert parsed is not None
    assert parsed["city"] == "Chicago"
    assert parsed["bracket_low"] == 50.0
    assert parsed["bracket_high"] == 60.0


def test_parse_low_temperature_question():
    parsed = parse_market_question(
        "Will the lowest temperature in Boston be below 35F on April 30, 2026?"
    )

    assert parsed is not None
    assert parsed["metric"] == "min"
    assert parsed["bracket_low"] is None
    assert parsed["bracket_high"] == 35.0
