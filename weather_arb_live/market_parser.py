"""Parse Polymarket weather question strings into structured market metadata."""
from __future__ import annotations
import re
from dataclasses import dataclass
from datetime import date, datetime

CITIES = {
    "New York": {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},
    "NYC":      {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},
    "New York City": {"lat": 40.7128, "lon": -74.0060, "tz": "America/New_York"},
    "Chicago":  {"lat": 41.8781, "lon": -87.6298, "tz": "America/Chicago"},
    "Los Angeles": {"lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles"},
    "LA":       {"lat": 34.0522, "lon": -118.2437, "tz": "America/Los_Angeles"},
    "Miami":    {"lat": 25.7617, "lon": -80.1918, "tz": "America/New_York"},
    "London":   {"lat": 51.5074, "lon": -0.1278, "tz": "Europe/London"},
    "Tokyo":    {"lat": 35.6762, "lon": 139.6503, "tz": "Asia/Tokyo"},
    "Paris":    {"lat": 48.8566, "lon": 2.3522, "tz": "Europe/Paris"},
    "Sydney":   {"lat": -33.8688, "lon": 151.2093, "tz": "Australia/Sydney"},
    "Seoul":    {"lat": 37.5665, "lon": 126.9780, "tz": "Asia/Seoul"},
    "Boston":   {"lat": 42.3601, "lon": -71.0589, "tz": "America/New_York"},
    "Dallas":   {"lat": 32.7767, "lon": -96.7970, "tz": "America/Chicago"},
    "Houston":  {"lat": 29.7604, "lon": -95.3698, "tz": "America/Chicago"},
    "Denver":   {"lat": 39.7392, "lon": -104.9903, "tz": "America/Denver"},
    "Seattle":  {"lat": 47.6062, "lon": -122.3321, "tz": "America/Los_Angeles"},
    "Phoenix":  {"lat": 33.4484, "lon": -112.0740, "tz": "America/Phoenix"},
    "Atlanta":  {"lat": 33.7490, "lon": -84.3880, "tz": "America/New_York"},
    "Austin":   {"lat": 30.2672, "lon": -97.7431, "tz": "America/Chicago"},
    "Minneapolis": {"lat": 44.9778, "lon": -93.2650, "tz": "America/Chicago"},
    "Las Vegas":     {"lat": 36.1699, "lon": -115.1398, "tz": "America/Los_Angeles"},
    "San Francisco": {"lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles"},
    "SF":            {"lat": 37.7749, "lon": -122.4194, "tz": "America/Los_Angeles"},
    "Detroit":       {"lat": 42.3314, "lon": -83.0458, "tz": "America/Detroit"},
    "Philadelphia":  {"lat": 39.9526, "lon": -75.1652, "tz": "America/New_York"},
    "Philly":        {"lat": 39.9526, "lon": -75.1652, "tz": "America/New_York"},
    "Toronto":       {"lat": 43.6532, "lon": -79.3832, "tz": "America/Toronto"},
    "Vancouver":     {"lat": 49.2827, "lon": -123.1207, "tz": "America/Vancouver"},
    "Berlin":        {"lat": 52.5200, "lon": 13.4050, "tz": "Europe/Berlin"},
    "Amsterdam":     {"lat": 52.3676, "lon": 4.9041, "tz": "Europe/Amsterdam"},
    "Hong Kong":     {"lat": 22.3193, "lon": 114.1694, "tz": "Asia/Hong_Kong"},
    "Singapore":     {"lat": 1.3521, "lon": 103.8198, "tz": "Asia/Singapore"},
    "Mumbai":        {"lat": 19.0760, "lon": 72.8777, "tz": "Asia/Kolkata"},
    "Sao Paulo":     {"lat": -23.5505, "lon": -46.6333, "tz": "America/Sao_Paulo"},
    "Buenos Aires":  {"lat": -34.6037, "lon": -58.3816, "tz": "America/Argentina/Buenos_Aires"},
    "Ankara":        {"lat": 39.9334, "lon": 32.8597, "tz": "Europe/Istanbul"},
    "Munich":        {"lat": 48.1351, "lon": 11.5820, "tz": "Europe/Berlin"},
    "Tel Aviv":      {"lat": 32.0853, "lon": 34.7818, "tz": "Asia/Jerusalem"},
    "Milan":         {"lat": 45.4642, "lon": 9.1900, "tz": "Europe/Rome"},
    "Madrid":        {"lat": 40.4168, "lon": -3.7038, "tz": "Europe/Madrid"},
    "Warsaw":        {"lat": 52.2297, "lon": 21.0122, "tz": "Europe/Warsaw"},
    "Moscow":        {"lat": 55.7558, "lon": 37.6173, "tz": "Europe/Moscow"},
    "Istanbul":      {"lat": 41.0082, "lon": 28.9784, "tz": "Europe/Istanbul"},
    "Helsinki":      {"lat": 60.1699, "lon": 24.9384, "tz": "Europe/Helsinki"},
    "Jeddah":        {"lat": 21.4858, "lon": 39.1925, "tz": "Asia/Riyadh"},
    "Lagos":         {"lat": 6.5244, "lon": 3.3792, "tz": "Africa/Lagos"},
    "Cape Town":     {"lat": -33.9249, "lon": 18.4241, "tz": "Africa/Johannesburg"},
    "Shanghai":      {"lat": 31.2304, "lon": 121.4737, "tz": "Asia/Shanghai"},
    "Wellington":    {"lat": -41.2865, "lon": 174.7762, "tz": "Pacific/Auckland"},
    "Lucknow":       {"lat": 26.8467, "lon": 80.9462, "tz": "Asia/Kolkata"},
    "Taipei":        {"lat": 25.0330, "lon": 121.5654, "tz": "Asia/Taipei"},
    "Chongqing":     {"lat": 29.5630, "lon": 106.5516, "tz": "Asia/Shanghai"},
    "Beijing":       {"lat": 39.9042, "lon": 116.4074, "tz": "Asia/Shanghai"},
    "Wuhan":         {"lat": 30.5928, "lon": 114.3055, "tz": "Asia/Shanghai"},
    "Chengdu":       {"lat": 30.5728, "lon": 104.0668, "tz": "Asia/Shanghai"},
    "Shenzhen":      {"lat": 22.5431, "lon": 114.0579, "tz": "Asia/Shanghai"},
    "Mexico City":   {"lat": 19.4326, "lon": -99.1332, "tz": "America/Mexico_City"},
    "Busan":         {"lat": 35.1796, "lon": 129.0756, "tz": "Asia/Seoul"},
    "Panama City":   {"lat": 8.9824, "lon": -79.5199, "tz": "America/Panama"},
    "Kuala Lumpur":  {"lat": 3.1390, "lon": 101.6869, "tz": "Asia/Kuala_Lumpur"},
    "Jakarta":       {"lat": -6.2088, "lon": 106.8456, "tz": "Asia/Jakarta"},
    "Guangzhou":     {"lat": 23.1291, "lon": 113.2644, "tz": "Asia/Shanghai"},
    "Karachi":       {"lat": 24.8607, "lon": 67.0011, "tz": "Asia/Karachi"},
    "Manila":        {"lat": 14.5995, "lon": 120.9842, "tz": "Asia/Manila"},
}

