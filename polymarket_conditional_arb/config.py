from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOTENV_PATH = PROJECT_ROOT / ".env"

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
CLOB_PRODUCTION_HOST = "https://clob.polymarket.com"
CLOB_BATCH_BOOK_LIMIT = 500
MARKET_WS_PRODUCTION_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"

DEFAULT_MARKET_LIMIT = 0
DEFAULT_POLL_INTERVAL_SECONDS = 60
DEFAULT_MIN_NET_PROFIT_USD = 0.0
DEFAULT_MIN_NET_RETURN_BPS = 0.0
DEFAULT_STARTING_CAPITAL_USD = 1_000.0
DEFAULT_TRADE_CEILING_USD = 100.0
DEFAULT_MAX_CAPITAL_USD = DEFAULT_TRADE_CEILING_USD
DEFAULT_SLIPPAGE_BUFFER_BPS = 10.0
DEFAULT_MERGE_COST_USD = 0.02
DEFAULT_GAS_COST_USD = DEFAULT_MERGE_COST_USD
DEFAULT_TAKER_FEE_BPS = 0.0
DEFAULT_TAX_BPS = 0.0
DEFAULT_MAX_BOOK_AGE_SECONDS = 20.0
DEFAULT_INCLUDE_NEG_RISK = False
DEFAULT_MARKET_WS_ENABLED = True
DEFAULT_MARKET_WS_HEARTBEAT_SECONDS = 10.0
DEFAULT_MARKET_WS_MAX_ASSETS_PER_CONNECTION = 500
DEFAULT_MARKET_REFRESH_INTERVAL_SECONDS = 300
DEFAULT_REST_RECONCILE_INTERVAL_SECONDS = 60
DEFAULT_WS_STALE_SECONDS = 5.0
DEFAULT_FAST_START_ENABLED = False
DEFAULT_FAST_START_EVENT_LIMIT = 20
DEFAULT_FAST_START_TOKEN_LIMIT = 500
DEFAULT_UNIVERSE_CACHE_MAX_AGE_SECONDS = 3600
DEFAULT_PAPER_SIMULATION_ENABLED = True
DEFAULT_PAPER_SIM_SEED = 0
DEFAULT_PAPER_LATENCY_MS = 250.0
DEFAULT_PAPER_LATENCY_JITTER_MS = 50.0
DEFAULT_PAPER_SIGNING_LATENCY_MS = 50.0
DEFAULT_PAPER_SETTLEMENT_LATENCY_MS = 1500.0
DEFAULT_PAPER_MAX_FILL_PRICE_MOVE_BPS = 25.0
DEFAULT_PAPER_QUEUE_DEPTH_RATIO = 0.75
DEFAULT_PAPER_QUEUE_FILL_PROBABILITY = 0.95
DEFAULT_PAPER_PARTIAL_FILL_PROBABILITY = 0.15
DEFAULT_PAPER_PARTIAL_FILL_MIN_RATIO = 0.50
DEFAULT_PAPER_SUBMIT_FAILURE_PROBABILITY = 0.005
DEFAULT_PAPER_ACCEPT_FAILURE_PROBABILITY = 0.0025
DEFAULT_PAPER_FILL_FAILURE_PROBABILITY = 0.01
DEFAULT_PAPER_CANCEL_FAILURE_PROBABILITY = 0.0025
DEFAULT_PAPER_THROTTLE_MAX_SUBMISSIONS_PER_SECOND = 8
DEFAULT_PAPER_THROTTLE_QUANTITY_RATIO = 0.50
DEFAULT_PAPER_ADVERSE_SELECTION_PROBABILITY = 0.25
DEFAULT_PAPER_ADVERSE_DEPTH_REMOVAL_RATIO = 0.50
DEFAULT_PAPER_ADVERSE_PRICE_MOVE_BPS = 10.0
DEFAULT_PAPER_LATENCY_MODE = "fixed"
DEFAULT_PAPER_LOCAL_TIMEOUT_MS = 0.0
DEFAULT_PAPER_TELEMETRY_LATENCY_WINDOW = 50
DEFAULT_PAPER_LATENCY_JITTER_SEED_SCOPE = "market_book_stage"
DEFAULT_PAPER_FILL_ELIGIBILITY_MODE = "strict_public_depth"
DEFAULT_PAPER_ALLOW_TRADE_PRINT_FILL_SUPPORT = True
DEFAULT_PAPER_ALLOW_DETERMINISTIC_FILL_FALLBACK = False
DEFAULT_PAPER_SETTLEMENT_ENABLED = True
DEFAULT_PAPER_SETTLEMENT_SOURCE = "public_metadata_or_ws"
DEFAULT_PAPER_UNMATCHED_OPEN_VALUATION = "best_bid_midpoint_or_zero"
DEFAULT_PAPER_SETTLEMENT_REQUIRE_WINNER = True
DEFAULT_PAPER_SLIPPAGE_MODE = "fixed_plus_calibrated"
DEFAULT_PAPER_SLIPPAGE_MAX_BPS = 100.0
DEFAULT_PAPER_SLIPPAGE_LOOKBACK_EVENTS = 50
DEFAULT_PAPER_SLIPPAGE_COMBINE_MODE = "max"

