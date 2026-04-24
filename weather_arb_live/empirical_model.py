"""Empirical conditional distribution P(actual | forecast) for weather brackets.

Replaces the symmetric Normal(μ=forecast, σ=3°F) with an empirical ECDF of the
residuals `actual − forecast`, keyed by (city, month, metric, lead_bucket). At
query time P(bracket) = ECDF(hi−μ) − ECDF(lo−μ) on the residual sample.

Fallback chain when the residual sample is sparse (n < MIN_SAMPLE):
    1. sigma_model.get_sigma(city, target_date) → Gaussian CDF
    2. fixed Normal σ = FALLBACK_SIGMA_F → Gaussian CDF

Residuals are fetched from Open-Meteo:
    - actuals:   archive-api.open-meteo.com/v1/archive (hourly temperature_2m)
    - forecasts: previous-runs-api.open-meteo.com/v1/forecast
                 (hourly temperature_2m_previous_day1..7)
Both stored and computed in °F; brackets in °C are converted at query time.
"""
from __future__ import annotations
import json
import os
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import date, timedelta

from scipy.stats import norm

from . import config, network
from .sigma_model import get_sigma as _sigma_for_city

RESIDUALS_CACHE_PATH = config.RESIDUALS_CACHE_PATH
SIGMA_CACHE_PATH = config.SIGMA_CACHE_PATH
HISTORY_DAYS = 365           # 1 year of residual samples (reduced from 730 for rate-limit safety)
MIN_SAMPLE = 30              # per-key threshold before falling back to sigma_model
FALLBACK_SIGMA_F = 3.0
FETCH_RETRIES = 5            # transient 429/5xx retries per request
INTER_CITY_SLEEP_SEC = 2.0   # throttle spacing between city fetches

_residuals: dict | None = None
_sigma_cache: dict | None = None
_lock = threading.Lock()
_session = network.get_session()


# ---------- residual keying -----------------------------------------------

def lead_bucket(lead_days: int) -> str:
    """Bucket the 0..7 day lead range so residuals are shared across similar leads."""
    if lead_days <= 1:
        return "1"
    if lead_days <= 3:
        return "2-3"
    return "4-7"


def _residual_key(city: str, month: int, metric: str, lead_days: int) -> str:
    return f"{city.lower()}|{month:02d}|{metric}|{lead_bucket(lead_days)}"


# ---------- cache I/O ------------------------------------------------------

def _load_residuals() -> dict:
    global _residuals
    if _residuals is not None:
        return _residuals
    with _lock:
        if _residuals is not None:
            return _residuals
        if RESIDUALS_CACHE_PATH.exists():
            try:
                with RESIDUALS_CACHE_PATH.open(encoding="utf-8") as f:
                    _residuals = json.load(f)
            except Exception:
                _residuals = {}
        else:
            _residuals = {}
    return _residuals


def _load_sigma_cache() -> dict:
    global _sigma_cache
    if _sigma_cache is not None:
        return _sigma_cache
    with _lock:
        if _sigma_cache is not None:
            return _sigma_cache
        if SIGMA_CACHE_PATH.exists():
            try:
                with SIGMA_CACHE_PATH.open(encoding="utf-8") as f:
                    _sigma_cache = json.load(f)
            except Exception:
                _sigma_cache = {}
        else:
            _sigma_cache = {}
    return _sigma_cache


