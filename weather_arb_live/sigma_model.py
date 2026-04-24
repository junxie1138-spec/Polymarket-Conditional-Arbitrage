"""Compute city/season-specific temperature forecast σ from Open-Meteo historical data."""
from __future__ import annotations
import json
import os
import math
from datetime import date, timedelta
from statistics import mean

from . import config, network
from . import market_parser

SIGMA_CACHE_PATH = config.SIGMA_CACHE_PATH
FALLBACK_SIGMA = 3.0

SEASONS = {
    "DJF": [12, 1, 2],
    "MAM": [3, 4, 5],
    "JJA": [6, 7, 8],
    "SON": [9, 10, 11],
}

RAW_MARKETS_PATH = config.DATA_DIR / "raw_markets.json"


def _supported_cities_from_parser() -> dict[str, dict]:
    cities: dict[str, dict] = {}
    for name, info in market_parser.CITIES.items():
        canonical = market_parser._ALIAS_TO_CANONICAL.get(name, name)
        key = canonical.lower()
        if key not in cities:
            cities[key] = {
                "city": canonical,
                "lat": info.get("lat"),
                "lon": info.get("lon"),
                "tz": info.get("tz", "America/New_York"),
            }
    return cities


def _load_cities_from_markets() -> dict[str, dict]:
    """Load unique (city, lat, lon, tz) from raw_markets.json by parsing questions."""
    if not RAW_MARKETS_PATH.exists():
        return _supported_cities_from_parser()

    from .market_parser import parse_market_question, _parse_end_date

    with RAW_MARKETS_PATH.open(encoding="utf-8") as f:
        markets = json.load(f)

    cities = {}
    for i, market in enumerate(markets):
        if i % 1000 == 0 and i > 0:
            print(f"  Parsing {i}/{len(markets)} markets for city extraction...")

        question = market.get("question", "")
        if not question:
            continue

        end_date_str = market.get("endDate") or market.get("_event_endDate")
        end_date_hint = _parse_end_date(end_date_str) if end_date_str else None

        parsed = parse_market_question(question, end_date_hint=end_date_hint)
        if not parsed:
            continue

        city = parsed.get("city")
        if city:
            key = city.lower()
            if key not in cities:
                cities[key] = {
                    "city": city,
                    "lat": parsed.get("lat"),
                    "lon": parsed.get("lon"),
                    "tz": parsed.get("tz", "America/New_York"),
                }

    return cities


def compute_sigma_for_cities(cities_dict: dict[str, dict]) -> dict[str, dict[str, float]]:
    """
    Compute σ for a dict of {city_name: {city, lat, lon, tz}, ...}.
    Returns {city_name_lower: {season: rmse_F, ...}, ...}
    """
    result = {}
    for key, city_info in cities_dict.items():
        city_name = city_info["city"]
        lat = city_info["lat"]
        lon = city_info["lon"]
        tz = city_info["tz"]

        if lat is None or lon is None:
            continue

        print(f"  Fetching {city_name} ({lat:.2f}, {lon:.2f})...")
        seasonal_sigma = _fetch_city_sigma(lat, lon, tz, city_name, lead_day=3)
        if seasonal_sigma:
            result[city_name.lower()] = seasonal_sigma

    return result