LATENCY_MODES = {"fixed", "telemetry"}
LATENCY_JITTER_SEED_SCOPES = {"global", "market", "market_stage", "market_book_stage"}
FILL_ELIGIBILITY_MODES = {"strict_public_depth"}
SETTLEMENT_SOURCES = {"public_metadata_or_ws"}
UNMATCHED_OPEN_VALUATION_MODES = {"best_bid_midpoint_or_zero"}
SLIPPAGE_MODES = {"fixed_only", "fixed_plus_calibrated"}
SLIPPAGE_COMBINE_MODES = {"max", "add"}


@dataclass(frozen=True)
class PaperExecutionSimulationConfig:
    enabled: bool = DEFAULT_PAPER_SIMULATION_ENABLED
    seed: int = DEFAULT_PAPER_SIM_SEED
    latency_ms: float = DEFAULT_PAPER_LATENCY_MS
    latency_jitter_ms: float = DEFAULT_PAPER_LATENCY_JITTER_MS
    latency_mode: str = DEFAULT_PAPER_LATENCY_MODE
    local_timeout_ms: float = DEFAULT_PAPER_LOCAL_TIMEOUT_MS
    telemetry_latency_window: int = DEFAULT_PAPER_TELEMETRY_LATENCY_WINDOW
    latency_jitter_seed_scope: str = DEFAULT_PAPER_LATENCY_JITTER_SEED_SCOPE
    signing_latency_ms: float = DEFAULT_PAPER_SIGNING_LATENCY_MS
    settlement_latency_ms: float = DEFAULT_PAPER_SETTLEMENT_LATENCY_MS
    max_fill_price_move_bps: float = DEFAULT_PAPER_MAX_FILL_PRICE_MOVE_BPS
    fill_eligibility_mode: str = DEFAULT_PAPER_FILL_ELIGIBILITY_MODE
    allow_trade_print_fill_support: bool = DEFAULT_PAPER_ALLOW_TRADE_PRINT_FILL_SUPPORT
    allow_deterministic_fill_fallback: bool = DEFAULT_PAPER_ALLOW_DETERMINISTIC_FILL_FALLBACK
    settlement_enabled: bool = DEFAULT_PAPER_SETTLEMENT_ENABLED
    settlement_source: str = DEFAULT_PAPER_SETTLEMENT_SOURCE
    unmatched_open_valuation: str = DEFAULT_PAPER_UNMATCHED_OPEN_VALUATION
    settlement_require_winner: bool = DEFAULT_PAPER_SETTLEMENT_REQUIRE_WINNER
    slippage_mode: str = DEFAULT_PAPER_SLIPPAGE_MODE
    slippage_max_bps: float = DEFAULT_PAPER_SLIPPAGE_MAX_BPS
    slippage_lookback_events: int = DEFAULT_PAPER_SLIPPAGE_LOOKBACK_EVENTS
    slippage_combine_mode: str = DEFAULT_PAPER_SLIPPAGE_COMBINE_MODE
    queue_depth_ratio: float = DEFAULT_PAPER_QUEUE_DEPTH_RATIO
    queue_fill_probability: float = DEFAULT_PAPER_QUEUE_FILL_PROBABILITY
    partial_fill_probability: float = DEFAULT_PAPER_PARTIAL_FILL_PROBABILITY
    partial_fill_min_ratio: float = DEFAULT_PAPER_PARTIAL_FILL_MIN_RATIO
    submit_failure_probability: float = DEFAULT_PAPER_SUBMIT_FAILURE_PROBABILITY
    accept_failure_probability: float = DEFAULT_PAPER_ACCEPT_FAILURE_PROBABILITY
    fill_failure_probability: float = DEFAULT_PAPER_FILL_FAILURE_PROBABILITY
    cancel_failure_probability: float = DEFAULT_PAPER_CANCEL_FAILURE_PROBABILITY
    throttle_max_submissions_per_second: int = DEFAULT_PAPER_THROTTLE_MAX_SUBMISSIONS_PER_SECOND
    throttle_quantity_ratio: float = DEFAULT_PAPER_THROTTLE_QUANTITY_RATIO
    adverse_selection_probability: float = DEFAULT_PAPER_ADVERSE_SELECTION_PROBABILITY
    adverse_depth_removal_ratio: float = DEFAULT_PAPER_ADVERSE_DEPTH_REMOVAL_RATIO
    adverse_price_move_bps: float = DEFAULT_PAPER_ADVERSE_PRICE_MOVE_BPS

    def __post_init__(self) -> None:
        if self.latency_mode not in LATENCY_MODES:
            raise ValueError(
                f"latency_mode must be one of {sorted(LATENCY_MODES)}; got {self.latency_mode!r}"
            )
        if self.latency_jitter_seed_scope not in LATENCY_JITTER_SEED_SCOPES:
            raise ValueError(
                "latency_jitter_seed_scope must be one of "
                f"{sorted(LATENCY_JITTER_SEED_SCOPES)}; got {self.latency_jitter_seed_scope!r}"
            )
        if self.fill_eligibility_mode not in FILL_ELIGIBILITY_MODES:
            raise ValueError(
                "fill_eligibility_mode must be one of "
                f"{sorted(FILL_ELIGIBILITY_MODES)}; got {self.fill_eligibility_mode!r}"
            )
        if self.settlement_source not in SETTLEMENT_SOURCES:
            raise ValueError(
                f"settlement_source must be one of {sorted(SETTLEMENT_SOURCES)}; got {self.settlement_source!r}"
            )
        if self.unmatched_open_valuation not in UNMATCHED_OPEN_VALUATION_MODES:
            raise ValueError(
                "unmatched_open_valuation must be one of "
                f"{sorted(UNMATCHED_OPEN_VALUATION_MODES)}; got {self.unmatched_open_valuation!r}"
            )
        if self.slippage_mode not in SLIPPAGE_MODES:
            raise ValueError(
                f"slippage_mode must be one of {sorted(SLIPPAGE_MODES)}; got {self.slippage_mode!r}"
            )
        if self.slippage_combine_mode not in SLIPPAGE_COMBINE_MODES:
            raise ValueError(
                "slippage_combine_mode must be one of "
                f"{sorted(SLIPPAGE_COMBINE_MODES)}; got {self.slippage_combine_mode!r}"
            )
        if self.local_timeout_ms < 0.0:
            raise ValueError("local_timeout_ms must be greater than or equal to 0")
        if self.telemetry_latency_window < 1:
            raise ValueError("telemetry_latency_window must be greater than or equal to 1")
        if self.slippage_max_bps < 0.0:
            raise ValueError("slippage_max_bps must be greater than or equal to 0")
        if self.slippage_lookback_events < 1:
            raise ValueError("slippage_lookback_events must be greater than or equal to 1")

    @classmethod
    def zero_friction(cls) -> "PaperExecutionSimulationConfig":
        return cls(
            enabled=False,
            latency_ms=0.0,
            latency_jitter_ms=0.0,
            latency_mode=DEFAULT_PAPER_LATENCY_MODE,
            local_timeout_ms=0.0,
            telemetry_latency_window=DEFAULT_PAPER_TELEMETRY_LATENCY_WINDOW,
            latency_jitter_seed_scope=DEFAULT_PAPER_LATENCY_JITTER_SEED_SCOPE,
            signing_latency_ms=0.0,
            settlement_latency_ms=0.0,
            max_fill_price_move_bps=0.0,
            fill_eligibility_mode=DEFAULT_PAPER_FILL_ELIGIBILITY_MODE,
            allow_trade_print_fill_support=DEFAULT_PAPER_ALLOW_TRADE_PRINT_FILL_SUPPORT,
            allow_deterministic_fill_fallback=False,
            settlement_enabled=False,
            settlement_source=DEFAULT_PAPER_SETTLEMENT_SOURCE,
            unmatched_open_valuation=DEFAULT_PAPER_UNMATCHED_OPEN_VALUATION,
            settlement_require_winner=DEFAULT_PAPER_SETTLEMENT_REQUIRE_WINNER,
            slippage_mode="fixed_only",
            slippage_max_bps=0.0,
            slippage_lookback_events=DEFAULT_PAPER_SLIPPAGE_LOOKBACK_EVENTS,
            slippage_combine_mode=DEFAULT_PAPER_SLIPPAGE_COMBINE_MODE,
            queue_depth_ratio=0.0,
            queue_fill_probability=0.0,
            partial_fill_probability=0.0,
            partial_fill_min_ratio=0.0,
            submit_failure_probability=0.0,
            accept_failure_probability=0.0,
            fill_failure_probability=0.0,
            cancel_failure_probability=0.0,
            throttle_max_submissions_per_second=0,
            throttle_quantity_ratio=0.0,
            adverse_selection_probability=0.0,
            adverse_depth_removal_ratio=0.0,
            adverse_price_move_bps=0.0,
        )

    @property
    def is_zero_friction(self) -> bool:
        if not self.enabled:
            return True
        return (
            self.latency_ms <= 0.0
            and self.latency_jitter_ms <= 0.0
            and self.local_timeout_ms <= 0.0
            and self.signing_latency_ms <= 0.0
            and self.settlement_latency_ms <= 0.0
            and self.max_fill_price_move_bps <= 0.0
            and self.slippage_max_bps <= 0.0
            and self.queue_depth_ratio <= 0.0
            and self.queue_fill_probability <= 0.0
            and self.partial_fill_probability <= 0.0
            and self.submit_failure_probability <= 0.0
            and self.accept_failure_probability <= 0.0
            and self.fill_failure_probability <= 0.0
            and self.cancel_failure_probability <= 0.0
            and self.throttle_max_submissions_per_second <= 0
            and self.throttle_quantity_ratio <= 0.0
            and self.adverse_selection_probability <= 0.0
            and self.adverse_depth_removal_ratio <= 0.0
            and self.adverse_price_move_bps <= 0.0
        )