_ALIAS_TO_CANONICAL = {
    "NYC": "New York",
    "New York City": "New York",
    "LA": "Los Angeles",
    "SF": "San Francisco",
    "Philly": "Philadelphia",
}

# Longer names must come first so "New York City" matches before "New York".
_CITY_ORDER = sorted(CITIES.keys(), key=len, reverse=True)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _normalize(s: str) -> str:
    # Replace various unicode chars that corrupt the ° symbol.
    s = s.replace("\u00b0", " deg ")
    # Some questions use "–", "—", or literal en/em dashes as range separators.
    s = s.replace("\u2013", "-").replace("\u2014", "-")
    # Sometimes " C" or " F" sticks to number; ensure a space
    s = re.sub(r"(\d)\s*deg\s*", r"\1 ", s)
    return s


def _find_city(q: str) -> tuple[str, dict] | None:
    low = q.lower()
    for name in _CITY_ORDER:
        if re.search(r'\b' + re.escape(name.lower()) + r'\b', low):
            canonical = _ALIAS_TO_CANONICAL.get(name, name)
            return canonical, CITIES[name]
    return None


_DATE_RE_MDY = re.compile(
    r"(?:on\s+)?(january|february|march|april|may|june|july|august|september|october|november|december|jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s*(\d{4}))?",
    re.IGNORECASE,
)