def _fetch_city_sigma(lat: float, lon: float, tz: str, city_name: str, lead_day: int = 3) -> dict[str, float]:
    """
    Fetch 180 days of historical forecast RMSE per season for a city.
    Returns {season: rmse_in_F, ...}
    """
    session = network.get_session()

    end_date = date.today()
    start_date = end_date - timedelta(days=180)

    params_actual = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
    }

    params_forecast = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": f"temperature_2m_previous_day{lead_day}",
        "temperature_unit": "fahrenheit",
        "timezone": tz,
    }

    try:
        resp_actual = session.get(
            "https://previous-runs-api.open-meteo.com/v1/forecast",
            params=params_actual,
            timeout=30,
        )
        resp_actual.raise_for_status()
        data_actual = resp_actual.json()

        resp_forecast = session.get(
            "https://previous-runs-api.open-meteo.com/v1/forecast",
            params=params_forecast,
            timeout=30,
        )
        resp_forecast.raise_for_status()
        data_forecast = resp_forecast.json()
    except Exception as e:
        print(f"  Warning: failed to fetch {city_name} ({lat}, {lon}): {e}")
        return {}

    hourly_actual = data_actual.get("hourly", {})
    hourly_forecast = data_forecast.get("hourly", {})

    times = hourly_actual.get("time", [])
    actuals = hourly_actual.get("temperature_2m", [])
    forecasts = hourly_forecast.get(f"temperature_2m_previous_day{lead_day}", [])

    if not times or not actuals or not forecasts:
        return {}

    daily_actuals = {}
    daily_forecasts = {}

    for time_str, actual, forecast in zip(times, actuals, forecasts):
        date_str = time_str.split("T")[0]
        if date_str not in daily_actuals:
            daily_actuals[date_str] = []
            daily_forecasts[date_str] = []
        daily_actuals[date_str].append(actual if actual is not None else 0)
        daily_forecasts[date_str].append(forecast if forecast is not None else 0)

    daily_max_actuals = {d: max(vals) for d, vals in daily_actuals.items() if vals}
    daily_max_forecasts = {d: max(vals) for d, vals in daily_forecasts.items() if vals}

    seasonal_rmse = {}
    for season, months in SEASONS.items():
        errors = []
        for date_str in daily_max_actuals.keys():
            month = int(date_str.split("-")[1])
            if month in months and date_str in daily_max_forecasts:
                actual = daily_max_actuals[date_str]
                forecast = daily_max_forecasts[date_str]
                errors.append(actual - forecast)

        if len(errors) >= 20:
            rmse = math.sqrt(mean([e ** 2 for e in errors]))
            seasonal_rmse[season] = rmse

    if not seasonal_rmse:
        all_errors = []
        for date_str in daily_max_actuals.keys():
            if date_str in daily_max_forecasts:
                actual = daily_max_actuals[date_str]
                forecast = daily_max_forecasts[date_str]
                all_errors.append(actual - forecast)

        if all_errors:
            rmse = math.sqrt(mean([e ** 2 for e in all_errors]))
            seasonal_rmse = {season: rmse for season in SEASONS.keys()}

    return seasonal_rmse


def compute_all_city_sigmas() -> dict[str, dict[str, float]]:
    """
    Compute σ per (city, season) and cache in sigma_cache.json.
    Returns {city: {season: sigma_F, ...}, ...}
    """
    if SIGMA_CACHE_PATH.exists():
        try:
            with SIGMA_CACHE_PATH.open(encoding="utf-8") as f:
                cached = json.load(f)
                return cached
        except Exception:
            pass

    print("Computing city/season sigma values...")
    cities = _load_cities_from_markets()

    if not cities:
        print("  No cities found in raw_markets.json. Using fallback.")
        return {}

    result = {}
    for key, city_info in cities.items():
        city_name = city_info["city"]
        lat = city_info["lat"]
        lon = city_info["lon"]
        tz = city_info["tz"]

        if lat is None or lon is None:
            continue

        print(f"  Fetching {city_name} ({lat:.2f}, {lon:.2f})...")
        seasonal_sigma = _fetch_city_sigma(lat, lon, tz, city_name, lead_day=3)
        if seasonal_sigma:
            result[city_name.lower()] = seasonal_sigma

    SIGMA_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with SIGMA_CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)

    print(f"Cached {len(result)} cities to {SIGMA_CACHE_PATH}")
    return result


def get_sigma(city: str, target_date: date, cache: dict | None = None) -> float:
    """
    Get city/season-specific σ in °F.
    Falls back to FALLBACK_SIGMA if not available.
    """
    if cache is None:
        cache = {}

    city_key = city.lower()
    season_sigmas = cache.get(city_key, {})

    if not season_sigmas:
        return FALLBACK_SIGMA

    month = target_date.month
    for season, months in SEASONS.items():
        if month in months:
            return season_sigmas.get(season, FALLBACK_SIGMA)

    return FALLBACK_SIGMA


if __name__ == "__main__":
    sigmas = compute_all_city_sigmas()
    print(f"\nComputed sigmas for {len(sigmas)} cities")
    for city, seasons in list(sigmas.items())[:5]:
        print(f"  {city}: {seasons}")