def _save_residuals(data: dict) -> None:
    RESIDUALS_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = RESIDUALS_CACHE_PATH.with_name(RESIDUALS_CACHE_PATH.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(data, f)
    os.replace(tmp, RESIDUALS_CACHE_PATH)


# ---------- fetching residuals --------------------------------------------

def _fetch_city_residuals(lat: float, lon: float, tz: str, city: str) -> dict[str, list[float]]:
    """Fetch HISTORY_DAYS of daily-max/min residuals per (month, metric, lead_bucket).

    Returns {residual_key: [float, ...]} — keyed by city/month/metric/lead_bucket.
    """
    end = date.today()
    start = end - timedelta(days=HISTORY_DAYS)

    # One call returns temperature_2m (day-0 "actual") plus
    # temperature_2m_previous_day1..7 (lead-N forecasts) — same endpoint that
    # sigma_model uses successfully.
    combined_params = {
        "latitude": lat,
        "longitude": lon,
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "hourly": ",".join(
            ["temperature_2m"]
            + [f"temperature_2m_previous_day{n}" for n in range(1, 8)]
        ),
        "temperature_unit": "fahrenheit",
        "timezone": tz,
    }

    def _get_with_retry(url: str, params: dict) -> dict | None:
        last_err: str | None = None
        for attempt in range(FETCH_RETRIES):
            try:
                r = _session.get(url, params=params, timeout=60)
                if r.status_code == 200:
                    return r.json()
                if r.status_code in (429, 500, 502, 503, 504):
                    backoff = 2.0 ** attempt + 1.0  # 2, 3, 5, 9, 17 s
                    last_err = f"HTTP {r.status_code} (attempt {attempt + 1}/{FETCH_RETRIES})"
                    time.sleep(backoff)
                    continue
                last_err = f"HTTP {r.status_code}"
                return None
            except Exception as e:
                last_err = str(e)
                time.sleep(2.0 ** attempt)
        print(f"  empirical_model: {city} {url.split('/')[-1]} giving up: {last_err}")
        return None

    data = _get_with_retry(
        "https://previous-runs-api.open-meteo.com/v1/forecast", combined_params,
    )
    if data is None:
        return {}
    actual = data
    fc = data

    # Collapse hourly to daily max/min for actuals and for each lead.
    def _daily_extrema(times: list[str], vals: list, fn):
        daily: dict[str, list] = {}
        for t, v in zip(times, vals):
            if v is None:
                continue
            d = t.split("T")[0]
            daily.setdefault(d, []).append(v)
        return {d: fn(vs) for d, vs in daily.items() if vs}

    ah = actual.get("hourly") or {}
    times_a = ah.get("time") or []
    a_vals = ah.get("temperature_2m") or []
    actual_max = _daily_extrema(times_a, a_vals, max)
    actual_min = _daily_extrema(times_a, a_vals, min)

    fh = fc.get("hourly") or {}
    times_f = fh.get("time") or []

    residuals: dict[str, list[float]] = {}

    for lead in range(1, 8):
        var = f"temperature_2m_previous_day{lead}"
        f_vals = fh.get(var) or []
        if not f_vals:
            continue
        fmax = _daily_extrema(times_f, f_vals, max)
        fmin = _daily_extrema(times_f, f_vals, min)
        for day_str, fval in fmax.items():
            aval = actual_max.get(day_str)
            if aval is None:
                continue
            month = int(day_str.split("-")[1])
            key = _residual_key(city, month, "max", lead)
            residuals.setdefault(key, []).append(float(aval) - float(fval))
        for day_str, fval in fmin.items():
            aval = actual_min.get(day_str)
            if aval is None:
                continue
            month = int(day_str.split("-")[1])
            key = _residual_key(city, month, "min", lead)
            residuals.setdefault(key, []).append(float(aval) - float(fval))

    return residuals


def build_empirical_residuals(force: bool = False) -> dict[str, list[float]]:
    """Build and cache residuals for every city present in raw_markets.json.

    Resumes from a partial cache: cities whose residual keys already appear in
    ``data/empirical_residuals.json`` are skipped. Pass ``force=True`` to
    start over.
    """
    existing: dict[str, list[float]] = {}
    if not force and RESIDUALS_CACHE_PATH.exists():
        try:
            with RESIDUALS_CACHE_PATH.open(encoding="utf-8") as f:
                existing = json.load(f) or {}
        except Exception:
            existing = {}

    from .sigma_model import _load_cities_from_markets
    cities = _load_cities_from_markets()
    if not cities:
        print("empirical_model: no cities to fetch (raw_markets.json missing?)")
        return existing

    covered_cities = {k.split("|", 1)[0] for k in existing.keys()}
    if covered_cities:
        print(f"empirical_model: resuming — {len(covered_cities)} cities already cached")

    combined: dict[str, list[float]] = dict(existing)
    for i, (_, info) in enumerate(cities.items(), 1):
        city = info["city"]
        lat, lon, tz = info.get("lat"), info.get("lon"), info.get("tz")
        if lat is None or lon is None:
            continue
        if city.lower() in covered_cities:
            print(f"  empirical_model [{i}/{len(cities)}] {city} — cached, skipping")
            continue
        print(f"  empirical_model [{i}/{len(cities)}] {city} ({lat:.2f},{lon:.2f})")
        res = _fetch_city_residuals(lat, lon, tz, city)
        combined.update(res)
        # Checkpoint after every city so partial progress survives an interrupt.
        if res:
            _save_residuals(combined)
        if i < len(cities):
            time.sleep(INTER_CITY_SLEEP_SEC)

    _save_residuals(combined)
    print(f"empirical_model: saved {len(combined)} keys to {RESIDUALS_CACHE_PATH}")
    return combined


# ---------- ECDF at query time --------------------------------------------

def _ecdf_between(sample_sorted: list[float], lo: float, hi: float) -> float:
    """Fraction of `sample_sorted` (already ascending) falling in (lo, hi]."""
    n = len(sample_sorted)
    if n == 0:
        return 0.0
    lo_idx = bisect_right(sample_sorted, lo)
    hi_idx = bisect_right(sample_sorted, hi)
    return max(0.0, (hi_idx - lo_idx) / n)


def estimate_forecast_prob_empirical(
    city: str,
    target_date: date,
    forecast_mean_f: float,
    bracket_low_f: float | None,
    bracket_high_f: float | None,
    metric: str,
    lead_days: int,
) -> float | None:
    """Return P(bracket_low <= T <= bracket_high) using the empirical residual ECDF.

    Inputs MUST be in °F. Convert upstream if the market question is in °C.
    Returns None only when a forecast mean is not provided — the sparse-cell
    fallback chain (sigma_model → FALLBACK_SIGMA_F) always yields a probability.
    """
    if forecast_mean_f is None:
        return None

    lo_res = (bracket_low_f - forecast_mean_f) if bracket_low_f is not None else -1e9
    hi_res = (bracket_high_f - forecast_mean_f) if bracket_high_f is not None else 1e9

    residuals = _load_residuals()
    key = _residual_key(city, target_date.month, metric, lead_days)
    sample = residuals.get(key, [])

    if len(sample) >= MIN_SAMPLE:
        sample_sorted = sorted(sample)
        return float(max(0.0, min(1.0, _ecdf_between(sample_sorted, lo_res, hi_res))))

    # Fallback 1: sigma_model city/season σ → Normal
    sigma_f = _sigma_for_city(city, target_date, _load_sigma_cache())
    if sigma_f is None or sigma_f <= 0:
        sigma_f = FALLBACK_SIGMA_F
    p = norm.cdf(hi_res, loc=0.0, scale=sigma_f) - norm.cdf(lo_res, loc=0.0, scale=sigma_f)
    return float(max(0.0, min(1.0, p)))


if __name__ == "__main__":
    build_empirical_residuals()
