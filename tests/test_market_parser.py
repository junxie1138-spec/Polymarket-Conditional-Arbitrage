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


def test_parse_live_celsius_exact_temperature_question():
    parsed = parse_market_question(
        "Will the highest temperature in Sao Paulo be 26\u00b0C on April 25?",
        end_date_hint=date(2026, 4, 26),
    )

    assert parsed is not None
    assert parsed["city"] == "Sao Paulo"
    assert parsed["date"] == date(2026, 4, 25)
    assert parsed["unit"] == "C"
    assert parsed["bracket_low"] == 25.5
    assert parsed["bracket_high"] == 26.5


def test_parse_live_celsius_negative_temperature_question():
    parsed = parse_market_question(
        "Will the highest temperature in Moscow be -2\u00b0C or below on April 25?",
        end_date_hint=date(2026, 4, 26),
    )

    assert parsed is not None
    assert parsed["city"] == "Moscow"
    assert parsed["bracket_low"] is None
    assert parsed["bracket_high"] == -2.0


def test_parse_live_celsius_low_temperature_question():
    parsed = parse_market_question(
        "Will the lowest temperature in Shanghai be 18\u00b0C or higher on April 25?",
        end_date_hint=date(2026, 4, 26),
    )

    assert parsed is not None
    assert parsed["city"] == "Shanghai"
    assert parsed["metric"] == "min"
    assert parsed["bracket_low"] == 18.0
    assert parsed["bracket_high"] is None


def test_parse_live_temperature_city_list_from_logs():
    cities = [
        "Sao Paulo",
        "Buenos Aires",
        "Ankara",
        "Munich",
        "Tel Aviv",
        "Milan",
        "Madrid",
        "Warsaw",
        "Moscow",
        "Istanbul",
        "Helsinki",
        "Jeddah",
        "Lagos",
        "Cape Town",
        "Shanghai",
        "Wellington",
        "Lucknow",
        "Taipei",
        "Chongqing",
        "Beijing",
        "Wuhan",
        "Chengdu",
        "Shenzhen",
        "Mexico City",
        "Busan",
        "Panama City",
        "Kuala Lumpur",
        "Jakarta",
        "Guangzhou",
        "Karachi",
        "Manila",
    ]

    for city in cities:
        parsed = parse_market_question(
            f"Will the highest temperature in {city} be 25\u00b0C or below on April 25?",
            end_date_hint=date(2026, 4, 26),
        )

        assert parsed is not None, city
        assert parsed["city"] == city
        assert parsed["date"] == date(2026, 4, 25)
        assert parsed["unit"] == "C"
        assert parsed["bracket_low"] is None
        assert parsed["bracket_high"] == 25.0