def _decode_dotenv_value(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        return value[1:-1]
    comment_start = value.find(" #")
    if comment_start != -1:
        value = value[:comment_start].rstrip()
    return value


def load_dotenv(path: str | Path = DOTENV_PATH, *, override: bool = False) -> None:
    env_path = Path(path)
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
            continue
        if override or name not in os.environ:
            os.environ[name] = _decode_dotenv_value(raw_value)


load_dotenv()

TRUE_ENV_VALUES = {"1", "true", "yes", "y", "on"}
FALSE_ENV_VALUES = {"0", "false", "no", "n", "off"}


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized in TRUE_ENV_VALUES:
        return True
    if normalized in FALSE_ENV_VALUES:
        return False
    allowed = ", ".join(sorted(TRUE_ENV_VALUES | FALSE_ENV_VALUES))
    raise ValueError(f"{name} must be a boolean value ({allowed}); got {value!r}")


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


def _non_negative_float_env(name: str, default: float) -> float:
    value = env_float(name, default)
    if value < 0.0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return value


def _non_negative_int_env(name: str, default: int) -> int:
    value = env_int(name, default)
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to 0")
    return value


def _probability_env(name: str, default: float) -> float:
    value = env_float(name, default)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _ratio_env(name: str, default: float) -> float:
    value = env_float(name, default)
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return value


def _choice_env(name: str, default: str, allowed: set[str]) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    normalized = value.strip().lower()
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}")
    return normalized