def _extract_date(q: str, end_date_hint: date | None = None) -> date | None:
    """Extract a date from the question. If year missing, infer from end_date_hint."""
    m = _DATE_RE_MDY.search(q)
    if not m:
        return None
    mon = _MONTHS[m.group(1).lower()]
    day = int(m.group(2))
    year_str = m.group(3)
    if year_str:
        year = int(year_str)
    else:
        # Infer from endDate
        if end_date_hint:
            year = end_date_hint.year
            cand = date(year, mon, day)
            if abs((cand - end_date_hint).days) > 180:
                year = end_date_hint.year - 1
                cand = date(year, mon, day)
            return cand
        return None
    try:
        return date(year, mon, day)
    except ValueError:
        return None


def _parse_brackets(q: str, unit: str) -> tuple[float | None, float | None] | None:
    """Return (low, high) temperature brackets in the detected unit."""
    # Normalize: ° was replaced with " deg ". Strip " deg " and remove unit markers
    # to get just numbers in context.
    text = q
    # Unify "F" or "C" marker handling — look for them near the number.

    # "between X and Y" or "between X-Y" or "X-Y" or "X – Y"
    # `-?` on each capture allows negative temperatures (e.g. "between -10C and -5C").
    patterns_between = [
        r"between\s+(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c|fahrenheit|celsius)?\s*(?:and|to|-|–|—|\u2013|\u2014)\s*(-?\d+(?:\.\d+)?)",
        r"(-?\d+(?:\.\d+)?)\s*(?:deg)?\s*(?:f|c)?\s*-\s*(-?\d+(?:\.\d+)?)\s*(?:deg)?\s*(?:f|c)",
        r"(-?\d+(?:\.\d+)?)\s*-\s*(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)",
    ]
    for pat in patterns_between:
        m = re.search(pat, text, re.IGNORECASE)
        if m:
            lo = float(m.group(1))
            hi = float(m.group(2))
            if lo > hi:
                lo, hi = hi, lo
            return (lo, hi)

    # "above X" / "greater than X" / "X or higher" / "X or above" / "at least X"
    m = re.search(
        r"(?:above|greater than|higher than|over|at least|>=|>)\s*(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)?",
        text, re.IGNORECASE,
    )
    if m:
        return (float(m.group(1)), None)
    m = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)?\s*(?:or higher|or above|or more)",
        text, re.IGNORECASE,
    )
    if m:
        return (float(m.group(1)), None)

    # "below X" / "less than X" / "X or below" / "X or lower"
    m = re.search(
        r"(?:below|less than|under|<=|<)\s*(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)?",
        text, re.IGNORECASE,
    )
    if m:
        return (None, float(m.group(1)))
    m = re.search(
        r"(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)?\s*(?:or below|or lower|or less)",
        text, re.IGNORECASE,
    )
    if m:
        return (None, float(m.group(1)))

    # "exactly X" / "exactly X°F" / just a single number "be X°F on"
    m = re.search(
        r"(?:exactly|be)\s+(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)\b",
        text, re.IGNORECASE,
    )
    if m:
        v = float(m.group(1))
        # Single-integer bracket: ±0.5 for integer resolution
        return (v - 0.5, v + 0.5)

    # fallback: any "N F" or "N C" alone (lookbehind avoids mid-word/number matches
    # that the original `\b` boundary blocked, while still allowing a leading `-`).
    m = re.search(
        r"(?<![\w.])(-?\d+(?:\.\d+)?)\s*(?:deg\s*)?(?:f|c)\b",
        text, re.IGNORECASE,
    )
    if m:
        v = float(m.group(1))
        return (v - 0.5, v + 0.5)

    return None


