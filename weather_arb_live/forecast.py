"""Probability model: estimate P(bracket) from a lead-time-pinned weather forecast.

Forecast lead time is pinned via Open-Meteo's previous-runs-api using
``temperature_2m_previous_dayN`` hourly variables. Day 0 (``temperature_2m``)
is the nowcast/0-day-out forecast; ``previous_day1..7`` are the forecasts that
were issued N days before the target day. Valid lead range is 0..7 days.

Confluence: setting ``confluence=True`` averages the lead-pinned forecast
across several open-data NWP models (GFS, ECMWF IFS, GEM, JMA) instead of
using gfs_seamless alone.
"""
from __future__ import annotations
import json
import os
import threading
from datetime import date, timedelta

from scipy.stats import norm

from . import config, network

CACHE_PATH = config.WEATHER_CACHE_PATH
_cache: dict | None = None
_cache_lock = threading.Lock()
_session = network.get_session()


def _load_cache() -> dict:
    global _cache
    if _cache is not None:
        return _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        if CACHE_PATH.exists():
            try:
                with CACHE_PATH.open(encoding="utf-8") as f:
                    _cache = json.load(f)
            except Exception:
                _cache = {}
        else:
            _cache = {}
    return _cache


def _save_cache():
    with _cache_lock:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(_cache, f)
        os.replace(tmp, CACHE_PATH)


_save_counter = 0


MAX_LEAD_DAYS = config.MAX_LEAD_DAYS
DEFAULT_MODEL = config.DEFAULT_MODEL
CONFLUENCE_MODELS = config.CONFLUENCE_MODELS
ENSEMBLE_SIGMA_FLOOR_F = config.ENSEMBLE_SIGMA_FLOOR_F


def _fetch_forecast_response(
    lat: float, lon: float, tz: str, target_date: date, unit: str,
    model: str = DEFAULT_MODEL,
) -> dict | None:
    """Fetch hourly temperature for target_date from the previous-runs-api,
    including temperature_2m (day 0) and temperature_2m_previous_day1..7.

    A single call serves every lead day 0..7 for the (lat, lon, target_date,
    unit, model) tuple. Cached under that composite key.
    """
    global _save_counter
    cache = _load_cache()
    key = (
        f"pr2|{round(lat,2)},{round(lon,2)}|{target_date.isoformat()}|"
        f"{unit}|{model}"
    )
    with _cache_lock:
        if key in cache:
            return cache[key]

    url = "https://previous-runs-api.open-meteo.com/v1/forecast"
    temp_unit = "fahrenheit" if unit == "F" else "celsius"
    hourly_vars = ["temperature_2m"] + [
        f"temperature_2m_previous_day{n}" for n in range(1, MAX_LEAD_DAYS + 1)
    ]
    params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": target_date.isoformat(),
        "end_date": target_date.isoformat(),
        "hourly": ",".join(hourly_vars),
        "timezone": tz,
        "models": model,
        "temperature_unit": temp_unit,
    }
    try:
        r = None
        for attempt in range(4):
            r = _session.get(url, params=params, timeout=30)
            if r.status_code == 200:
                break
            if r.status_code in (429, 502, 503, 504):
                import time as _t
                _t.sleep(1.0 + attempt * 2.0)
                continue
            break
        if r is None or r.status_code != 200:
            with _cache_lock:
                cache[key] = None
                _save_counter += 1
                should_save = (_save_counter % 100 == 0)
            if should_save:
                _save_cache()
            return None
        data = r.json()
    except Exception:
        with _cache_lock:
            cache[key] = None
        return None

    with _cache_lock:
        cache[key] = data
        _save_counter += 1
        should_save = (_save_counter % 100 == 0)
    if should_save:
        _save_cache()
    return data


def _temp_for_lead(data: dict, lead_days: int, metric: str) -> float | None:
    """From a previous-runs-api hourly response, extract the max or min temp
    corresponding to a specific forecast lead day (0..7)."""
    hourly = (data or {}).get("hourly") or {}
    var = (
        "temperature_2m"
        if lead_days == 0
        else f"temperature_2m_previous_day{lead_days}"
    )
    vals = [v for v in hourly.get(var, []) if v is not None]
    if not vals:
        return None
    if metric == "max":
        return float(max(vals))
    if metric == "min":
        return float(min(vals))
    return None


def _fetch_forecast_window(
    lat: float, lon: float, tz: str, as_of_date: date, target_date: date,
    unit: str, model: str = DEFAULT_MODEL,
) -> dict | None:
    """Return a lead-pinned forecast summary for (as_of_date, target_date).

    The dict contains both a max and min derived from the hourly response for
    the specific lead day ``(target - as_of).days``. Returns None when the lead
    is out of the 0..7 horizon or the API call fails.

    Shape: {"target_date": str, "lead_days": int, "model": str,
            "temp_max": float|None, "temp_min": float|None}
    """
    if target_date < as_of_date:
        return None
    lead = (target_date - as_of_date).days
    if lead > MAX_LEAD_DAYS:
        return None
    raw = _fetch_forecast_response(lat, lon, tz, target_date, unit, model=model)
    if not raw:
        return None
    return {
        "target_date": target_date.isoformat(),
        "lead_days": lead,
        "model": model,
        "temp_max": _temp_for_lead(raw, lead, "max"),
        "temp_min": _temp_for_lead(raw, lead, "min"),
    }