def data_dir() -> Path:
    return Path(os.getenv("COND_ARB_DATA_DIR", PROJECT_ROOT / "data"))


def log_dir() -> Path:
    return Path(os.getenv("COND_ARB_LOG_DIR", PROJECT_ROOT / "logs"))


def market_limit() -> int | None:
    value = env_int("COND_ARB_MARKET_LIMIT", DEFAULT_MARKET_LIMIT)
    return value if value > 0 else None


def poll_interval_seconds() -> int:
    return max(1, env_int("COND_ARB_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS))


def min_net_profit_usd() -> float:
    return max(0.0, env_float("COND_ARB_MIN_NET_PROFIT_USD", DEFAULT_MIN_NET_PROFIT_USD))


def min_net_return_bps() -> float:
    return max(0.0, env_float("COND_ARB_MIN_NET_RETURN_BPS", DEFAULT_MIN_NET_RETURN_BPS))


def max_capital_usd() -> float:
    value = env_float("COND_ARB_MAX_CAPITAL_USD", trade_ceiling_usd())
    if value <= 0:
        raise ValueError("COND_ARB_MAX_CAPITAL_USD must be greater than 0")
    return value


def starting_capital_usd() -> float:
    value = env_float("COND_ARB_STARTING_CAPITAL_USD", DEFAULT_STARTING_CAPITAL_USD)
    if value <= 0:
        raise ValueError("COND_ARB_STARTING_CAPITAL_USD must be greater than 0")
    return value


def trade_ceiling_usd() -> float:
    value = env_float("COND_ARB_TRADE_CEILING_USD", DEFAULT_TRADE_CEILING_USD)
    if value <= 0:
        raise ValueError("COND_ARB_TRADE_CEILING_USD must be greater than 0")
    return value


def slippage_buffer_bps() -> float:
    return max(0.0, env_float("COND_ARB_SLIPPAGE_BUFFER_BPS", DEFAULT_SLIPPAGE_BUFFER_BPS))


def merge_cost_usd() -> float:
    if os.getenv("COND_ARB_MERGE_COST_USD") not in (None, ""):
        return max(0.0, env_float("COND_ARB_MERGE_COST_USD", DEFAULT_MERGE_COST_USD))
    return max(0.0, env_float("COND_ARB_GAS_COST_USD", DEFAULT_MERGE_COST_USD))


def gas_cost_usd() -> float:
    return merge_cost_usd()


def taker_fee_bps() -> float:
    return max(0.0, env_float("COND_ARB_TAKER_FEE_BPS", DEFAULT_TAKER_FEE_BPS))


def tax_bps() -> float:
    return max(0.0, env_float("COND_ARB_TAX_BPS", DEFAULT_TAX_BPS))


def max_book_age_seconds() -> float:
    return max(1.0, env_float("COND_ARB_MAX_BOOK_AGE_SECONDS", DEFAULT_MAX_BOOK_AGE_SECONDS))


def include_neg_risk() -> bool:
    return env_bool("COND_ARB_INCLUDE_NEG_RISK", DEFAULT_INCLUDE_NEG_RISK)


def _validated_url(value: str, *, env_name: str, allowed_schemes: set[str]) -> str:
    raw = value.strip()
    schemes = "/".join(sorted(allowed_schemes))
    if not raw:
        raise ValueError(f"{env_name} must be a non-empty {schemes} URL")
    parsed = urlparse(raw)
    if parsed.scheme not in allowed_schemes:
        raise ValueError(f"{env_name} must use {schemes} scheme; got {parsed.scheme or 'missing'}")
    if not parsed.netloc:
        raise ValueError(f"{env_name} must include a host; got {raw!r}")
    return raw


def clob_host() -> str:
    return _validated_url(
        os.getenv("POLYMARKET_CLOB_HOST", CLOB_PRODUCTION_HOST),
        env_name="POLYMARKET_CLOB_HOST",
        allowed_schemes={"http", "https"},
    ).rstrip("/")


def market_ws_enabled() -> bool:
    return env_bool("COND_ARB_MARKET_WS_ENABLED", DEFAULT_MARKET_WS_ENABLED)


def market_ws_endpoint() -> str:
    return _validated_url(
        os.getenv("COND_ARB_MARKET_WS_ENDPOINT", MARKET_WS_PRODUCTION_ENDPOINT),
        env_name="COND_ARB_MARKET_WS_ENDPOINT",
        allowed_schemes={"ws", "wss"},
    )


def market_ws_heartbeat_seconds() -> float:
    return max(0.1, env_float("COND_ARB_MARKET_WS_HEARTBEAT_SECONDS", DEFAULT_MARKET_WS_HEARTBEAT_SECONDS))


def market_ws_max_assets_per_connection() -> int:
    return max(1, env_int("COND_ARB_MARKET_WS_MAX_ASSETS_PER_CONNECTION", DEFAULT_MARKET_WS_MAX_ASSETS_PER_CONNECTION))


def market_refresh_interval_seconds() -> int:
    return max(1, env_int("COND_ARB_MARKET_REFRESH_INTERVAL_SECONDS", DEFAULT_MARKET_REFRESH_INTERVAL_SECONDS))


def rest_reconcile_interval_seconds() -> int:
    return max(1, env_int("COND_ARB_REST_RECONCILE_INTERVAL_SECONDS", DEFAULT_REST_RECONCILE_INTERVAL_SECONDS))


def ws_stale_seconds() -> float:
    return max(0.1, env_float("COND_ARB_WS_STALE_SECONDS", DEFAULT_WS_STALE_SECONDS))


def fast_start_enabled() -> bool:
    return env_bool("COND_ARB_FAST_START_ENABLED", DEFAULT_FAST_START_ENABLED)


def fast_start_event_limit() -> int:
    return max(1, env_int("COND_ARB_FAST_START_EVENT_LIMIT", DEFAULT_FAST_START_EVENT_LIMIT))


def fast_start_token_limit() -> int:
    return max(2, env_int("COND_ARB_FAST_START_TOKEN_LIMIT", DEFAULT_FAST_START_TOKEN_LIMIT))


def universe_cache_max_age_seconds() -> int:
    return max(0, env_int("COND_ARB_UNIVERSE_CACHE_MAX_AGE_SECONDS", DEFAULT_UNIVERSE_CACHE_MAX_AGE_SECONDS))


def paper_execution_simulation_config() -> PaperExecutionSimulationConfig:
    return PaperExecutionSimulationConfig(
        enabled=env_bool("COND_ARB_PAPER_SIMULATION_ENABLED", DEFAULT_PAPER_SIMULATION_ENABLED),
        seed=env_int("COND_ARB_PAPER_SIM_SEED", DEFAULT_PAPER_SIM_SEED),
        latency_ms=_non_negative_float_env("COND_ARB_PAPER_LATENCY_MS", DEFAULT_PAPER_LATENCY_MS),
        latency_jitter_ms=_non_negative_float_env("COND_ARB_PAPER_LATENCY_JITTER_MS", DEFAULT_PAPER_LATENCY_JITTER_MS),
        latency_mode=_choice_env("COND_ARB_PAPER_LATENCY_MODE", DEFAULT_PAPER_LATENCY_MODE, LATENCY_MODES),
        local_timeout_ms=_non_negative_float_env(
            "COND_ARB_PAPER_LOCAL_TIMEOUT_MS",
            DEFAULT_PAPER_LOCAL_TIMEOUT_MS,
        ),
        telemetry_latency_window=max(
            1,
            _non_negative_int_env(
                "COND_ARB_PAPER_TELEMETRY_LATENCY_WINDOW",
                DEFAULT_PAPER_TELEMETRY_LATENCY_WINDOW,
            ),
        ),
        latency_jitter_seed_scope=_choice_env(
            "COND_ARB_PAPER_LATENCY_JITTER_SEED_SCOPE",
            DEFAULT_PAPER_LATENCY_JITTER_SEED_SCOPE,
            LATENCY_JITTER_SEED_SCOPES,
        ),
        signing_latency_ms=_non_negative_float_env(
            "COND_ARB_PAPER_SIGNING_LATENCY_MS",
            DEFAULT_PAPER_SIGNING_LATENCY_MS,
        ),
        settlement_latency_ms=_non_negative_float_env(
            "COND_ARB_PAPER_SETTLEMENT_LATENCY_MS",
            DEFAULT_PAPER_SETTLEMENT_LATENCY_MS,
        ),
        max_fill_price_move_bps=_non_negative_float_env(
            "COND_ARB_PAPER_MAX_FILL_PRICE_MOVE_BPS",
            DEFAULT_PAPER_MAX_FILL_PRICE_MOVE_BPS,
        ),
        fill_eligibility_mode=_choice_env(
            "COND_ARB_PAPER_FILL_ELIGIBILITY_MODE",
            DEFAULT_PAPER_FILL_ELIGIBILITY_MODE,
            FILL_ELIGIBILITY_MODES,
        ),
        allow_trade_print_fill_support=env_bool(
            "COND_ARB_PAPER_ALLOW_TRADE_PRINT_FILL_SUPPORT",
            DEFAULT_PAPER_ALLOW_TRADE_PRINT_FILL_SUPPORT,
        ),
        allow_deterministic_fill_fallback=env_bool(
            "COND_ARB_PAPER_ALLOW_DETERMINISTIC_FILL_FALLBACK",
            DEFAULT_PAPER_ALLOW_DETERMINISTIC_FILL_FALLBACK,
        ),
        settlement_enabled=env_bool(
            "COND_ARB_PAPER_SETTLEMENT_ENABLED",
            DEFAULT_PAPER_SETTLEMENT_ENABLED,
        ),
        settlement_source=_choice_env(
            "COND_ARB_PAPER_SETTLEMENT_SOURCE",
            DEFAULT_PAPER_SETTLEMENT_SOURCE,
            SETTLEMENT_SOURCES,
        ),
        unmatched_open_valuation=_choice_env(
            "COND_ARB_PAPER_UNMATCHED_OPEN_VALUATION",
            DEFAULT_PAPER_UNMATCHED_OPEN_VALUATION,
            UNMATCHED_OPEN_VALUATION_MODES,
        ),
        settlement_require_winner=env_bool(
            "COND_ARB_PAPER_SETTLEMENT_REQUIRE_WINNER",
            DEFAULT_PAPER_SETTLEMENT_REQUIRE_WINNER,
        ),
        slippage_mode=_choice_env(
            "COND_ARB_PAPER_SLIPPAGE_MODE",
            DEFAULT_PAPER_SLIPPAGE_MODE,
            SLIPPAGE_MODES,
        ),
        slippage_max_bps=_non_negative_float_env(
            "COND_ARB_PAPER_SLIPPAGE_MAX_BPS",
            DEFAULT_PAPER_SLIPPAGE_MAX_BPS,
        ),
        slippage_lookback_events=max(
            1,
            _non_negative_int_env(
                "COND_ARB_PAPER_SLIPPAGE_LOOKBACK_EVENTS",
                DEFAULT_PAPER_SLIPPAGE_LOOKBACK_EVENTS,
            ),
        ),
        slippage_combine_mode=_choice_env(
            "COND_ARB_PAPER_SLIPPAGE_COMBINE_MODE",
            DEFAULT_PAPER_SLIPPAGE_COMBINE_MODE,
            SLIPPAGE_COMBINE_MODES,
        ),
        queue_depth_ratio=_ratio_env("COND_ARB_PAPER_QUEUE_DEPTH_RATIO", DEFAULT_PAPER_QUEUE_DEPTH_RATIO),
        queue_fill_probability=_probability_env(
            "COND_ARB_PAPER_QUEUE_FILL_PROBABILITY",
            DEFAULT_PAPER_QUEUE_FILL_PROBABILITY,
        ),
        partial_fill_probability=_probability_env(
            "COND_ARB_PAPER_PARTIAL_FILL_PROBABILITY",
            DEFAULT_PAPER_PARTIAL_FILL_PROBABILITY,
        ),
        partial_fill_min_ratio=_ratio_env(
            "COND_ARB_PAPER_PARTIAL_FILL_MIN_RATIO",
            DEFAULT_PAPER_PARTIAL_FILL_MIN_RATIO,
        ),
        submit_failure_probability=_probability_env(
            "COND_ARB_PAPER_SUBMIT_FAILURE_PROBABILITY",
            DEFAULT_PAPER_SUBMIT_FAILURE_PROBABILITY,
        ),
        accept_failure_probability=_probability_env(
            "COND_ARB_PAPER_ACCEPT_FAILURE_PROBABILITY",
            DEFAULT_PAPER_ACCEPT_FAILURE_PROBABILITY,
        ),
        fill_failure_probability=_probability_env(
            "COND_ARB_PAPER_FILL_FAILURE_PROBABILITY",
            DEFAULT_PAPER_FILL_FAILURE_PROBABILITY,
        ),
        cancel_failure_probability=_probability_env(
            "COND_ARB_PAPER_CANCEL_FAILURE_PROBABILITY",
            DEFAULT_PAPER_CANCEL_FAILURE_PROBABILITY,
        ),
        throttle_max_submissions_per_second=_non_negative_int_env(
            "COND_ARB_PAPER_THROTTLE_MAX_SUBMISSIONS_PER_SECOND",
            DEFAULT_PAPER_THROTTLE_MAX_SUBMISSIONS_PER_SECOND,
        ),
        throttle_quantity_ratio=_ratio_env(
            "COND_ARB_PAPER_THROTTLE_QUANTITY_RATIO",
            DEFAULT_PAPER_THROTTLE_QUANTITY_RATIO,
        ),
        adverse_selection_probability=_probability_env(
            "COND_ARB_PAPER_ADVERSE_SELECTION_PROBABILITY",
            DEFAULT_PAPER_ADVERSE_SELECTION_PROBABILITY,
        ),
        adverse_depth_removal_ratio=_ratio_env(
            "COND_ARB_PAPER_ADVERSE_DEPTH_REMOVAL_RATIO",
            DEFAULT_PAPER_ADVERSE_DEPTH_REMOVAL_RATIO,
        ),
        adverse_price_move_bps=_non_negative_float_env(
            "COND_ARB_PAPER_ADVERSE_PRICE_MOVE_BPS",
            DEFAULT_PAPER_ADVERSE_PRICE_MOVE_BPS,
        ),
    )


def event_log_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "conditional_arb_events.jsonl"


def market_universe_cache_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "market_universe_cache.json"


def paper_portfolio_instance_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "paper_portfolio_instance.json"


def paper_portfolio_events_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "paper_portfolio_events.jsonl"


def paper_portfolio_runtime_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "paper_portfolio_runtime.json"


def scan_log_path(base_log_dir: Path | None = None) -> Path:
    return (base_log_dir or log_dir()) / "conditional_arb_scan.log"


@dataclass(frozen=True)
class ScanConfig:
    data_dir: Path
    log_dir: Path
    clob_host: str
    market_limit: int | None
    poll_interval_seconds: int
    min_net_profit_usd: float
    min_net_return_bps: float
    max_capital_usd: float
    starting_capital_usd: float = DEFAULT_STARTING_CAPITAL_USD
    trade_ceiling_usd: float = DEFAULT_TRADE_CEILING_USD
    slippage_buffer_bps: float = DEFAULT_SLIPPAGE_BUFFER_BPS
    gas_cost_usd: float = DEFAULT_GAS_COST_USD
    merge_cost_usd: float = DEFAULT_MERGE_COST_USD
    taker_fee_bps: float = DEFAULT_TAKER_FEE_BPS
    tax_bps: float = DEFAULT_TAX_BPS
    max_book_age_seconds: float = DEFAULT_MAX_BOOK_AGE_SECONDS
    include_neg_risk: bool = DEFAULT_INCLUDE_NEG_RISK
    market_ws_enabled: bool = DEFAULT_MARKET_WS_ENABLED
    market_ws_endpoint: str = MARKET_WS_PRODUCTION_ENDPOINT
    market_ws_heartbeat_seconds: float = DEFAULT_MARKET_WS_HEARTBEAT_SECONDS
    market_ws_max_assets_per_connection: int = DEFAULT_MARKET_WS_MAX_ASSETS_PER_CONNECTION
    market_refresh_interval_seconds: int = DEFAULT_MARKET_REFRESH_INTERVAL_SECONDS
    rest_reconcile_interval_seconds: int = DEFAULT_REST_RECONCILE_INTERVAL_SECONDS
    ws_stale_seconds: float = DEFAULT_WS_STALE_SECONDS
    fast_start_enabled: bool = DEFAULT_FAST_START_ENABLED
    fast_start_event_limit: int = DEFAULT_FAST_START_EVENT_LIMIT
    fast_start_token_limit: int = DEFAULT_FAST_START_TOKEN_LIMIT
    universe_cache_max_age_seconds: int = DEFAULT_UNIVERSE_CACHE_MAX_AGE_SECONDS
    paper_simulation: PaperExecutionSimulationConfig = field(default_factory=PaperExecutionSimulationConfig)

    @property
    def event_log_path(self) -> Path:
        return event_log_path(self.data_dir)

    @property
    def market_universe_cache_path(self) -> Path:
        return market_universe_cache_path(self.data_dir)

    @property
    def paper_portfolio_instance_path(self) -> Path:
        return paper_portfolio_instance_path(self.data_dir)

    @property
    def paper_portfolio_events_path(self) -> Path:
        return paper_portfolio_events_path(self.data_dir)

    @property
    def paper_portfolio_runtime_path(self) -> Path:
        return paper_portfolio_runtime_path(self.data_dir)

    @property
    def scan_log_path(self) -> Path:
        return scan_log_path(self.log_dir)


def load_scan_config() -> ScanConfig:
    return ScanConfig(
        data_dir=data_dir(),
        log_dir=log_dir(),
        clob_host=clob_host(),
        market_limit=market_limit(),
        poll_interval_seconds=poll_interval_seconds(),
        min_net_profit_usd=min_net_profit_usd(),
        min_net_return_bps=min_net_return_bps(),
        max_capital_usd=max_capital_usd(),
        starting_capital_usd=starting_capital_usd(),
        trade_ceiling_usd=trade_ceiling_usd(),
        slippage_buffer_bps=slippage_buffer_bps(),
        gas_cost_usd=gas_cost_usd(),
        merge_cost_usd=merge_cost_usd(),
        taker_fee_bps=taker_fee_bps(),
        tax_bps=tax_bps(),
        max_book_age_seconds=max_book_age_seconds(),
        include_neg_risk=include_neg_risk(),
        market_ws_enabled=market_ws_enabled(),
        market_ws_endpoint=market_ws_endpoint(),
        market_ws_heartbeat_seconds=market_ws_heartbeat_seconds(),
        market_ws_max_assets_per_connection=market_ws_max_assets_per_connection(),
        market_refresh_interval_seconds=market_refresh_interval_seconds(),
        rest_reconcile_interval_seconds=rest_reconcile_interval_seconds(),
        ws_stale_seconds=ws_stale_seconds(),
        fast_start_enabled=fast_start_enabled(),
        fast_start_event_limit=fast_start_event_limit(),
        fast_start_token_limit=fast_start_token_limit(),
        universe_cache_max_age_seconds=universe_cache_max_age_seconds(),
        paper_simulation=paper_execution_simulation_config(),
    )
