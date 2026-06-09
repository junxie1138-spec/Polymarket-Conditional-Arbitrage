from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

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


def clob_host() -> str:
    return os.getenv("POLYMARKET_CLOB_HOST", CLOB_PRODUCTION_HOST).rstrip("/")


def market_ws_enabled() -> bool:
    return env_bool("COND_ARB_MARKET_WS_ENABLED", DEFAULT_MARKET_WS_ENABLED)


def market_ws_endpoint() -> str:
    return os.getenv("COND_ARB_MARKET_WS_ENDPOINT", MARKET_WS_PRODUCTION_ENDPOINT).strip()


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


def event_log_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "conditional_arb_events.jsonl"


def paper_portfolio_instance_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "paper_portfolio_instance.json"


def paper_portfolio_events_path(base_data_dir: Path | None = None) -> Path:
    return (base_data_dir or data_dir()) / "paper_portfolio_events.jsonl"


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

    @property
    def event_log_path(self) -> Path:
        return event_log_path(self.data_dir)

    @property
    def paper_portfolio_instance_path(self) -> Path:
        return paper_portfolio_instance_path(self.data_dir)

    @property
    def paper_portfolio_events_path(self) -> Path:
        return paper_portfolio_events_path(self.data_dir)

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
    )