def _find_temp_for_date(data: dict | None, target: date, metric: str) -> float | None:
    """Extract the max or min temperature from a ``_fetch_forecast_window``
    response. ``target`` is accepted for signature compatibility with previous
    callers; the lead/target are already baked into ``data``."""
    if not data:
        return None
    if data.get("target_date") and data["target_date"] != target.isoformat():
        return None
    if metric == "max":
        v = data.get("temp_max")
    elif metric == "min":
        v = data.get("temp_min")
    else:
        return None
    return float(v) if v is not None else None


def estimate_forecast_prob(
    lat: float,
    lon: float,
    tz: str,
    target_date: date,
    bracket_low: float | None,
    bracket_high: float | None,
    unit: str,
    as_of_date: date,
    metric: str = "max",
    temp_std_f: float = 3.0,
    confluence: bool = False,
    models: list[str] | None = None,
    ensemble_sigma: bool = False,
    ensemble_sigma_floor_f: float = ENSEMBLE_SIGMA_FLOOR_F,
    use_empirical: bool = False,
    city: str | None = None,
) -> float | None:
    """Return P(bracket_low <= T <= bracket_high) for target_date using the
    forecast that was issued on as_of_date.

    The forecast lead time is pinned via ``temperature_2m_previous_dayN``; no
    ``start_date`` trick is used. Only leads of 0..7 days are supported.

    When ``confluence=True`` the forecast mean is the average across
    ``CONFLUENCE_MODELS`` (or ``models`` if provided). The actual temperature
    is modeled as Normal(mean, std) with std = temp_std_f (F) or temp_std_f/1.8
    (C).

    When ``ensemble_sigma=True`` (implies confluence), σ is the standard
    deviation of the per-model forecasts (model disagreement = market
    uncertainty), floored at ``ensemble_sigma_floor_f`` and only applied when
    >= 2 models returned. Falls back to ``temp_std_f`` if only one model is
    available.
    """
    if target_date < as_of_date:
        return None
    lead = (target_date - as_of_date).days
    if lead > MAX_LEAD_DAYS:
        return None

    if models is None:
        if ensemble_sigma or confluence:
            model_list = CONFLUENCE_MODELS
        else:
            model_list = [DEFAULT_MODEL]
    else:
        model_list = list(models)

    means: list[float] = []
    for m in model_list:
        d = _fetch_forecast_window(lat, lon, tz, as_of_date, target_date, unit, model=m)
        if not d:
            continue
        v = d.get("temp_max") if metric == "max" else d.get("temp_min")
        if v is not None:
            means.append(float(v))
    if not means:
        return None

    mean = sum(means) / len(means)

    # Empirical residual-distribution path (Point 2).
    if use_empirical and city:
        from .empirical_model import estimate_forecast_prob_empirical

        if unit == "C":
            mean_f = mean * 1.8 + 32.0
            lo_f = (bracket_low * 1.8 + 32.0) if bracket_low is not None else None
            hi_f = (bracket_high * 1.8 + 32.0) if bracket_high is not None else None
        else:
            mean_f = mean
            lo_f = bracket_low
            hi_f = bracket_high
        return estimate_forecast_prob_empirical(
            city=city,
            target_date=target_date,
            forecast_mean_f=mean_f,
            bracket_low_f=lo_f,
            bracket_high_f=hi_f,
            metric=metric,
            lead_days=lead,
        )

    if ensemble_sigma and len(means) >= 2:
        # Sample std (ddof=1) of per-model means; convert floor to current unit.
        n = len(means)
        var = sum((v - mean) ** 2 for v in means) / (n - 1)
        spread = var ** 0.5
        floor = ensemble_sigma_floor_f if unit == "F" else ensemble_sigma_floor_f / 1.8
        std = max(floor, spread)
    else:
        std = temp_std_f if unit == "F" else temp_std_f / 1.8

    lo = bracket_low if bracket_low is not None else -1e9
    hi = bracket_high if bracket_high is not None else 1e9
    p = norm.cdf(hi, loc=mean, scale=std) - norm.cdf(lo, loc=mean, scale=std)
    return float(max(0.0, min(1.0, p)))


def flush_cache():
    _save_cache()


if __name__ == "__main__":
    target = date(2025, 7, 15)
    lat, lon, tz = 40.7128, -74.006, "America/New_York"
    print(f"NYC max temp on {target} — lead-pinned forecasts (gfs_seamless):")
    for lead in (0, 1, 3, 5, 7):
        as_of = target - timedelta(days=lead)
        d = _fetch_forecast_window(lat, lon, tz, as_of, target, "F")
        t = d["temp_max"] if d else None
        print(f"  lead {lead}d  as_of={as_of}  forecast_max={t}")

    print("\nConfluence (mean across 4 models):")
    for lead in (0, 1, 3, 5, 7):
        as_of = target - timedelta(days=lead)
        p = estimate_forecast_prob(
            lat=lat, lon=lon, tz=tz, target_date=target,
            bracket_low=85.0, bracket_high=None, unit="F",
            as_of_date=as_of, metric="max", confluence=True,
        )
        print(f"  lead {lead}d  P(max>=85F)={p}")
    flush_cache()
