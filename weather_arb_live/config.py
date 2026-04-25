from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.getenv("WEATHER_ARB_DATA_DIR", PROJECT_ROOT / "data"))
LOG_DIR = Path(os.getenv("WEATHER_ARB_LOG_DIR", PROJECT_ROOT / "logs"))

WEATHER_CACHE_PATH = DATA_DIR / "weather_cache.json"
SIGMA_CACHE_PATH = DATA_DIR / "sigma_cache.json"
RESIDUALS_CACHE_PATH = DATA_DIR / "empirical_residuals.json"
CALIBRATION_PATH = DATA_DIR / "calibration_table.json"
POSITIONS_PATH = DATA_DIR / "live_positions.json"

MIN_EDGE = 0.12
MAX_POSITION_USD = 50.0
SLIPPAGE = 0.005
TEMP_STD_F = 3.0
MIN_HOURS_BEFORE_CLOSE = 24
MIN_ENTRY_PRICE = 0.25
MIN_MARKET_VOLUME_USD = 500.0
MIN_FORECAST_PROB = 0.65
USE_EMPIRICAL = True
MODEL_NAME = "fixed_v1_no"
MODEL_VARIANT = "Combined"
ENABLE_NO_SIDE = True
MAX_NO_ENTRY_PRICE = 0.75

MAX_LEAD_DAYS = 7
DEFAULT_MODEL = "gfs_seamless"
CONFLUENCE_MODELS = ["gfs_seamless", "ecmwf_ifs025", "gem_seamless", "jma_seamless"]
ENSEMBLE_SIGMA_FLOOR_F = 1.5

CLOB_V2_TEST_HOST = "https://clob-v2.polymarket.com"
CLOB_PRODUCTION_HOST = "https://clob.polymarket.com"
CLOB_V2_CUTOVER_UTC = datetime(2026, 4, 28, 11, 0, tzinfo=timezone.utc)
GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return float(value)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return int(value)


def enable_no_side() -> bool:
    return env_bool("ENABLE_NO_SIDE", ENABLE_NO_SIDE)


def default_clob_host(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current < CLOB_V2_CUTOVER_UTC:
        return CLOB_V2_TEST_HOST
    return CLOB_PRODUCTION_HOST


def clob_host() -> str:
    return os.getenv("POLYMARKET_CLOB_HOST") or default_clob_host()


def dry_run() -> bool:
    return env_bool("DRY_RUN", True)


def poll_interval_seconds() -> int:
    return max(1, env_int("POLL_INTERVAL_MINUTES", 15)) * 60


def max_position_usd() -> float:
    return min(MAX_POSITION_USD, env_float("MAX_POSITION_USD", MAX_POSITION_USD))


def live_market_limit() -> int | None:
    value = env_int("LIVE_MARKET_LIMIT", 0)
    return value if value > 0 else None


@dataclass(frozen=True)
class RuntimeConfig:
    dry_run: bool
    poll_interval_seconds: int
    max_position_usd: float
    clob_host: str
    model_name: str
    model_variant: str
    enable_no_side: bool


def load_runtime_config() -> RuntimeConfig:
    return RuntimeConfig(
        dry_run=dry_run(),
        poll_interval_seconds=poll_interval_seconds(),
        max_position_usd=max_position_usd(),
        clob_host=clob_host(),
        model_name=MODEL_NAME,
        model_variant=MODEL_VARIANT,
        enable_no_side=enable_no_side(),
    )