def _detect_unit(q: str) -> str | None:
    low = q.lower()
    # Look for explicit mentions
    if re.search(r"\bf\b|fahrenheit|\bdeg\s*f\b|°f|(?<![a-z])\d+(?:\.\d+)?\s*f\b", low):
        has_f = True
    else:
        has_f = False
    if re.search(r"\bc\b|celsius|\bdeg\s*c\b|°c|(?<![a-z])\d+(?:\.\d+)?\s*c\b", low):
        has_c = True
    else:
        has_c = False
    if has_f and not has_c:
        return "F"
    if has_c and not has_f:
        return "C"
    if has_f and has_c:
        # prefer whichever is more frequent
        if low.count("f") > low.count("c"):
            return "F"
        return "C"
    return None


def parse_market_question(
    question: str,
    end_date_hint: date | None = None,
) -> dict | None:
    """Extract {city, lat, lon, tz, date, unit, bracket_low, bracket_high} or None."""
    if not question:
        return None
    q = _normalize(question)

    city = _find_city(q)
    if not city:
        return None
    city_name, city_info = city

    # Only handle "highest temperature" / "temperature" style questions.
    ql = q.lower()
    if "temperature" not in ql and "high in" not in ql and "low in" not in ql \
            and "temp" not in ql:
        return None

    unit = _detect_unit(q)
    if unit is None:
        return None

    brackets = _parse_brackets(q, unit)
    if brackets is None:
        return None
    lo, hi = brackets

    d = _extract_date(q, end_date_hint=end_date_hint)
    if d is None:
        return None

    # Heuristic: is this a 'high' (max) or 'low' (min) question?
    metric = "max"
    if re.search(r"\blow(?:est)?\b|\blowest\s+temperature\b", ql):
        metric = "min"

    return {
        "city": city_name,
        "lat": city_info["lat"],
        "lon": city_info["lon"],
        "tz": city_info["tz"],
        "date": d,
        "unit": unit,
        "bracket_low": lo,
        "bracket_high": hi,
        "metric": metric,
    }


def _parse_end_date(end_date_str: str | None) -> date | None:
    if not end_date_str:
        return None
    try:
        return datetime.fromisoformat(end_date_str.replace("Z", "+00:00")).date()
    except Exception:
        return None


if __name__ == "__main__":
    import json
    from . import config

    with (config.DATA_DIR / "raw_markets.json").open(encoding="utf-8") as f:
        markets = json.load(f)

    parsed_ok = 0
    samples_ok = []
    samples_fail = []
    weather_markets = 0
    city_temp_markets = 0
    for m in markets:
        q = m.get("question") or ""
        # Only apply parser to city temperature markets
        if "temperature" not in q.lower() and "temp" not in q.lower():
            continue
        weather_markets += 1
        has_city = any(c.lower() in q.lower() for c in _CITY_ORDER)
        if not has_city:
            continue
        city_temp_markets += 1
        ed = _parse_end_date(m.get("endDate"))
        parsed = parse_market_question(q, end_date_hint=ed)
        if parsed:
            parsed_ok += 1
            if len(samples_ok) < 5:
                samples_ok.append((q, parsed))
        else:
            if len(samples_fail) < 10:
                samples_fail.append(q)

    print(f"Total markets: {len(markets)}")
    print(f"Markets mentioning temperature: {weather_markets}")
    print(f"City-temperature markets: {city_temp_markets}")
    print(f"Parser succeeded on: {parsed_ok} / {city_temp_markets} "
          f"({100.0 * parsed_ok / max(1, city_temp_markets):.1f}%)")

    print("\n=== Sample successful parses ===")
    for q, p in samples_ok:
        print(f"Q: {q}")
        print(f"  -> {p}")

    print("\n=== Sample failures ===")
    for q in samples_fail:
        print(f"- {q}")
