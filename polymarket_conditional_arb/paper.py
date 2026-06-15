from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from . import config
from .arb_models import BinaryMarket, BookLevel, OrderBookSide
from .event_log import AppendOnlyJsonl, jsonable, utc_iso

EPSILON = 1e-9
SCHEMA_VERSION = 1
PORTFOLIO_SCHEMA_VERSION = 2
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class FillTimeBookEvidence:
    source: str
    yes_book: OrderBookSide | None = None
    no_book: OrderBookSide | None = None
    observed_at: datetime | None = None
    snapshot_ready: Mapping[str, bool] = field(default_factory=dict)
    snapshot_generation: Mapping[str, int] = field(default_factory=dict)
    public_price_changes: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    public_trade_prints: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    tick_size_changes: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    best_bid_asks: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    request_records: tuple[Mapping[str, Any], ...] = ()
    errors: Mapping[str, str] = field(default_factory=dict)
    public_error: str | None = None
    fallback_reason: str | None = None


FillTimeBookReader = Callable[
    [BinaryMarket, datetime],
    FillTimeBookEvidence | tuple[OrderBookSide | None, OrderBookSide | None],
]


class PaperPortfolioLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperPortfolioParams:
    starting_capital_usd: float
    trade_ceiling_usd: float
    slippage_buffer_bps: float
    taker_fee_bps: float
    tax_bps: float
    merge_cost_usd: float
    min_net_profit_usd: float = 0.0
    min_net_return_bps: float = 0.0
    max_book_age_seconds: float = config.DEFAULT_MAX_BOOK_AGE_SECONDS
    simulation: config.PaperExecutionSimulationConfig = field(default_factory=config.PaperExecutionSimulationConfig)

    @property
    def slippage_buffer_rate(self) -> float:
        return self.slippage_buffer_bps / 10_000.0

    @property
    def taker_fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0

    @property
    def tax_rate(self) -> float:
        return self.tax_bps / 10_000.0

    @property
    def linear_cost_rate(self) -> float:
        return 1.0 + self.slippage_buffer_rate + self.taker_fee_rate + self.tax_rate

    @classmethod
    def from_config(cls, scan_config: config.ScanConfig | None = None) -> "PaperPortfolioParams":
        loaded = scan_config or config.load_scan_config()
        return cls(
            starting_capital_usd=loaded.starting_capital_usd,
            trade_ceiling_usd=loaded.trade_ceiling_usd,
            slippage_buffer_bps=loaded.slippage_buffer_bps,
            taker_fee_bps=loaded.taker_fee_bps,
            tax_bps=loaded.tax_bps,
            merge_cost_usd=loaded.merge_cost_usd,
            min_net_profit_usd=loaded.min_net_profit_usd,
            min_net_return_bps=loaded.min_net_return_bps,
            max_book_age_seconds=loaded.max_book_age_seconds,
            simulation=loaded.paper_simulation,
        )


@dataclass(frozen=True)
class PaperPortfolioDecision:
    action: str
    reason: str | None = None
    execution: dict[str, Any] | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(cls, reason: str, **details: Any) -> "PaperPortfolioDecision":
        return cls(action="SKIP", reason=reason, details=details)

    @classmethod
    def execute(cls, execution: dict[str, Any]) -> "PaperPortfolioDecision":
        return cls(action="EXECUTE", execution=execution)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _as_float(value: Any, default: float = 0.0) -> float:
    if value in (None, "") or isinstance(value, bool):
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if parsed != parsed or parsed in (float("inf"), float("-inf")):
        return default
    return parsed


def _stale_seconds(book: OrderBookSide, as_of: datetime) -> float | None:
    if book.updated_at is None:
        return None
    return (_ensure_aware(as_of) - _ensure_aware(book.updated_at)).total_seconds()


def _rounded_tranches(tranches: tuple[dict[str, float], ...]) -> list[dict[str, float]]:
    return [
        {
            "quantity": round(float(tranche["quantity"]), 12),
            "yes_price": round(float(tranche["yes_price"]), 12),
            "no_price": round(float(tranche["no_price"]), 12),
            "unit_gross_cost": round(float(tranche["unit_gross_cost"]), 12),
        }
        for tranche in tranches
    ]


def book_pair_fingerprint(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    tranches: tuple[dict[str, float], ...] = (),
) -> str:
    payload = {
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "tranches": _rounded_tranches(tranches),
    }
    source_revisions = {
        side: revision
        for side, revision in (
            ("yes", yes_asks.source_revision),
            ("no", no_asks.source_revision),
        )
        if revision not in (None, "")
    }
    if source_revisions:
        payload["source_revisions"] = source_revisions
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cost_breakdown(
    gross_cost: float,
    params: PaperPortfolioParams,
    *,
    merge_cost_usd: float | None = None,
    slippage_bps: float | None = None,
) -> dict[str, float]:
    effective_slippage_bps = params.slippage_buffer_bps if slippage_bps is None else max(0.0, slippage_bps)
    return {
        "fees_usd": gross_cost * params.taker_fee_rate,
        "slippage_usd": gross_cost * (effective_slippage_bps / 10_000.0),
        "tax_usd": gross_cost * params.tax_rate,
        "merge_usd": params.merge_cost_usd if merge_cost_usd is None else merge_cost_usd,
    }


def _inventory_rows(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = state.get("inventory")
    if not isinstance(rows, dict):
        return {}
    normalized: dict[str, dict[str, Any]] = {}
    for token_id, row in rows.items():
        if not isinstance(row, Mapping):
            continue
        quantity = _as_float(row.get("quantity"))
        if quantity <= EPSILON:
            continue
        normalized[str(token_id)] = {
            **dict(row),
            "token_id": str(row.get("token_id") or token_id),
            "market_id": str(row.get("market_id") or ""),
            "condition_id": row.get("condition_id"),
            "outcome": str(row.get("outcome") or "").upper(),
            "quantity": quantity,
            "cost_basis_usd": _as_float(row.get("cost_basis_usd")),
            "last_valuation_price": _as_float(row.get("last_valuation_price")),
            "last_valuation_usd": _as_float(row.get("last_valuation_usd")),
            "last_valuation_source": row.get("last_valuation_source"),
            "last_valued_at_utc": row.get("last_valued_at_utc"),
            "pending_settlement": bool(row.get("pending_settlement")),
        }
    return normalized


def _inventory_equity_value(state: Mapping[str, Any]) -> float:
    inventory = _inventory_rows(state)
    by_market: dict[str, dict[str, dict[str, Any]]] = {}
    for row in inventory.values():
        market_id = str(row.get("market_id") or "")
        outcome = str(row.get("outcome") or "").upper()
        if outcome not in {"YES", "NO"}:
            continue
        by_market.setdefault(market_id, {})[outcome] = row

    total = 0.0
    for grouped in by_market.values():
        yes_row = grouped.get("YES")
        no_row = grouped.get("NO")
        yes_quantity = _as_float(yes_row.get("quantity")) if yes_row is not None else 0.0
        no_quantity = _as_float(no_row.get("quantity")) if no_row is not None else 0.0
        paired = min(yes_quantity, no_quantity)
        if paired > EPSILON:
            total += paired
        if yes_row is not None and yes_quantity - paired > EPSILON:
            total += (yes_quantity - paired) * _as_float(yes_row.get("last_valuation_price"))
        if no_row is not None and no_quantity - paired > EPSILON:
            total += (no_quantity - paired) * _as_float(no_row.get("last_valuation_price"))
    return total


def _open_inventory_market_ids(state: Mapping[str, Any]) -> set[str]:
    return {
        str(row.get("market_id") or "")
        for row in _inventory_rows(state).values()
        if str(row.get("market_id") or "")
    }


def initial_portfolio_state(
    params: PaperPortfolioParams,
    *,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    now = utc_iso(as_of)
    return {
        "schema_version": PORTFOLIO_SCHEMA_VERSION,
        "mode": "paper_portfolio_instance",
        "starting_capital_usd": params.starting_capital_usd,
        "cash": params.starting_capital_usd,
        "realized_pnl": 0.0,
        "total_equity": params.starting_capital_usd,
        "costs": {
            "fees_usd": 0.0,
            "slippage_usd": 0.0,
            "tax_usd": 0.0,
            "merge_usd": 0.0,
        },
        "executions": [],
        "inventory": {},
        "settlements": [],
        "book_fingerprints": {},
        "metadata": {
            "created_at_utc": now,
            "updated_at_utc": now,
            "paper_only": True,
            "execution_scope": "binary_complete_set",
        },
    }


def _normalized_state(data: Mapping[str, Any], params: PaperPortfolioParams) -> dict[str, Any]:
    state = dict(data)
    state.setdefault("schema_version", PORTFOLIO_SCHEMA_VERSION)
    state.setdefault("mode", "paper_portfolio_instance")
    state.setdefault("starting_capital_usd", params.starting_capital_usd)
    state.setdefault("cash", state["starting_capital_usd"])
    state.setdefault("realized_pnl", _as_float(state.get("cash")) - _as_float(state.get("starting_capital_usd")))
    state.setdefault("executions", [])
    state.setdefault("inventory", {})
    state.setdefault("settlements", [])
    state.setdefault("book_fingerprints", {})
    state.setdefault("metadata", {})
    costs = state.get("costs") if isinstance(state.get("costs"), Mapping) else {}
    state["costs"] = {
        "fees_usd": _as_float(costs.get("fees_usd") if isinstance(costs, Mapping) else None),
        "slippage_usd": _as_float(costs.get("slippage_usd") if isinstance(costs, Mapping) else None),
        "tax_usd": _as_float(costs.get("tax_usd") if isinstance(costs, Mapping) else None),
        "merge_usd": _as_float(costs.get("merge_usd") if isinstance(costs, Mapping) else None),
    }
    state["cash"] = _as_float(state.get("cash"), _as_float(state.get("starting_capital_usd")))
    state["starting_capital_usd"] = _as_float(state.get("starting_capital_usd"), params.starting_capital_usd)
    state["realized_pnl"] = _as_float(state.get("realized_pnl"))
    state["inventory"] = _inventory_rows(state)
    state["settlements"] = [
        dict(row)
        for row in state.get("settlements", [])
        if isinstance(row, Mapping)
    ]
    state["total_equity"] = state["cash"] + _inventory_equity_value(state)
    return state


def _simulation_budget(cash: float, params: PaperPortfolioParams) -> float:
    return min(max(0.0, cash), params.trade_ceiling_usd)


def _simulate_paired_tranches(
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    cash: float,
    params: PaperPortfolioParams,
    max_quantity: float | None = None,
) -> tuple[float, float, float, tuple[dict[str, float], ...], str]:
    spend_limit = _simulation_budget(cash, params)
    if spend_limit <= EPSILON:
        return 0.0, 0.0, 0.0, (), "cash_limit"
    if spend_limit <= params.merge_cost_usd + EPSILON:
        return 0.0, 0.0, 0.0, (), "cash_limit"

    yes_index = 0
    no_index = 0
    yes_remaining = yes_asks.levels[0].size if yes_asks.levels else 0.0
    no_remaining = no_asks.levels[0].size if no_asks.levels else 0.0
    quantity = 0.0
    yes_cost = 0.0
    no_cost = 0.0
    tranches: list[dict[str, float]] = []
    stop_reason = "depth_exhausted"

    while yes_index < len(yes_asks.levels) and no_index < len(no_asks.levels):
        yes_price = yes_asks.levels[yes_index].price
        no_price = no_asks.levels[no_index].price
        unit_gross_cost = yes_price + no_price
        unit_capital_used = unit_gross_cost * params.linear_cost_rate
        if unit_gross_cost <= EPSILON or unit_capital_used <= EPSILON:
            stop_reason = "invalid_price"
            break
        if 1.0 - unit_capital_used <= EPSILON:
            stop_reason = "edge_disappeared"
            break

        gross_cost = yes_cost + no_cost
        linear_cost_so_far = gross_cost * params.linear_cost_rate
        remaining_budget = spend_limit - params.merge_cost_usd - linear_cost_so_far
        if remaining_budget <= EPSILON:
            stop_reason = "cash_or_ceiling_limit"
            break

        available_equal_depth = min(yes_remaining, no_remaining)
        remaining_quantity = None if max_quantity is None else max(0.0, max_quantity - quantity)
        if remaining_quantity is not None and remaining_quantity <= EPSILON:
            stop_reason = "target_quantity_limit"
            break
        step = min(available_equal_depth, remaining_budget / unit_capital_used)
        if remaining_quantity is not None:
            step = min(step, remaining_quantity)
        if step <= EPSILON:
            stop_reason = "cash_or_ceiling_limit"
            break

        budget_limited = step + EPSILON < available_equal_depth
        quantity_limited = remaining_quantity is not None and step + EPSILON >= remaining_quantity
        yes_cost += step * yes_price
        no_cost += step * no_price
        quantity += step
        tranches.append(
            {
                "quantity": step,
                "yes_price": yes_price,
                "no_price": no_price,
                "unit_gross_cost": unit_gross_cost,
            }
        )

        if quantity_limited:
            stop_reason = "target_quantity_limit"
            break
        if budget_limited:
            stop_reason = "cash_or_ceiling_limit"
            break

        yes_remaining -= step
        no_remaining -= step
        if yes_remaining <= EPSILON:
            yes_index += 1
            if yes_index < len(yes_asks.levels):
                yes_remaining = yes_asks.levels[yes_index].size
        if no_remaining <= EPSILON:
            no_index += 1
            if no_index < len(no_asks.levels):
                no_remaining = no_asks.levels[no_index].size

    return quantity, yes_cost, no_cost, tuple(tranches), stop_reason


def _known_fingerprint(state: Mapping[str, Any], market_id: str) -> str | None:
    fingerprints = state.get("book_fingerprints")
    if not isinstance(fingerprints, Mapping):
        return None
    row = fingerprints.get(market_id)
    if isinstance(row, Mapping):
        value = row.get("fingerprint")
        return str(value) if value not in (None, "") else None
    return str(row) if row not in (None, "") else None


def _stable_unit_interval(*parts: Any) -> float:
    payload = "|".join(str(part) for part in parts)
    digest = hashlib.sha256(payload.encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big") / float(2**64 - 1)


def _stage_random(
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    book_fingerprint: str,
    stage: str,
    leg: str = "",
) -> float:
    return _stable_unit_interval(simulation.seed, market.market_id, book_fingerprint, stage, leg)


def _copy_book_with_levels(
    book: OrderBookSide,
    levels: list[BookLevel],
    *,
    source_suffix: str,
) -> OrderBookSide:
    return OrderBookSide(
        token_id=book.token_id,
        side=book.side,
        levels=tuple(level for level in levels if level.size > EPSILON),
        source=f"{book.source}_{source_suffix}",
        updated_at=book.updated_at,
        source_revision=book.source_revision,
        source_hash=book.source_hash,
    )


def _scale_book_depth(book: OrderBookSide, ratio: float) -> OrderBookSide:
    scaled = max(0.0, min(1.0, ratio))
    if scaled >= 1.0 - EPSILON:
        return book
    return _copy_book_with_levels(
        book,
        [BookLevel(price=level.price, size=level.size * scaled) for level in book.levels],
        source_suffix="queue",
    )


def _adverse_adjust_book(book: OrderBookSide, *, removal_ratio: float, price_move_bps: float) -> OrderBookSide:
    depth_ratio = max(0.0, 1.0 - max(0.0, min(1.0, removal_ratio)))
    price_factor = 1.0 + max(0.0, price_move_bps) / 10_000.0
    levels = [
        BookLevel(price=min(0.999999, level.price * price_factor), size=level.size * depth_ratio)
        for level in book.levels
    ]
    return _copy_book_with_levels(book, levels, source_suffix="adverse")


def _fill_cost(book: OrderBookSide, quantity: float) -> float:
    cost = book.cost_to_fill(quantity)
    if cost is None:
        raise ValueError(f"insufficient {book.side} depth for {book.token_id}")
    return cost


def _simulation_failure_decision(
    reason: str,
    *,
    market: BinaryMarket,
    simulation: dict[str, Any],
    book_fingerprint: str,
    **details: Any,
) -> PaperPortfolioDecision:
    simulation = dict(simulation)
    simulation.setdefault("failure_stage", reason)
    simulation.setdefault("failure_reason", reason)
    return PaperPortfolioDecision.skip(
        reason,
        market_id=market.market_id,
        book_fingerprint=book_fingerprint,
        simulation=simulation,
        simulation_failure=True,
        **details,
    )


def _simulation_latency_fields(
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    book_fingerprint: str,
    signal_time: datetime,
) -> tuple[datetime, dict[str, Any]]:
    return _simulation_latency_fields_with_requests(
        simulation,
        market,
        book_fingerprint,
        signal_time,
        request_records=(),
    )


def _request_latency_samples(request_records: Sequence[Mapping[str, Any]], *, limit: int) -> list[float]:
    samples = [
        max(0.0, _as_float(record.get("latency_seconds")) * 1000.0)
        for record in request_records[-max(1, int(limit)) :]
        if isinstance(record, Mapping)
    ]
    return [sample for sample in samples if sample >= 0.0]


def _percentile(values: Sequence[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((percentile / 100.0) * (len(ordered) - 1)))))
    return ordered[index]


def _simulation_latency_seed_parts(
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    book_fingerprint: str,
    stage: str,
) -> tuple[Any, ...]:
    scope = simulation.latency_jitter_seed_scope
    if scope == "global":
        return (simulation.seed, stage)
    if scope == "market":
        return (simulation.seed, market.market_id, stage)
    if scope == "market_stage":
        return (simulation.seed, market.market_id, book_fingerprint, stage)
    return (simulation.seed, market.market_id, book_fingerprint, stage, market.condition_id)


def _simulation_latency_fields_with_requests(
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    book_fingerprint: str,
    signal_time: datetime,
    *,
    request_records: Sequence[Mapping[str, Any]],
) -> tuple[datetime, dict[str, Any]]:
    latency_mode = simulation.latency_mode
    telemetry_samples = _request_latency_samples(request_records, limit=simulation.telemetry_latency_window)
    telemetry_summary = {
        "sample_count": len(telemetry_samples),
        "p50_latency_ms": _percentile(telemetry_samples, 50.0),
        "p95_latency_ms": _percentile(telemetry_samples, 95.0),
        "samples_ms": telemetry_samples,
    }
    base_latency_ms = max(0.0, simulation.latency_ms)
    if latency_mode == "telemetry" and telemetry_samples:
        base_latency_ms = max(base_latency_ms, telemetry_summary["p95_latency_ms"])
    jitter = 0.0
    if simulation.latency_jitter_ms > 0.0:
        jitter_draw = _stable_unit_interval(
            *_simulation_latency_seed_parts(simulation, market, book_fingerprint, "latency_jitter")
        )
        jitter = (jitter_draw - 0.5) * 2.0 * simulation.latency_jitter_ms
    submit_latency_ms = max(0.0, base_latency_ms + jitter)
    signing_latency_ms = max(0.0, simulation.signing_latency_ms)
    settlement_latency_ms = max(0.0, simulation.settlement_latency_ms)
    fill_latency_ms = submit_latency_ms + signing_latency_ms
    fill_time = signal_time + timedelta(milliseconds=fill_latency_ms)
    settlement_time = fill_time + timedelta(milliseconds=settlement_latency_ms)
    timeout_ms = max(0.0, simulation.local_timeout_ms)
    return fill_time, {
        "seed": simulation.seed,
        "enabled": simulation.enabled,
        "latency_mode": latency_mode,
        "signal_timestamp_utc": utc_iso(signal_time),
        "base_latency_ms": base_latency_ms,
        "submit_latency_ms": submit_latency_ms,
        "latency_jitter_ms": jitter,
        "latency_jitter_seed_scope": simulation.latency_jitter_seed_scope,
        "signing_latency_ms": signing_latency_ms,
        "fill_latency_ms": fill_latency_ms,
        "settlement_latency_ms": settlement_latency_ms,
        "fill_timestamp_utc": utc_iso(fill_time),
        "settlement_timestamp_utc": utc_iso(settlement_time),
        "local_timeout_ms": timeout_ms,
        "simulated_submit_timestamp_utc": utc_iso(signal_time + timedelta(milliseconds=submit_latency_ms)),
        "simulated_timeout_timestamp_utc": (
            utc_iso(signal_time + timedelta(milliseconds=timeout_ms)) if timeout_ms > 0.0 else None
        ),
        "telemetry": telemetry_summary,
    }


def _book_timestamps(yes_asks: OrderBookSide, no_asks: OrderBookSide) -> dict[str, str | None]:
    return {
        "yes_book": utc_iso(yes_asks.updated_at) if yes_asks.updated_at else None,
        "no_book": utc_iso(no_asks.updated_at) if no_asks.updated_at else None,
    }


def _book_audit(book: OrderBookSide | None) -> dict[str, Any] | None:
    if book is None:
        return None
    return {
        "token_id": book.token_id,
        "side": book.side,
        "source": book.source,
        "updated_at_utc": utc_iso(book.updated_at) if book.updated_at else None,
        "source_revision": book.source_revision,
        "source_hash": book.source_hash,
        "best_price": book.best_price,
        "available_size": book.available_size,
        "level_count": len(book.levels),
    }


def _book_fingerprint_input(book: OrderBookSide) -> list[dict[str, float]]:
    return [{"price": level.price, "size": level.size} for level in book.levels]


def _signal_fill_book_comparison(
    signal_yes: OrderBookSide,
    signal_no: OrderBookSide,
    fill_yes: OrderBookSide | None,
    fill_no: OrderBookSide | None,
) -> dict[str, Any]:
    return {
        "yes": {
            "signal": _book_audit(signal_yes),
            "fill": _book_audit(fill_yes),
            "levels_changed": _book_fingerprint_input(signal_yes)
            != (_book_fingerprint_input(fill_yes) if fill_yes is not None else []),
        },
        "no": {
            "signal": _book_audit(signal_no),
            "fill": _book_audit(fill_no),
            "levels_changed": _book_fingerprint_input(signal_no)
            != (_book_fingerprint_input(fill_no) if fill_no is not None else []),
        },
    }


def _evidence_rows_for_token(
    rows_by_token: Mapping[str, tuple[Mapping[str, Any], ...]] | Mapping[str, Any],
    token_id: str,
) -> list[Mapping[str, Any]]:
    rows = rows_by_token.get(token_id) if isinstance(rows_by_token, Mapping) else None
    if not isinstance(rows, (list, tuple)):
        return []
    return [row for row in rows if isinstance(row, Mapping)]


def _queue_ahead_at_prices(book: OrderBookSide, prices: set[float]) -> float:
    return sum(level.size for level in book.levels if level.price in prices)


def _intended_prices(tranches: Mapping[str, Any] | None, side: str) -> set[float]:
    if not isinstance(tranches, Mapping):
        return set()
    prices: set[float] = set()
    raw_tranches = tranches.get("tranches")
    if isinstance(raw_tranches, list):
        key = f"{side}_price"
        for tranche in raw_tranches:
            if isinstance(tranche, Mapping):
                price = _as_float(tranche.get(key))
                if price > EPSILON:
                    prices.add(price)
    return prices


def _observed_queue_decrease(
    evidence: FillTimeBookEvidence,
    *,
    token_id: str,
    prices: set[float],
    side_name: str,
) -> tuple[float, float, list[dict[str, Any]], list[dict[str, Any]]]:
    price_changes: list[dict[str, Any]] = []
    trade_prints: list[dict[str, Any]] = []
    price_change_size = 0.0
    trade_print_size = 0.0
    for row in _evidence_rows_for_token(evidence.public_price_changes, token_id):
        side = str(row.get("side") or "").lower()
        price = _as_float(row.get("price"))
        delta_size = _as_float(row.get("delta_size"))
        if side != "ask" or price not in prices:
            continue
        price_changes.append(dict(row))
        if delta_size < 0.0:
            price_change_size += abs(delta_size)
    for row in _evidence_rows_for_token(evidence.public_trade_prints, token_id):
        price = _as_float(row.get("price"))
        size = _as_float(row.get("size"))
        if price not in prices or size <= EPSILON:
            continue
        trade_prints.append(dict(row))
        trade_print_size += size
    _ = side_name
    return price_change_size, trade_print_size, price_changes, trade_prints


def _normalize_fill_time_evidence(
    raw: FillTimeBookEvidence | tuple[OrderBookSide | None, OrderBookSide | None],
) -> FillTimeBookEvidence:
    if isinstance(raw, FillTimeBookEvidence):
        return raw
    yes_book, no_book = raw
    return FillTimeBookEvidence(
        source="legacy_fill_time_reader",
        yes_book=yes_book,
        no_book=no_book,
    )


def _fill_evidence_audit(
    evidence: FillTimeBookEvidence,
    *,
    market: BinaryMarket,
) -> dict[str, Any]:
    return {
        "source": evidence.source,
        "observed_at_utc": utc_iso(evidence.observed_at) if evidence.observed_at else None,
        "snapshot_ready": dict(evidence.snapshot_ready),
        "snapshot_generation": dict(evidence.snapshot_generation),
        "books": {
            "yes": _book_audit(evidence.yes_book),
            "no": _book_audit(evidence.no_book),
        },
        "public_price_changes": {
            "yes": list(_evidence_rows_for_token(evidence.public_price_changes, market.yes_token_id)),
            "no": list(_evidence_rows_for_token(evidence.public_price_changes, market.no_token_id)),
        },
        "public_trade_prints": {
            "yes": list(_evidence_rows_for_token(evidence.public_trade_prints, market.yes_token_id)),
            "no": list(_evidence_rows_for_token(evidence.public_trade_prints, market.no_token_id)),
        },
        "tick_size_changes": {
            "yes": list(_evidence_rows_for_token(evidence.tick_size_changes, market.yes_token_id)),
            "no": list(_evidence_rows_for_token(evidence.tick_size_changes, market.no_token_id)),
        },
        "best_bid_asks": {
            "yes": list(_evidence_rows_for_token(evidence.best_bid_asks, market.yes_token_id)),
            "no": list(_evidence_rows_for_token(evidence.best_bid_asks, market.no_token_id)),
        },
        "request_records": [dict(row) for row in evidence.request_records],
        "errors": dict(evidence.errors),
        "public_error": evidence.public_error,
        "fallback_reason": evidence.fallback_reason,
    }


def _capped_signal_tranches(
    signal_execution: Mapping[str, Any],
    *,
    signal_yes: OrderBookSide,
    signal_no: OrderBookSide,
    target_quantity: float,
) -> tuple[dict[str, float], ...]:
    details = signal_execution.get("details") if isinstance(signal_execution, Mapping) else None
    raw_tranches = details.get("tranches") if isinstance(details, Mapping) else None
    remaining = max(0.0, target_quantity)
    tranches: list[dict[str, float]] = []
    if isinstance(raw_tranches, list):
        for tranche in raw_tranches:
            if not isinstance(tranche, Mapping):
                continue
            tranche_quantity = min(remaining, max(0.0, _as_float(tranche.get("quantity"))))
            yes_price = _as_float(tranche.get("yes_price"))
            no_price = _as_float(tranche.get("no_price"))
            if tranche_quantity <= EPSILON or yes_price <= EPSILON or no_price <= EPSILON:
                continue
            tranches.append(
                {
                    "quantity": tranche_quantity,
                    "yes_price": yes_price,
                    "no_price": no_price,
                    "unit_gross_cost": _as_float(tranche.get("unit_gross_cost"), yes_price + no_price),
                }
            )
            remaining -= tranche_quantity
            if remaining <= EPSILON:
                break
    if tranches:
        return tuple(tranches)
    if target_quantity <= EPSILON or signal_yes.best_price is None or signal_no.best_price is None:
        return ()
    return (
        {
            "quantity": target_quantity,
            "yes_price": signal_yes.best_price,
            "no_price": signal_no.best_price,
            "unit_gross_cost": signal_yes.best_price + signal_no.best_price,
        },
    )


def _supported_quantity_for_tranches(
    book: OrderBookSide,
    tranches: Sequence[Mapping[str, Any]],
    *,
    side: str,
) -> float:
    if not tranches:
        return 0.0
    levels: list[dict[str, float]] = [
        {"price": level.price, "size": level.size}
        for level in book.levels
    ]
    level_index = 0
    supported = 0.0
    price_key = f"{side}_price"
    for tranche in tranches:
        remaining = _as_float(tranche.get("quantity"))
        price_limit = _as_float(tranche.get(price_key))
        if remaining <= EPSILON or price_limit <= EPSILON:
            continue
        while remaining > EPSILON and level_index < len(levels):
            level = levels[level_index]
            if level["price"] > price_limit + EPSILON:
                return supported
            take = min(remaining, max(0.0, level["size"]))
            if take > EPSILON:
                supported += take
                remaining -= take
                level["size"] -= take
            if level["size"] <= EPSILON:
                level_index += 1
            elif take <= EPSILON:
                break
        if remaining > EPSILON:
            return supported
    return supported


def _book_with_support(
    book: OrderBookSide,
    additions_by_price: Mapping[float, float],
    *,
    source_suffix: str,
) -> OrderBookSide:
    if not additions_by_price:
        return book
    merged = {level.price: level.size for level in book.levels}
    for price, size in additions_by_price.items():
        if price <= EPSILON or size <= EPSILON:
            continue
        merged[price] = merged.get(price, 0.0) + size
    levels = [BookLevel(price=price, size=size) for price, size in merged.items() if size > EPSILON]
    levels.sort(key=lambda level: level.price, reverse=(book.side == "bid"))
    return _copy_book_with_levels(book, levels, source_suffix=source_suffix)


def _fill_eligibility_for_leg(
    *,
    simulation: config.PaperExecutionSimulationConfig,
    token_id: str,
    side_name: str,
    signal_book: OrderBookSide,
    fill_book: OrderBookSide,
    signal_execution: Mapping[str, Any],
    evidence: FillTimeBookEvidence,
    tranches: Sequence[Mapping[str, Any]],
    target_quantity: float,
) -> tuple[OrderBookSide, float, dict[str, Any]]:
    intended_prices = _intended_prices(signal_execution.get("details"), side_name)
    if not intended_prices and signal_book.best_price is not None:
        intended_prices.add(signal_book.best_price)
    queue_ahead = _queue_ahead_at_prices(signal_book, intended_prices)
    price_change_size, trade_print_size, price_deltas, trade_prints = _observed_queue_decrease(
        evidence,
        token_id=token_id,
        prices=intended_prices,
        side_name=side_name,
    )
    depth_supported_quantity = min(target_quantity, _supported_quantity_for_tranches(fill_book, tranches, side=side_name))
    additions_by_price: dict[float, float] = {}
    for row in price_deltas:
        price = _as_float(row.get("price"))
        delta_size = abs(min(0.0, _as_float(row.get("delta_size"))))
        if price > EPSILON and delta_size > EPSILON:
            additions_by_price[price] = additions_by_price.get(price, 0.0) + delta_size
    trade_print_support_applied = False
    if (
        simulation.allow_trade_print_fill_support
        and trade_print_size > EPSILON
        and (depth_supported_quantity > EPSILON or price_change_size > EPSILON)
    ):
        trade_print_support_applied = True
        for row in trade_prints:
            price = _as_float(row.get("price"))
            size = _as_float(row.get("size"))
            if price > EPSILON and size > EPSILON:
                additions_by_price[price] = additions_by_price.get(price, 0.0) + size
    augmented_book = _book_with_support(fill_book, additions_by_price, source_suffix="public_support")
    supported_quantity = min(target_quantity, _supported_quantity_for_tranches(augmented_book, tranches, side=side_name))
    trade_only = depth_supported_quantity <= EPSILON and price_change_size <= EPSILON and trade_print_size > EPSILON
    source = "none"
    if supported_quantity >= target_quantity - EPSILON and depth_supported_quantity >= target_quantity - EPSILON:
        source = "strict_public_depth"
    elif supported_quantity > depth_supported_quantity + EPSILON and price_change_size > EPSILON:
        source = "public_depth_plus_price_change"
    elif supported_quantity > depth_supported_quantity + EPSILON and trade_print_support_applied:
        source = "public_depth_plus_trade_print"
    elif supported_quantity > EPSILON and depth_supported_quantity > EPSILON:
        source = "partial_public_depth"
    elif supported_quantity > EPSILON and price_change_size > EPSILON:
        source = "price_change_support_only"
    elif trade_only:
        source = "trade_print_only"
    return augmented_book, supported_quantity, {
        "source": source,
        "target_quantity": target_quantity,
        "supported_quantity": supported_quantity,
        "depth_supported_quantity": depth_supported_quantity,
        "price_change_supported_quantity": price_change_size,
        "trade_print_supported_quantity": trade_print_size if trade_print_support_applied else 0.0,
        "trade_print_observed_quantity": trade_print_size,
        "trade_print_support_applied": trade_print_support_applied,
        "trade_print_only": trade_only,
        "intended_prices": sorted(intended_prices),
        "queue_ahead_at_signal": queue_ahead,
        "price_delta_evidence": price_deltas,
        "trade_print_evidence": trade_prints,
        "fill_book_available_depth": fill_book.available_size,
        "augmented_fill_book_available_depth": augmented_book.available_size,
    }


def _latest_best_bid_ask(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    filtered = [row for row in rows if isinstance(row, Mapping)]
    return filtered[-1] if filtered else None


def _calibrated_slippage(
    *,
    params: PaperPortfolioParams,
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    quantity: float,
    yes_book: OrderBookSide,
    no_book: OrderBookSide,
    signal_execution: Mapping[str, Any],
    evidence: FillTimeBookEvidence | None,
    as_of: datetime,
) -> tuple[float, dict[str, Any]]:
    base_bps = max(0.0, params.slippage_buffer_bps)
    metadata = {
        "mode": simulation.slippage_mode,
        "base_bps": base_bps,
        "combine_mode": simulation.slippage_combine_mode,
        "calibrated_bps": 0.0,
        "final_bps": base_bps,
        "cap_bps": simulation.slippage_max_bps,
        "capped": False,
        "legs": {},
    }
    if simulation.slippage_mode != "fixed_plus_calibrated" or quantity <= EPSILON or evidence is None:
        return base_bps, metadata

    tranches = _capped_signal_tranches(
        signal_execution,
        signal_yes=yes_book,
        signal_no=no_book,
        target_quantity=quantity,
    )
    legs: dict[str, Any] = {}
    calibrated_values: list[float] = []
    for side_name, token_id, book in (
        ("yes", market.yes_token_id, yes_book),
        ("no", market.no_token_id, no_book),
    ):
        bba = _latest_best_bid_ask(
            _evidence_rows_for_token(
                evidence.best_bid_asks,
                token_id,
            )[-simulation.slippage_lookback_events :]
        )
        best_bid = _as_float(bba.get("best_bid")) if isinstance(bba, Mapping) else 0.0
        best_ask = _as_float(bba.get("best_ask")) if isinstance(bba, Mapping) else _as_float(book.best_price)
        midpoint = (best_bid + best_ask) / 2.0 if best_bid > EPSILON and best_ask > EPSILON else max(best_bid, best_ask)
        spread_bps = (
            ((best_ask - best_bid) / midpoint) * 10_000.0
            if midpoint > EPSILON and best_ask > EPSILON and best_bid > EPSILON and best_ask >= best_bid
            else 0.0
        )
        supported_depth = max(EPSILON, _supported_quantity_for_tranches(book, tranches, side=side_name))
        depth_ratio = quantity / supported_depth
        lookback_prices = [
            _as_float(row.get("price"))
            for row in _evidence_rows_for_token(evidence.public_price_changes, token_id)[
                -simulation.slippage_lookback_events :
            ]
            if _as_float(row.get("price")) > EPSILON
        ]
        lookback_prices.extend(
            _as_float(row.get("price"))
            for row in _evidence_rows_for_token(evidence.public_trade_prints, token_id)[
                -simulation.slippage_lookback_events :
            ]
            if _as_float(row.get("price")) > EPSILON
        )
        intended_prices = _intended_prices(signal_execution.get("details"), side_name)
        intended_price = max(intended_prices) if intended_prices else _as_float(book.best_price)
        observed_move_bps = 0.0
        if intended_price > EPSILON and lookback_prices:
            observed_move_bps = max(0.0, ((max(lookback_prices) - intended_price) / intended_price) * 10_000.0)
        recent_trade_sizes = [
            _as_float(row.get("size"))
            for row in _evidence_rows_for_token(evidence.public_trade_prints, token_id)[
                -simulation.slippage_lookback_events :
            ]
            if _as_float(row.get("size")) > EPSILON
        ]
        avg_trade_size = sum(recent_trade_sizes) / len(recent_trade_sizes) if recent_trade_sizes else 0.0
        trade_pressure = quantity / avg_trade_size if avg_trade_size > EPSILON else 1.0
        stale_age = _stale_seconds(book, as_of) or 0.0
        age_ratio = stale_age / max(params.max_book_age_seconds, 1.0)
        request_records = list(evidence.request_records)[-simulation.slippage_lookback_events :]
        retry_count = sum(max(0, int(_as_float(record.get("retries")))) for record in request_records)
        error_count = sum(1 for record in request_records if record.get("error"))
        latency_seconds = max((_as_float(record.get("latency_seconds")) for record in request_records), default=0.0)
        multiplier = 1.0 + max(0.0, age_ratio * 0.25) + (retry_count * 0.10) + (error_count * 0.15) + (
            latency_seconds * 0.10
        )
        leg_bps = (
            (spread_bps * 0.50)
            + (max(0.0, depth_ratio - 1.0) * 25.0)
            + (observed_move_bps * 0.35)
            + (max(0.0, trade_pressure - 1.0) * 5.0)
        ) * multiplier
        leg_bps = min(max(0.0, leg_bps), max(0.0, simulation.slippage_max_bps))
        legs[side_name] = {
            "spread_bps": spread_bps,
            "depth_ratio": depth_ratio,
            "observed_price_move_bps": observed_move_bps,
            "average_trade_size": avg_trade_size,
            "trade_pressure": trade_pressure,
            "stale_age_seconds": stale_age,
            "retry_count": retry_count,
            "error_count": error_count,
            "latency_seconds": latency_seconds,
            "calibrated_bps": leg_bps,
        }
        calibrated_values.append(leg_bps)
    calibrated_bps = min(max(calibrated_values or [0.0]), max(0.0, simulation.slippage_max_bps))
    final_bps = (
        max(base_bps, calibrated_bps)
        if simulation.slippage_combine_mode == "max"
        else min(max(0.0, simulation.slippage_max_bps), base_bps + calibrated_bps)
    )
    metadata.update(
        {
            "calibrated_bps": calibrated_bps,
            "final_bps": final_bps,
            "capped": calibrated_bps >= simulation.slippage_max_bps - EPSILON,
            "legs": legs,
        }
    )
    return final_bps, metadata


def _finalize_execution_from_books(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    state: Mapping[str, Any],
    params: PaperPortfolioParams,
    as_of: datetime,
    max_quantity: float | None,
    simulation_metadata: dict[str, Any],
    signal_execution: Mapping[str, Any],
    evidence: FillTimeBookEvidence | None = None,
    side_fill_quantities: Mapping[str, float] | None = None,
) -> PaperPortfolioDecision:
    cash = _as_float(state.get("cash"), params.starting_capital_usd)
    quantity, yes_cost, no_cost, tranches, stop_reason = _simulate_paired_tranches(
        yes_asks,
        no_asks,
        cash=cash,
        params=params,
        max_quantity=max_quantity,
    )
    fingerprint = book_pair_fingerprint(market, yes_asks, no_asks, tranches=tranches)
    simulation_metadata = dict(simulation_metadata)
    simulation_metadata.update(
        {
            "fill_book_source": simulation_metadata.get("book_source", "fill_time"),
            "fill_book_fingerprint": fingerprint,
            "fill_source_timestamps": _book_timestamps(yes_asks, no_asks),
        }
    )

    if _known_fingerprint(state, market.market_id) == fingerprint:
        return PaperPortfolioDecision.skip(
            "unchanged_book_snapshot",
            market_id=market.market_id,
            book_fingerprint=fingerprint,
            simulation=simulation_metadata,
        )

    min_quantity = market.effective_min_order_size
    if quantity <= EPSILON:
        return _simulation_failure_decision(
            stop_reason,
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            yes_best_ask=yes_asks.best_price,
            no_best_ask=no_asks.best_price,
        )
    if quantity + EPSILON < min_quantity:
        return _simulation_failure_decision(
            "simulation_insufficient_depth",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            available_equal_depth=quantity,
            min_quantity=min_quantity,
        )

    gross_cost = yes_cost + no_cost
    signal_quantity = _as_float(signal_execution.get("quantity"))
    signal_gross_cost = _as_float(signal_execution.get("gross_cost"))
    if params.simulation.max_fill_price_move_bps > 0.0 and signal_quantity > EPSILON and signal_gross_cost > EPSILON:
        signal_unit_cost = signal_gross_cost / signal_quantity
        fill_unit_cost = gross_cost / quantity
        move_bps = ((fill_unit_cost - signal_unit_cost) / signal_unit_cost) * 10_000.0
        simulation_metadata["price_move_bps"] = move_bps
        if move_bps > params.simulation.max_fill_price_move_bps + EPSILON:
            return _simulation_failure_decision(
                "simulation_fill_price_moved",
                market=market,
                simulation=simulation_metadata,
                book_fingerprint=fingerprint,
                signal_unit_cost=signal_unit_cost,
                fill_unit_cost=fill_unit_cost,
                max_fill_price_move_bps=params.simulation.max_fill_price_move_bps,
            )

    effective_slippage_bps, slippage_metadata = _calibrated_slippage(
        params=params,
        simulation=params.simulation,
        market=market,
        quantity=quantity,
        yes_book=yes_asks,
        no_book=no_asks,
        signal_execution=signal_execution,
        evidence=evidence,
        as_of=as_of,
    )
    simulation_metadata["slippage"] = slippage_metadata
    full_fill_costs = _cost_breakdown(gross_cost, params, slippage_bps=effective_slippage_bps)
    full_fill_capital_used = gross_cost + sum(full_fill_costs.values())
    full_fill_net_profit = quantity - full_fill_capital_used
    full_fill_net_return_bps = (
        (full_fill_net_profit / full_fill_capital_used) * 10_000.0 if full_fill_capital_used > 0 else 0.0
    )
    if (
        full_fill_net_profit <= EPSILON
        or full_fill_net_profit + EPSILON < params.min_net_profit_usd
        or full_fill_net_return_bps + EPSILON < params.min_net_return_bps
    ):
        return _simulation_failure_decision(
            "simulation_not_profitable_at_fill",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            quantity=quantity,
            gross_cost=gross_cost,
            capital_used=full_fill_capital_used,
            net_profit=full_fill_net_profit,
            net_return_bps=full_fill_net_return_bps,
        )

    partial_applied = False
    side_fill_source = "full_public_depth"
    yes_filled_quantity = quantity
    no_filled_quantity = quantity
    if isinstance(side_fill_quantities, Mapping):
        yes_filled_quantity = min(quantity, max(0.0, _as_float(side_fill_quantities.get("yes"), quantity)))
        no_filled_quantity = min(quantity, max(0.0, _as_float(side_fill_quantities.get("no"), quantity)))
        partial_applied = yes_filled_quantity + EPSILON < quantity or no_filled_quantity + EPSILON < quantity
        side_fill_source = "public_queue_evidence"
    elif (
        params.simulation.partial_fill_probability > 0.0
        and _stage_random(params.simulation, market, fingerprint, "partial_fill") <= params.simulation.partial_fill_probability
    ):
        partial_applied = True
        side_fill_source = "deterministic_partial_fallback"
        min_ratio = max(0.0, min(1.0, params.simulation.partial_fill_min_ratio))
        yes_ratio = min_ratio + (1.0 - min_ratio) * _stage_random(
            params.simulation,
            market,
            fingerprint,
            "partial_ratio",
            "yes",
        )
        no_ratio = min_ratio + (1.0 - min_ratio) * _stage_random(
            params.simulation,
            market,
            fingerprint,
            "partial_ratio",
            "no",
        )
        yes_filled_quantity = quantity * yes_ratio
        no_filled_quantity = quantity * no_ratio

    yes_actual_cost = _fill_cost(yes_asks, yes_filled_quantity)
    no_actual_cost = _fill_cost(no_asks, no_filled_quantity)
    gross_cost = yes_actual_cost + no_actual_cost
    matched_quantity = min(yes_filled_quantity, no_filled_quantity)
    merge_cost = params.merge_cost_usd if matched_quantity > EPSILON else 0.0
    costs = _cost_breakdown(
        gross_cost,
        params,
        merge_cost_usd=merge_cost,
        slippage_bps=effective_slippage_bps,
    )
    capital_used = gross_cost + sum(costs.values())
    redeemed_value = matched_quantity
    net_profit = redeemed_value - capital_used
    net_return_bps = (net_profit / capital_used) * 10_000.0 if capital_used > 0 else 0.0

    simulation_metadata["partial_fill"] = {
        "applied": partial_applied,
        "source": side_fill_source,
        "target_quantity": quantity,
        "yes_filled_quantity": yes_filled_quantity,
        "no_filled_quantity": no_filled_quantity,
        "matched_quantity": matched_quantity,
        "unmatched_yes_quantity": max(0.0, yes_filled_quantity - matched_quantity),
        "unmatched_no_quantity": max(0.0, no_filled_quantity - matched_quantity),
    }
    execution_count = len(state.get("executions") or [])
    execution_id = f"paper:{market.market_id}:{execution_count + 1}:{fingerprint[:12]}"
    execution = {
        "execution_id": execution_id,
        "opportunity_id": f"binary:{market.market_id}:{fingerprint[:12]}",
        "kind": "binary_complete_set",
        "mode": "paper_portfolio_instance",
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "event_id": market.event_id,
        "event_title": market.event_title,
        "question": market.question,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "executed_at_utc": simulation_metadata["fill_timestamp_utc"],
        "book_fingerprint": fingerprint,
        "quantity": quantity,
        "quantity_redeemed": matched_quantity,
        "yes_filled_quantity": yes_filled_quantity,
        "no_filled_quantity": no_filled_quantity,
        "unmatched_yes_quantity": max(0.0, yes_filled_quantity - matched_quantity),
        "unmatched_no_quantity": max(0.0, no_filled_quantity - matched_quantity),
        "yes_vwap": yes_actual_cost / yes_filled_quantity if yes_filled_quantity > EPSILON else 0.0,
        "no_vwap": no_actual_cost / no_filled_quantity if no_filled_quantity > EPSILON else 0.0,
        "yes_cost": yes_actual_cost,
        "no_cost": no_actual_cost,
        "gross_cost": gross_cost,
        "estimated_fees": costs["fees_usd"],
        "slippage_buffer": costs["slippage_usd"],
        "tax_cost": costs["tax_usd"],
        "merge_cost": costs["merge_usd"],
        "capital_used": capital_used,
        "redeemed_value": redeemed_value,
        "net_profit": net_profit,
        "net_return_bps": net_return_bps,
        "effective_slippage_bps": effective_slippage_bps,
        "trade_ceiling_usd": params.trade_ceiling_usd,
        "ceiling_used_usd": capital_used,
        "stop_reason": stop_reason,
        "simulation": simulation_metadata,
        "source_timestamps": _book_timestamps(yes_asks, no_asks),
        "details": {
            "yes_best_ask": yes_asks.best_price,
            "no_best_ask": no_asks.best_price,
            "yes_source": yes_asks.source,
            "no_source": no_asks.source,
            "min_order_size": min_quantity,
            "tranches": list(tranches),
            "signal_book_fingerprint": signal_execution.get("book_fingerprint"),
        },
    }
    return PaperPortfolioDecision.execute(execution)


def _evaluate_optimistic_binary_paper_execution(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    state: Mapping[str, Any],
    params: PaperPortfolioParams,
    as_of: datetime | None = None,
) -> PaperPortfolioDecision:
    now = _ensure_aware(as_of or _utc_now())

    if market.neg_risk:
        return PaperPortfolioDecision.skip("neg_risk_not_supported", market_id=market.market_id)
    if not market.active or market.closed:
        return PaperPortfolioDecision.skip("inactive_or_closed", market_id=market.market_id)
    if not market.accepting_orders or not market.enable_order_book:
        return PaperPortfolioDecision.skip("not_accepting_orders", market_id=market.market_id)
    if len({market.yes_token_id, market.no_token_id}) != 2:
        return PaperPortfolioDecision.skip("invalid_token_mapping", market_id=market.market_id)
    if yes_asks.token_id != market.yes_token_id:
        return PaperPortfolioDecision.skip("yes_book_token_mismatch", market_id=market.market_id)
    if no_asks.token_id != market.no_token_id:
        return PaperPortfolioDecision.skip("no_book_token_mismatch", market_id=market.market_id)
    if yes_asks.side != "ask" or no_asks.side != "ask":
        return PaperPortfolioDecision.skip("requires_ask_books", market_id=market.market_id)
    if not yes_asks.levels or not no_asks.levels:
        return PaperPortfolioDecision.skip(
            "missing_two_sided_ask_liquidity",
            market_id=market.market_id,
            yes_levels=len(yes_asks.levels),
            no_levels=len(no_asks.levels),
        )

    for label, book in (("yes", yes_asks), ("no", no_asks)):
        age = _stale_seconds(book, now)
        if age is not None and (age < -EPSILON or age > params.max_book_age_seconds):
            return PaperPortfolioDecision.skip(
                "stale_book",
                market_id=market.market_id,
                side=label,
                age_seconds=age,
                max_age_seconds=params.max_book_age_seconds,
            )

    cash = _as_float(state.get("cash"), params.starting_capital_usd)
    quantity, yes_cost, no_cost, tranches, stop_reason = _simulate_paired_tranches(
        yes_asks,
        no_asks,
        cash=cash,
        params=params,
    )
    fingerprint = book_pair_fingerprint(market, yes_asks, no_asks, tranches=tranches)
    if _known_fingerprint(state, market.market_id) == fingerprint:
        return PaperPortfolioDecision.skip(
            "unchanged_book_snapshot",
            market_id=market.market_id,
            book_fingerprint=fingerprint,
        )

    min_quantity = market.effective_min_order_size
    if quantity <= EPSILON:
        return PaperPortfolioDecision.skip(
            stop_reason,
            market_id=market.market_id,
            yes_best_ask=yes_asks.best_price,
            no_best_ask=no_asks.best_price,
            book_fingerprint=fingerprint,
        )
    if quantity + EPSILON < min_quantity:
        return PaperPortfolioDecision.skip(
            "insufficient_depth",
            market_id=market.market_id,
            available_equal_depth=quantity,
            min_quantity=min_quantity,
            book_fingerprint=fingerprint,
        )

    gross_cost = yes_cost + no_cost
    costs = _cost_breakdown(gross_cost, params)
    capital_used = gross_cost + sum(costs.values())
    redeemed_value = quantity
    net_profit = redeemed_value - capital_used
    net_return_bps = (net_profit / capital_used) * 10_000.0 if capital_used > 0 else 0.0
    if (
        net_profit <= EPSILON
        or net_profit + EPSILON < params.min_net_profit_usd
        or net_return_bps + EPSILON < params.min_net_return_bps
    ):
        return PaperPortfolioDecision.skip(
            "not_profitable",
            market_id=market.market_id,
            quantity=quantity,
            gross_cost=gross_cost,
            capital_used=capital_used,
            net_profit=net_profit,
            net_return_bps=net_return_bps,
            book_fingerprint=fingerprint,
        )

    execution_count = len(state.get("executions") or [])
    execution_id = f"paper:{market.market_id}:{execution_count + 1}:{fingerprint[:12]}"
    execution = {
        "execution_id": execution_id,
        "opportunity_id": f"binary:{market.market_id}:{fingerprint[:12]}",
        "kind": "binary_complete_set",
        "mode": "paper_portfolio_instance",
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "event_id": market.event_id,
        "event_title": market.event_title,
        "question": market.question,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "executed_at_utc": utc_iso(now),
        "book_fingerprint": fingerprint,
        "quantity": quantity,
        "quantity_redeemed": quantity,
        "yes_vwap": yes_cost / quantity,
        "no_vwap": no_cost / quantity,
        "yes_cost": yes_cost,
        "no_cost": no_cost,
        "gross_cost": gross_cost,
        "estimated_fees": costs["fees_usd"],
        "slippage_buffer": costs["slippage_usd"],
        "tax_cost": costs["tax_usd"],
        "merge_cost": costs["merge_usd"],
        "capital_used": capital_used,
        "redeemed_value": redeemed_value,
        "net_profit": net_profit,
        "net_return_bps": net_return_bps,
        "trade_ceiling_usd": params.trade_ceiling_usd,
        "ceiling_used_usd": capital_used,
        "stop_reason": stop_reason,
        "source_timestamps": {
            "yes_book": utc_iso(yes_asks.updated_at) if yes_asks.updated_at else None,
            "no_book": utc_iso(no_asks.updated_at) if no_asks.updated_at else None,
        },
        "details": {
            "yes_best_ask": yes_asks.best_price,
            "no_best_ask": no_asks.best_price,
            "yes_source": yes_asks.source,
            "no_source": no_asks.source,
            "min_order_size": min_quantity,
            "tranches": list(tranches),
        },
    }
    return PaperPortfolioDecision.execute(execution)


def _simulation_failure_triggered(
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    book_fingerprint: str,
    *,
    stage: str,
    probability: float,
) -> bool:
    return probability > 0.0 and _stage_random(simulation, market, book_fingerprint, stage) <= probability


def _apply_simulated_book_friction(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    simulation: config.PaperExecutionSimulationConfig,
    signal_fingerprint: str,
    metadata: dict[str, Any],
) -> tuple[OrderBookSide, OrderBookSide]:
    adjusted_yes = yes_asks
    adjusted_no = no_asks
    adverse_applied = False
    if (
        simulation.adverse_selection_probability > 0.0
        and _stage_random(simulation, market, signal_fingerprint, "adverse_selection")
        <= simulation.adverse_selection_probability
    ):
        adverse_applied = True
        adjusted_yes = _adverse_adjust_book(
            adjusted_yes,
            removal_ratio=simulation.adverse_depth_removal_ratio,
            price_move_bps=simulation.adverse_price_move_bps,
        )
        adjusted_no = _adverse_adjust_book(
            adjusted_no,
            removal_ratio=simulation.adverse_depth_removal_ratio,
            price_move_bps=simulation.adverse_price_move_bps,
        )
    metadata["adverse_selection"] = {
        "applied": adverse_applied,
        "probability": simulation.adverse_selection_probability,
        "depth_removal_ratio": simulation.adverse_depth_removal_ratio if adverse_applied else 0.0,
        "price_move_bps": simulation.adverse_price_move_bps if adverse_applied else 0.0,
    }

    queue_applied = simulation.queue_depth_ratio > 0.0
    queue_fill_draw = _stage_random(simulation, market, signal_fingerprint, "queue_fill")
    metadata["queue"] = {
        "applied": queue_applied,
        "depth_ratio": simulation.queue_depth_ratio if queue_applied else 1.0,
        "fill_probability": simulation.queue_fill_probability,
        "fill_draw": queue_fill_draw,
    }
    if queue_applied:
        adjusted_yes = _scale_book_depth(adjusted_yes, simulation.queue_depth_ratio)
        adjusted_no = _scale_book_depth(adjusted_no, simulation.queue_depth_ratio)
    return adjusted_yes, adjusted_no


def _local_pressure_failure_probability(
    simulation: config.PaperExecutionSimulationConfig,
    evidence: FillTimeBookEvidence | None,
) -> tuple[float, dict[str, Any]]:
    request_records = list(evidence.request_records) if evidence is not None else []
    retry_count = sum(max(0, int(_as_float(record.get("retries")))) for record in request_records)
    error_records = [
        record
        for record in request_records
        if record.get("error") or int(_as_float(record.get("status_code"))) in {429, 500, 502, 503, 504}
    ]
    timeout_records = [record for record in request_records if "Timeout" in str(record.get("error") or "")]
    throttle_probability = 0.0
    source = "no_public_pressure"
    if error_records or retry_count > 0:
        throttle_probability = min(1.0, 0.10 * retry_count + 0.25 * len(error_records) + 0.25 * len(timeout_records))
        source = "public_request_errors"
    elif simulation.throttle_max_submissions_per_second > 0:
        throttle_probability = min(1.0, 1.0 / max(1, simulation.throttle_max_submissions_per_second))
        source = "deterministic_local_pressure_fallback"
    return throttle_probability, {
        "source": source,
        "request_count": len(request_records),
        "retry_count": retry_count,
        "error_count": len(error_records),
        "timeout_count": len(timeout_records),
        "probability": throttle_probability,
        "request_records": [dict(record) for record in request_records],
    }


def _legacy_probability_failure(
    simulation: config.PaperExecutionSimulationConfig,
    market: BinaryMarket,
    signal_fingerprint: str,
) -> str | None:
    for stage, probability in (
        ("submit_failure", simulation.submit_failure_probability),
        ("accept_failure", simulation.accept_failure_probability),
        ("fill_failure", simulation.fill_failure_probability),
    ):
        if _simulation_failure_triggered(
            simulation,
            market,
            signal_fingerprint,
            stage=stage,
            probability=probability,
        ):
            if stage == "fill_failure" and _simulation_failure_triggered(
                simulation,
                market,
                signal_fingerprint,
                stage="cancel_failure",
                probability=simulation.cancel_failure_probability,
            ):
                return "simulation_cancel_failure"
            return f"simulation_{stage}"
    return None


def _public_queue_fill_metadata(
    market: BinaryMarket,
    signal_yes: OrderBookSide,
    signal_no: OrderBookSide,
    fill_yes: OrderBookSide,
    fill_no: OrderBookSide,
    signal_execution: Mapping[str, Any],
    evidence: FillTimeBookEvidence,
) -> tuple[dict[str, Any], dict[str, float] | None]:
    prices = {
        "yes": _intended_prices(signal_execution.get("details"), "yes"),
        "no": _intended_prices(signal_execution.get("details"), "no"),
    }
    if not prices["yes"] and signal_yes.best_price is not None:
        prices["yes"].add(signal_yes.best_price)
    if not prices["no"] and signal_no.best_price is not None:
        prices["no"].add(signal_no.best_price)

    queue_ahead_yes = _queue_ahead_at_prices(signal_yes, prices["yes"])
    queue_ahead_no = _queue_ahead_at_prices(signal_no, prices["no"])
    yes_price_change_size, yes_trade_size, yes_deltas, yes_trades = _observed_queue_decrease(
        evidence,
        token_id=market.yes_token_id,
        prices=prices["yes"],
        side_name="yes",
    )
    no_price_change_size, no_trade_size, no_deltas, no_trades = _observed_queue_decrease(
        evidence,
        token_id=market.no_token_id,
        prices=prices["no"],
        side_name="no",
    )
    has_public_evidence = bool(yes_deltas or no_deltas or yes_trades or no_trades)
    target_quantity = _as_float(signal_execution.get("quantity"))
    side_fill_quantities: dict[str, float] | None = None
    if has_public_evidence:
        side_fill_quantities = {
            "yes": min(target_quantity, max(0.0, yes_price_change_size + yes_trade_size)),
            "no": min(target_quantity, max(0.0, no_price_change_size + no_trade_size)),
        }
    metadata = {
        "source": "public_trade_delta_evidence" if has_public_evidence else "no_public_queue_evidence",
        "intended_prices": {
            "yes": sorted(prices["yes"]),
            "no": sorted(prices["no"]),
        },
        "queue_ahead_at_signal": {
            "yes": queue_ahead_yes,
            "no": queue_ahead_no,
        },
        "observed_public_support": {
            "yes": yes_price_change_size + yes_trade_size,
            "no": no_price_change_size + no_trade_size,
        },
        "observed_price_change_size_decrease": {
            "yes": yes_price_change_size,
            "no": no_price_change_size,
        },
        "observed_trade_print_size": {
            "yes": yes_trade_size,
            "no": no_trade_size,
        },
        "price_delta_evidence": {
            "yes": yes_deltas,
            "no": no_deltas,
        },
        "trade_print_evidence": {
            "yes": yes_trades,
            "no": no_trades,
        },
        "fill_books_available_depth": {
            "yes": fill_yes.available_size,
            "no": fill_no.available_size,
        },
    }
    return metadata, side_fill_quantities


def evaluate_binary_paper_execution(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    state: Mapping[str, Any],
    params: PaperPortfolioParams,
    as_of: datetime | None = None,
    fill_time_book_reader: FillTimeBookReader | None = None,
) -> PaperPortfolioDecision:
    signal_time = _ensure_aware(as_of or _utc_now())
    signal_decision = _evaluate_optimistic_binary_paper_execution(
        market,
        yes_asks,
        no_asks,
        state=state,
        params=params,
        as_of=signal_time,
    )
    simulation = params.simulation
    if signal_decision.action != "EXECUTE" or signal_decision.execution is None or simulation.is_zero_friction:
        return signal_decision

    signal_execution = signal_decision.execution
    signal_fingerprint = str(signal_execution["book_fingerprint"])
    fill_time, simulation_metadata = _simulation_latency_fields(
        simulation,
        market,
        signal_fingerprint,
        signal_time,
    )
    simulation_metadata["signal_book_fingerprint"] = signal_fingerprint
    simulation_metadata["signal_source_timestamps"] = _book_timestamps(yes_asks, no_asks)
    simulation_metadata["live_public_data"] = {
        "signal_books": {
            "yes": _book_audit(yes_asks),
            "no": _book_audit(no_asks),
        }
    }
    simulation_metadata["inferred"] = {}
    simulation_metadata["fallback"] = {}

    target_quantity = _as_float(signal_execution.get("quantity"))

    if fill_time_book_reader is not None:
        try:
            evidence = _normalize_fill_time_evidence(fill_time_book_reader(market, fill_time))
        except Exception as exc:
            evidence = FillTimeBookEvidence(
                source="error",
                public_error=f"{type(exc).__name__}: {exc}",
            )
    else:
        evidence = FillTimeBookEvidence(
            source="signal_fallback",
            yes_book=yes_asks,
            no_book=no_asks,
            observed_at=signal_time,
            fallback_reason="no_fill_time_public_reader",
        )
    fill_time, telemetry_latency_metadata = _simulation_latency_fields_with_requests(
        simulation,
        market,
        signal_fingerprint,
        signal_time,
        request_records=evidence.request_records,
    )
    simulation_metadata.update(telemetry_latency_metadata)
    fill_yes = evidence.yes_book
    fill_no = evidence.no_book
    simulation_metadata["book_source"] = evidence.source
    simulation_metadata["live_public_data"].update(
        {
            "fill_time": _fill_evidence_audit(evidence, market=market),
            "book_comparison": _signal_fill_book_comparison(yes_asks, no_asks, fill_yes, fill_no),
        }
    )

    if evidence.fallback_reason and not simulation.allow_deterministic_fill_fallback:
        return _simulation_failure_decision(
            "simulation_no_fill_time_public_source",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
            fallback_reason=evidence.fallback_reason,
        )

    if evidence.public_error or evidence.errors:
        return _simulation_failure_decision(
            "simulation_public_data_error",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
            public_error=evidence.public_error,
            errors=dict(evidence.errors),
        )

    ready_flags = dict(evidence.snapshot_ready)
    unready_tokens = [token_id for token_id, ready in ready_flags.items() if not ready]
    if unready_tokens:
        return _simulation_failure_decision(
            "simulation_ws_stale_fill_window" if evidence.source == "ws_cache" else "simulation_unready_fill_time_book",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
            unready_tokens=unready_tokens,
        )

    if fill_yes is None or fill_no is None:
        return _simulation_failure_decision(
            "simulation_missing_fill_time_book",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
            yes_book_present=fill_yes is not None,
            no_book_present=fill_no is not None,
        )

    timeout_ms = max(0.0, simulation.local_timeout_ms)
    if timeout_ms > 0.0:
        timed_out_records = [
            dict(record)
            for record in evidence.request_records
            if _as_float(record.get("latency_seconds")) * 1000.0 > timeout_ms + EPSILON
        ]
        if timed_out_records:
            return _simulation_failure_decision(
                "simulation_local_timeout",
                market=market,
                simulation=simulation_metadata,
                book_fingerprint=signal_fingerprint,
                timed_out_requests=timed_out_records,
                local_timeout_ms=timeout_ms,
            )

    for label, book in (("yes", fill_yes), ("no", fill_no)):
        age = _stale_seconds(book, fill_time)
        if age is not None and (age < -EPSILON or age > params.max_book_age_seconds):
            return _simulation_failure_decision(
                "simulation_stale_fill_time_book",
                market=market,
                simulation=simulation_metadata,
                book_fingerprint=signal_fingerprint,
                side=label,
                age_seconds=age,
                max_age_seconds=params.max_book_age_seconds,
            )

    if simulation.max_fill_price_move_bps > 0.0:
        fill_quantity, fill_yes_cost, fill_no_cost, _, _ = _simulate_paired_tranches(
            fill_yes,
            fill_no,
            cash=_as_float(state.get("cash"), params.starting_capital_usd),
            params=params,
            max_quantity=target_quantity,
        )
        signal_quantity = _as_float(signal_execution.get("quantity"))
        signal_gross_cost = _as_float(signal_execution.get("gross_cost"))
        fill_gross_cost = fill_yes_cost + fill_no_cost
        if fill_quantity > EPSILON and signal_quantity > EPSILON and signal_gross_cost > EPSILON:
            signal_unit_cost = signal_gross_cost / signal_quantity
            fill_unit_cost = fill_gross_cost / fill_quantity
            move_bps = ((fill_unit_cost - signal_unit_cost) / signal_unit_cost) * 10_000.0
            simulation_metadata["price_move_bps"] = move_bps
            if move_bps > simulation.max_fill_price_move_bps + EPSILON:
                return _simulation_failure_decision(
                    "simulation_fill_price_moved",
                    market=market,
                    simulation=simulation_metadata,
                    book_fingerprint=signal_fingerprint,
                    signal_unit_cost=signal_unit_cost,
                    fill_unit_cost=fill_unit_cost,
                    max_fill_price_move_bps=simulation.max_fill_price_move_bps,
                )

    throttle_probability, pressure_metadata = _local_pressure_failure_probability(simulation, evidence)
    throttle_draw = _stage_random(simulation, market, signal_fingerprint, "local_public_pressure")
    throttle_saturated = throttle_probability > 0.0 and throttle_draw <= throttle_probability
    simulation_metadata["throttle"] = {
        "saturated": throttle_saturated,
        "draw": throttle_draw,
        "quantity_ratio": simulation.throttle_quantity_ratio,
        "max_submissions_per_second": simulation.throttle_max_submissions_per_second,
        **pressure_metadata,
    }
    simulation_metadata["inferred"]["rate_limit_or_local_pressure"] = pressure_metadata
    if pressure_metadata["source"].endswith("_fallback"):
        simulation_metadata["fallback"]["rate_limit_or_local_pressure"] = pressure_metadata
    if throttle_saturated:
        target_quantity *= max(0.0, min(1.0, simulation.throttle_quantity_ratio))
        if target_quantity + EPSILON < market.effective_min_order_size:
            return _simulation_failure_decision(
                "simulation_local_pressure_min_size",
                market=market,
                simulation=simulation_metadata,
                book_fingerprint=signal_fingerprint,
                degraded_quantity=target_quantity,
                min_quantity=market.effective_min_order_size,
            )

    tranches = _capped_signal_tranches(
        signal_execution,
        signal_yes=yes_asks,
        signal_no=no_asks,
        target_quantity=target_quantity,
    )
    public_fill_yes, yes_supported_quantity, yes_eligibility = _fill_eligibility_for_leg(
        simulation=simulation,
        token_id=market.yes_token_id,
        side_name="yes",
        signal_book=yes_asks,
        fill_book=fill_yes,
        signal_execution=signal_execution,
        evidence=evidence,
        tranches=tranches,
        target_quantity=target_quantity,
    )
    public_fill_no, no_supported_quantity, no_eligibility = _fill_eligibility_for_leg(
        simulation=simulation,
        token_id=market.no_token_id,
        side_name="no",
        signal_book=no_asks,
        fill_book=fill_no,
        signal_execution=signal_execution,
        evidence=evidence,
        tranches=tranches,
        target_quantity=target_quantity,
    )
    side_fill_quantities = {"yes": yes_supported_quantity, "no": no_supported_quantity}
    simulation_metadata["fill_eligibility"] = {
        "mode": simulation.fill_eligibility_mode,
        "yes": yes_eligibility,
        "no": no_eligibility,
    }
    simulation_metadata["queue"] = {
        "applied": True,
        "source": "public_fill_eligibility",
        "yes": yes_eligibility,
        "no": no_eligibility,
    }
    simulation_metadata["inferred"]["queue"] = {
        "yes_source": yes_eligibility["source"],
        "no_source": no_eligibility["source"],
    }

    queue_metadata, queue_side_fill_quantities = _public_queue_fill_metadata(
        market,
        yes_asks,
        no_asks,
        fill_yes,
        fill_no,
        signal_execution,
        evidence,
    )
    simulation_metadata["queue"]["public_queue_evidence"] = queue_metadata
    simulation_metadata["inferred"]["queue"]["public_queue_source"] = queue_metadata["source"]

    working_fill_yes = public_fill_yes
    working_fill_no = public_fill_no
    public_support_available = (
        not evidence.fallback_reason
        and yes_supported_quantity > EPSILON
        and no_supported_quantity > EPSILON
    )
    if public_support_available:
        simulation_metadata["adverse_selection"] = {
            "source": "disabled",
            "applied": False,
            "probability": 0.0,
            "depth_removal_ratio": 0.0,
            "price_move_bps": 0.0,
        }
        if queue_side_fill_quantities is not None:
            side_fill_quantities = queue_side_fill_quantities
        else:
            side_fill_quantities = None
        for side_name, supported_quantity in (side_fill_quantities or {}).items():
            if supported_quantity > EPSILON and supported_quantity + EPSILON < market.effective_min_order_size:
                return _simulation_failure_decision(
                    "simulation_public_fill_below_min_size",
                    market=market,
                    simulation=simulation_metadata,
                    book_fingerprint=signal_fingerprint,
                    side=side_name,
                    supported_quantity=supported_quantity,
                    min_quantity=market.effective_min_order_size,
                )
    elif not simulation.allow_deterministic_fill_fallback:
        return _simulation_failure_decision(
            "simulation_insufficient_public_fill_evidence",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
            yes_supported_quantity=yes_supported_quantity,
            no_supported_quantity=no_supported_quantity,
        )
    else:
        side_fill_quantities = None
        fallback_queue_metadata = dict(queue_metadata)
        fallback_queue_metadata["source"] = "deterministic_depth_fallback"
        simulation_metadata["queue"] = {
            "applied": True,
            "depth_ratio": simulation.queue_depth_ratio,
            "fill_probability": simulation.queue_fill_probability,
            "fill_draw": _stage_random(simulation, market, signal_fingerprint, "queue_fill"),
            **fallback_queue_metadata,
        }
        simulation_metadata["fallback"]["queue"] = fallback_queue_metadata
        if simulation.queue_depth_ratio > 0.0:
            working_fill_yes = _scale_book_depth(working_fill_yes, simulation.queue_depth_ratio)
            working_fill_no = _scale_book_depth(working_fill_no, simulation.queue_depth_ratio)
            queued_signal_quantity, _, _, _, _ = _simulate_paired_tranches(
                working_fill_yes,
                working_fill_no,
                cash=_as_float(state.get("cash"), params.starting_capital_usd),
                params=params,
                max_quantity=target_quantity,
            )
            if queued_signal_quantity + EPSILON < market.effective_min_order_size:
                return _simulation_failure_decision(
                    "simulation_queue_min_size",
                    market=market,
                    simulation=simulation_metadata,
                    book_fingerprint=signal_fingerprint,
                    available_equal_depth=queued_signal_quantity,
                    min_quantity=market.effective_min_order_size,
                )
            if (
                simulation.queue_fill_probability > 0.0
                and simulation_metadata["queue"]["fill_draw"] > simulation.queue_fill_probability
            ):
                return _simulation_failure_decision(
                    "simulation_queue_unfilled",
                    market=market,
                    simulation=simulation_metadata,
                    book_fingerprint=signal_fingerprint,
                    fill_probability=simulation.queue_fill_probability,
                )

        if simulation.adverse_selection_probability > 0.0:
            adverse_draw = _stage_random(simulation, market, signal_fingerprint, "adverse_selection")
            adverse_applied = adverse_draw <= simulation.adverse_selection_probability
            simulation_metadata["adverse_selection"] = {
                "source": "deterministic_adverse_selection_fallback",
                "applied": adverse_applied,
                "draw": adverse_draw,
                "probability": simulation.adverse_selection_probability,
                "depth_removal_ratio": simulation.adverse_depth_removal_ratio if adverse_applied else 0.0,
                "price_move_bps": simulation.adverse_price_move_bps if adverse_applied else 0.0,
            }
            simulation_metadata["fallback"]["adverse_selection"] = simulation_metadata["adverse_selection"]
            if adverse_applied:
                working_fill_yes = _adverse_adjust_book(
                    working_fill_yes,
                    removal_ratio=simulation.adverse_depth_removal_ratio,
                    price_move_bps=simulation.adverse_price_move_bps,
                )
                working_fill_no = _adverse_adjust_book(
                    working_fill_no,
                    removal_ratio=simulation.adverse_depth_removal_ratio,
                    price_move_bps=simulation.adverse_price_move_bps,
                )
        else:
            simulation_metadata["adverse_selection"] = {
                "source": "disabled",
                "applied": False,
                "probability": 0.0,
                "depth_removal_ratio": 0.0,
                "price_move_bps": 0.0,
            }

    legacy_failure = _legacy_probability_failure(simulation, market, signal_fingerprint)
    if legacy_failure is not None:
        simulation_metadata["fallback"]["legacy_failure_probability"] = {
            "reason": legacy_failure,
            "source": "deterministic_legacy_probability_fallback",
            "note": "No private live order lifecycle is observed in paper mode.",
        }
        return _simulation_failure_decision(
            legacy_failure,
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
        )

    return _finalize_execution_from_books(
        market,
        working_fill_yes,
        working_fill_no,
        state=state,
        params=params,
        as_of=fill_time,
        max_quantity=target_quantity,
        simulation_metadata=simulation_metadata,
        signal_execution=signal_execution,
        evidence=evidence,
        side_fill_quantities=side_fill_quantities,
    )


class PaperPortfolio:
    def __init__(
        self,
        path: str | Path | None = None,
        *,
        events_path: str | Path | None = None,
        params: PaperPortfolioParams | None = None,
    ) -> None:
        self.path = Path(path or config.paper_portfolio_instance_path())
        self.params = params or PaperPortfolioParams.from_config()
        self.events = AppendOnlyJsonl(events_path or config.paper_portfolio_events_path())
        self.state: dict[str, Any] = {}

    def load(self) -> "PaperPortfolio":
        self.state = self._read_state()
        return self

    def _read_state(self) -> dict[str, Any]:
        if not self.path.exists():
            return initial_portfolio_state(self.params)
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperPortfolioLoadError(f"failed to load paper portfolio {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PaperPortfolioLoadError(
                f"paper portfolio {self.path} must contain a JSON object, got {type(data).__name__}"
            )
        return _normalized_state(data, self.params)

    def save(self) -> None:
        if not self.state:
            self.state = initial_portfolio_state(self.params)
        self._save_state(self.state)

    def _prepare_state_for_save(self, state: dict[str, Any]) -> None:
        state["cash"] = _as_float(state.get("cash"), self.params.starting_capital_usd)
        state["inventory"] = _inventory_rows(state)
        state["total_equity"] = state["cash"] + _inventory_equity_value(state)
        metadata = state.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["pending_settlement_count"] = sum(
                1 for row in _inventory_rows(state).values() if bool(row.get("pending_settlement"))
            )
            settlements = state.get("settlements") if isinstance(state.get("settlements"), list) else []
            metadata["settlements_applied_count"] = len(settlements)
            metadata["updated_at_utc"] = utc_iso()

    def _save_state(self, state: dict[str, Any]) -> None:
        if not state:
            state.update(initial_portfolio_state(self.params))
        self._prepare_state_for_save(state)
        self._write_state(state)

    def _write_state(self, state: Mapping[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(jsonable(state), f, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def _reload_after_failed_save(self, fallback_state: Mapping[str, Any]) -> None:
        try:
            self.state = self._read_state() if self.path.exists() else deepcopy(dict(fallback_state))
        except PaperPortfolioLoadError:
            self.state = deepcopy(dict(fallback_state))

    def append_event(self, event_type: str, payload: dict[str, Any] | None = None, **fields: Any) -> dict[str, Any]:
        merged = dict(payload or {})
        merged.update(fields)
        record = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": utc_iso(),
            "event_type": event_type,
        }
        record.update(merged)
        self.events.append(record)
        return record

    def reset(self, *, yes: bool) -> dict[str, Any]:
        if not yes:
            raise ValueError("reset requires --yes")
        self.state = initial_portfolio_state(self.params)
        self.save()
        self.append_event(
            "paper_portfolio_reset",
            {
                "starting_capital_usd": self.state["starting_capital_usd"],
                "cash": self.state["cash"],
            },
        )
        return self.state

    def status(self) -> dict[str, Any]:
        state = self._read_state()
        executions = state.get("executions") if isinstance(state.get("executions"), list) else []
        wins = sum(1 for execution in executions if _as_float(execution.get("net_profit")) > EPSILON)
        trade_count = len(executions)
        starting_capital = _as_float(state.get("starting_capital_usd"), self.params.starting_capital_usd)
        cash = _as_float(state.get("cash"), starting_capital)
        equity = cash + _inventory_equity_value(state)
        costs = state.get("costs") if isinstance(state.get("costs"), Mapping) else {}
        inventory = list(_inventory_rows(state).values())
        last_execution = executions[-1].get("executed_at_utc") if executions else None
        settlements = state.get("settlements") if isinstance(state.get("settlements"), list) else []
        last_settlement = settlements[-1].get("settled_at_utc") if settlements else None
        return {
            "starting_capital_usd": starting_capital,
            "cash": cash,
            "realized_pnl": _as_float(state.get("realized_pnl")),
            "total_equity": equity,
            "return_pct": ((equity - starting_capital) / starting_capital) * 100.0
            if starting_capital > 0
            else 0.0,
            "trade_count": trade_count,
            "win_rate_pct": (wins / trade_count) * 100.0 if trade_count else 0.0,
            "costs": {
                "fees_usd": _as_float(costs.get("fees_usd") if isinstance(costs, Mapping) else None),
                "slippage_usd": _as_float(costs.get("slippage_usd") if isinstance(costs, Mapping) else None),
                "tax_usd": _as_float(costs.get("tax_usd") if isinstance(costs, Mapping) else None),
                "merge_usd": _as_float(costs.get("merge_usd") if isinstance(costs, Mapping) else None),
            },
            "last_execution_at_utc": last_execution,
            "pending_settlement_count": sum(1 for row in inventory if bool(row.get("pending_settlement"))),
            "settlements_applied_count": len(settlements),
            "last_settlement_at_utc": last_settlement,
            "unmatched_inventory": inventory,
        }

    def open_inventory_market_ids(self) -> set[str]:
        if not self.state:
            self.load()
        return _open_inventory_market_ids(self.state)

    def reconcile_public_markets(
        self,
        *,
        markets_by_id: Mapping[str, BinaryMarket],
        resolution_events_by_market: Mapping[str, Sequence[Mapping[str, Any]]] | None = None,
        valuation_snapshots_by_token: Mapping[str, Mapping[str, Any]] | None = None,
        as_of: datetime | None = None,
    ) -> dict[str, Any]:
        if not self.state:
            self.load()
        base_state = deepcopy(self.state)
        working_state = deepcopy(self.state)
        now = _ensure_aware(as_of or _utc_now())
        inventory = working_state.get("inventory")
        if not isinstance(inventory, dict):
            inventory = {}
            working_state["inventory"] = inventory
        settlements = working_state.setdefault("settlements", [])
        if not isinstance(settlements, list):
            settlements = []
            working_state["settlements"] = settlements
        existing_keys = {
            str(row.get("settlement_key"))
            for row in settlements
            if isinstance(row, Mapping) and row.get("settlement_key") not in (None, "")
        }

        resolution_events = resolution_events_by_market or {}
        valuation_snapshots = valuation_snapshots_by_token or {}
        settlement_records: list[dict[str, Any]] = []
        pending_settlement_count = 0
        changed = False

        def latest_resolution_row(market_id: str) -> Mapping[str, Any] | None:
            rows = resolution_events.get(market_id)
            if isinstance(rows, Sequence) and rows:
                for row in reversed(rows):
                    if isinstance(row, Mapping):
                        return row
            return None

        def valuation_for_token(token_id: str) -> tuple[float, str]:
            snapshot = valuation_snapshots.get(token_id)
            if not isinstance(snapshot, Mapping):
                return 0.0, "zero"
            books = snapshot.get("books") if isinstance(snapshot.get("books"), Mapping) else {}
            best_bid = 0.0
            best_ask = 0.0
            recent_bba = snapshot.get("recent_best_bid_asks")
            if isinstance(recent_bba, list) and recent_bba:
                last_bba = recent_bba[-1]
                if isinstance(last_bba, Mapping):
                    best_bid = _as_float(last_bba.get("best_bid"))
                    best_ask = _as_float(last_bba.get("best_ask"))
            if best_bid <= EPSILON and isinstance(books.get("bid"), Mapping):
                best_bid = _as_float(books["bid"].get("best_price"))
            if best_ask <= EPSILON and isinstance(books.get("ask"), Mapping):
                best_ask = _as_float(books["ask"].get("best_price"))
            if self.params.simulation.unmatched_open_valuation == "best_bid_midpoint_or_zero":
                if best_bid > EPSILON and best_ask > EPSILON:
                    return (best_bid + best_ask) / 2.0, "best_bid_midpoint"
                if best_bid > EPSILON:
                    return best_bid, "best_bid"
            return 0.0, "zero"

        def winner_details(market: BinaryMarket, resolution_row: Mapping[str, Any] | None) -> tuple[str | None, str | None, str]:
            if isinstance(resolution_row, Mapping):
                winner_token = resolution_row.get("winning_asset_id")
                winner_outcome = resolution_row.get("winning_outcome")
                if winner_token not in (None, "") or winner_outcome not in (None, ""):
                    return (
                        str(winner_token) if winner_token not in (None, "") else None,
                        str(winner_outcome).upper() if winner_outcome not in (None, "") else None,
                        "ws_market_resolved",
                    )
            metadata = market.metadata if isinstance(market.metadata, Mapping) else {}
            winner_token = metadata.get("winner_token_id")
            winner_outcome = metadata.get("winner_outcome")
            return (
                str(winner_token) if winner_token not in (None, "") else None,
                str(winner_outcome).upper() if winner_outcome not in (None, "") else None,
                "public_metadata",
            )

        def market_looks_resolved(market: BinaryMarket, resolution_row: Mapping[str, Any] | None) -> bool:
            if resolution_row is not None:
                return True
            metadata = market.metadata if isinstance(market.metadata, Mapping) else {}
            status_values = {
                str(metadata.get("uma_resolution_status") or "").strip().lower(),
                str(metadata.get("resolution_status") or "").strip().lower(),
            }
            return market.closed or bool(status_values & {"resolved", "finalized", "complete", "settled"})

        for token_id, row in list(_inventory_rows(working_state).items()):
            market_id = str(row.get("market_id") or "")
            market = markets_by_id.get(market_id)
            if market is None:
                pending_settlement_count += 1
                row["pending_settlement"] = True
                inventory[token_id] = row
                continue
            valuation_price, valuation_source = valuation_for_token(token_id)
            row["last_valuation_price"] = valuation_price
            row["last_valuation_usd"] = row["quantity"] * valuation_price
            row["last_valuation_source"] = valuation_source
            row["last_valued_at_utc"] = utc_iso(now)
            resolution_row = latest_resolution_row(market_id)
            resolved = market_looks_resolved(market, resolution_row)
            winner_token_id, winner_outcome, settlement_source = winner_details(market, resolution_row)
            winner_matches_market = winner_token_id in {
                None,
                market.yes_token_id,
                market.no_token_id,
            } and winner_outcome in {None, "YES", "NO"}
            row["pending_settlement"] = False
            if not self.params.simulation.settlement_enabled:
                inventory[token_id] = row
                continue
            if market.neg_risk:
                pending_settlement_count += 1
                row["pending_settlement"] = True
                inventory[token_id] = row
                continue
            if not resolved:
                inventory[token_id] = row
                continue
            if self.params.simulation.settlement_require_winner and (
                (winner_token_id in (None, "") and winner_outcome in (None, ""))
                or not winner_matches_market
            ):
                pending_settlement_count += 1
                row["pending_settlement"] = True
                inventory[token_id] = row
                continue
            winning = bool(
                (winner_token_id not in (None, "") and winner_token_id == token_id)
                or (winner_outcome not in (None, "") and winner_outcome == str(row.get("outcome") or "").upper())
            )
            settlement_key = f"{market_id}:{token_id}:{winner_token_id or winner_outcome or 'unknown'}"
            if settlement_key in existing_keys:
                inventory.pop(token_id, None)
                changed = True
                continue
            realized_value = row["quantity"] if winning else 0.0
            cost_basis = _as_float(row.get("cost_basis_usd"))
            working_state["cash"] = _as_float(working_state.get("cash"), self.params.starting_capital_usd) + realized_value
            working_state["realized_pnl"] = _as_float(working_state.get("realized_pnl")) + (realized_value - cost_basis)
            settlement_record = {
                "settlement_key": settlement_key,
                "market_id": market_id,
                "token_id": token_id,
                "outcome": row.get("outcome"),
                "quantity": row["quantity"],
                "winning_token_id": winner_token_id,
                "winning_outcome": winner_outcome,
                "resolved_winning": winning,
                "settlement_source": settlement_source,
                "settled_value_usd": realized_value,
                "write_down_value_usd": max(0.0, cost_basis - realized_value),
                "cost_basis_usd": cost_basis,
                "paper_only": True,
                "settled_at_utc": utc_iso(now),
            }
            settlements.append(settlement_record)
            settlement_records.append(settlement_record)
            existing_keys.add(settlement_key)
            inventory.pop(token_id, None)
            changed = True

        working_state["inventory"] = inventory
        working_state["total_equity"] = _as_float(working_state.get("cash"), self.params.starting_capital_usd) + _inventory_equity_value(
            working_state
        )
        metadata = working_state.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["pending_settlement_count"] = pending_settlement_count
            metadata["settlements_applied_count"] = len(settlements)
            metadata["last_settlement_at_utc"] = settlement_records[-1]["settled_at_utc"] if settlement_records else metadata.get(
                "last_settlement_at_utc"
            )
        changed = changed or working_state != base_state
        summary = {
            "pending_settlement_count": pending_settlement_count,
            "settlements_applied": len(settlement_records),
            "last_settlement_at_utc": settlement_records[-1]["settled_at_utc"] if settlement_records else None,
            "settlements": settlement_records,
        }
        if not changed:
            self.state = working_state
            return summary
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise
        self.state = working_state
        for record in settlement_records:
            try:
                self.append_event("paper_portfolio_settlement", record)
            except Exception as exc:
                LOGGER.warning(
                    "paper_portfolio_settlement_event_append_failed settlement_key=%s error=%r",
                    record.get("settlement_key"),
                    exc,
                )
        return summary

    def execute_binary_complete_set(
        self,
        market: BinaryMarket,
        yes_asks: OrderBookSide,
        no_asks: OrderBookSide,
        *,
        as_of: datetime | None = None,
        params: PaperPortfolioParams | None = None,
        fill_time_book_reader: FillTimeBookReader | None = None,
    ) -> PaperPortfolioDecision:
        if not self.state:
            self.load()
        execution_params = params or self.params
        base_state = deepcopy(self.state)
        decision = evaluate_binary_paper_execution(
            market,
            yes_asks,
            no_asks,
            state=base_state,
            params=execution_params,
            as_of=as_of,
            fill_time_book_reader=fill_time_book_reader,
        )
        if decision.action != "EXECUTE" or decision.execution is None:
            if decision.details.get("simulation_failure"):
                simulation_details = decision.details.get("simulation")
                simulation_payload = dict(simulation_details) if isinstance(simulation_details, Mapping) else {}
                try:
                    self.append_event(
                        "paper_portfolio_execution_failed",
                        {
                            "market_id": market.market_id,
                            "reason": decision.reason,
                            "simulation": simulation_payload,
                            "failure_stage": simulation_payload.get("failure_stage") or decision.reason,
                            "failure_reason": simulation_payload.get("failure_reason") or decision.reason,
                            "details": dict(decision.details),
                        },
                    )
                except Exception as exc:
                    LOGGER.warning(
                        "paper_portfolio_execution_failed_event_append_failed market_id=%s reason=%s error=%r",
                        market.market_id,
                        decision.reason,
                        exc,
                    )
            return decision

        working_state = deepcopy(base_state)
        execution = deepcopy(decision.execution)
        self._apply_execution_to_state(working_state, execution, params=execution_params)
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise

        self.state = working_state
        returned_execution = deepcopy(execution)
        details = dict(decision.details)
        try:
            self.append_event("paper_portfolio_execution", returned_execution)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            details["event_log_error"] = error
            returned_execution["event_log_error"] = error
            LOGGER.warning(
                "paper_portfolio_execution_event_append_failed execution_id=%s error=%r",
                returned_execution.get("execution_id"),
                exc,
            )
        return PaperPortfolioDecision(
            action="EXECUTE",
            execution=returned_execution,
            details=details,
        )

    def _apply_execution(self, execution: dict[str, Any]) -> None:
        self._apply_execution_to_state(self.state, execution, params=self.params)

    def _apply_execution_to_state(
        self,
        state: dict[str, Any],
        execution: dict[str, Any],
        *,
        params: PaperPortfolioParams,
    ) -> None:
        preexisting_redeemed, preexisting_redeemed_cost = self._redeem_completed_pairs_with_cost_from_state(
            state,
            market_id=str(execution["market_id"]),
            yes_token_id=str(execution["yes_token_id"]),
            no_token_id=str(execution["no_token_id"]),
        )
        if preexisting_redeemed > EPSILON:
            state["cash"] = _as_float(state.get("cash"), params.starting_capital_usd) + preexisting_redeemed
            state["realized_pnl"] = _as_float(state.get("realized_pnl")) + (
                preexisting_redeemed - preexisting_redeemed_cost
            )
            normalizations = state.setdefault("inventory_normalizations", [])
            if isinstance(normalizations, list):
                normalizations.append(
                    {
                        "market_id": execution["market_id"],
                        "yes_token_id": execution["yes_token_id"],
                        "no_token_id": execution["no_token_id"],
                        "redeemed_value": preexisting_redeemed,
                        "redeemed_cost_basis_usd": preexisting_redeemed_cost,
                        "normalized_before_execution_id": execution["execution_id"],
                        "normalized_at_utc": execution["executed_at_utc"],
                    }
                )

        cash_before = _as_float(state.get("cash"), params.starting_capital_usd)
        capital_used = _as_float(execution.get("capital_used"))
        state["cash"] = cash_before - capital_used

        yes_quantity = _as_float(execution.get("yes_filled_quantity"), _as_float(execution.get("quantity")))
        no_quantity = _as_float(execution.get("no_filled_quantity"), _as_float(execution.get("quantity")))
        gross_cost = _as_float(execution.get("gross_cost"))
        variable_costs = (
            _as_float(execution.get("estimated_fees"))
            + _as_float(execution.get("slippage_buffer"))
            + _as_float(execution.get("tax_cost"))
        )
        yes_cost = _as_float(execution.get("yes_cost"))
        no_cost = _as_float(execution.get("no_cost"))
        yes_cost_basis = yes_cost + (variable_costs * (yes_cost / gross_cost) if gross_cost > EPSILON else 0.0)
        no_cost_basis = no_cost + (variable_costs * (no_cost / gross_cost) if gross_cost > EPSILON else 0.0)
        self._add_inventory_to_state(
            state,
            token_id=str(execution["yes_token_id"]),
            market_id=str(execution["market_id"]),
            condition_id=execution.get("condition_id"),
            outcome="YES",
            quantity=yes_quantity,
            cost_basis_usd=yes_cost_basis,
            valued_at_utc=execution.get("executed_at_utc"),
        )
        self._add_inventory_to_state(
            state,
            token_id=str(execution["no_token_id"]),
            market_id=str(execution["market_id"]),
            condition_id=execution.get("condition_id"),
            outcome="NO",
            quantity=no_quantity,
            cost_basis_usd=no_cost_basis,
            valued_at_utc=execution.get("executed_at_utc"),
        )
        redeemed, redeemed_cost_basis = self._redeem_completed_pairs_with_cost_from_state(
            state,
            market_id=str(execution["market_id"]),
            yes_token_id=str(execution["yes_token_id"]),
            no_token_id=str(execution["no_token_id"]),
        )
        state["cash"] += redeemed
        cash_after = _as_float(state.get("cash"))

        execution["cash_before"] = cash_before
        execution["cash_after"] = cash_after
        if preexisting_redeemed > EPSILON:
            execution["preexisting_redeemed_value"] = preexisting_redeemed
            execution["preexisting_redeemed_cost_basis_usd"] = preexisting_redeemed_cost
        execution["quantity_redeemed"] = redeemed
        execution["redeemed_cost_basis_usd"] = redeemed_cost_basis
        execution["net_profit"] = redeemed - redeemed_cost_basis - _as_float(execution.get("merge_cost"))
        costs = state.setdefault("costs", {})
        if isinstance(costs, dict):
            costs["fees_usd"] = _as_float(costs.get("fees_usd")) + _as_float(execution.get("estimated_fees"))
            costs["slippage_usd"] = _as_float(costs.get("slippage_usd")) + _as_float(
                execution.get("slippage_buffer")
            )
            costs["tax_usd"] = _as_float(costs.get("tax_usd")) + _as_float(execution.get("tax_cost"))
            costs["merge_usd"] = _as_float(costs.get("merge_usd")) + _as_float(execution.get("merge_cost"))
        state["realized_pnl"] = _as_float(state.get("realized_pnl")) + _as_float(execution.get("net_profit"))
        executions = state.setdefault("executions", [])
        if isinstance(executions, list):
            executions.append(execution)
        fingerprints = state.setdefault("book_fingerprints", {})
        if isinstance(fingerprints, dict):
            fingerprints[str(execution["market_id"])] = {
                "fingerprint": execution["book_fingerprint"],
                "execution_id": execution["execution_id"],
                "executed_at_utc": execution["executed_at_utc"],
            }
        state["last_execution_at_utc"] = execution["executed_at_utc"]
        state["total_equity"] = state["cash"] + _inventory_equity_value(state)

    def _add_inventory(
        self,
        *,
        token_id: str,
        market_id: str,
        condition_id: str | None,
        outcome: str,
        quantity: float,
        cost_basis_usd: float = 0.0,
        valued_at_utc: Any = None,
    ) -> None:
        self._add_inventory_to_state(
            self.state,
            token_id=token_id,
            market_id=market_id,
            condition_id=condition_id,
            outcome=outcome,
            quantity=quantity,
            cost_basis_usd=cost_basis_usd,
            valued_at_utc=valued_at_utc,
        )

    @staticmethod
    def _add_inventory_to_state(
        state: dict[str, Any],
        *,
        token_id: str,
        market_id: str,
        condition_id: str | None,
        outcome: str,
        quantity: float,
        cost_basis_usd: float = 0.0,
        valued_at_utc: Any = None,
    ) -> None:
        if quantity <= EPSILON:
            return
        inventory = state.setdefault("inventory", {})
        if not isinstance(inventory, dict):
            inventory = {}
            state["inventory"] = inventory
        row = dict(inventory.get(token_id) or {})
        previous_quantity = _as_float(row.get("quantity"))
        previous_cost_basis = _as_float(row.get("cost_basis_usd"))
        row.update(
            {
                "token_id": token_id,
                "market_id": market_id,
                "condition_id": condition_id,
                "outcome": outcome,
            }
        )
        row["quantity"] = previous_quantity + quantity
        row["cost_basis_usd"] = previous_cost_basis + max(0.0, cost_basis_usd)
        row["last_valuation_price"] = _as_float(row.get("last_valuation_price"))
        row["last_valuation_usd"] = _as_float(row.get("last_valuation_usd"))
        row["last_valuation_source"] = row.get("last_valuation_source") or "zero"
        row["last_valued_at_utc"] = row.get("last_valued_at_utc") or valued_at_utc
        row["pending_settlement"] = bool(row.get("pending_settlement"))
        inventory[token_id] = row

    def _redeem_completed_pairs(self, *, market_id: str, yes_token_id: str, no_token_id: str) -> float:
        return self._redeem_completed_pairs_from_state(
            self.state,
            market_id=market_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )

    @staticmethod
    def _redeem_completed_pairs_from_state(
        state: dict[str, Any],
        *,
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> float:
        redeemed, _cost_basis = PaperPortfolio._redeem_completed_pairs_with_cost_from_state(
            state,
            market_id=market_id,
            yes_token_id=yes_token_id,
            no_token_id=no_token_id,
        )
        return redeemed

    @staticmethod
    def _redeem_completed_pairs_with_cost_from_state(
        state: dict[str, Any],
        *,
        market_id: str,
        yes_token_id: str,
        no_token_id: str,
    ) -> tuple[float, float]:
        inventory = state.get("inventory")
        if not isinstance(inventory, dict):
            return 0.0, 0.0
        yes_row = inventory.get(yes_token_id)
        no_row = inventory.get(no_token_id)
        if not isinstance(yes_row, dict) or not isinstance(no_row, dict):
            return 0.0, 0.0
        if str(yes_row.get("market_id")) != market_id or str(no_row.get("market_id")) != market_id:
            return 0.0, 0.0
        yes_quantity = _as_float(yes_row.get("quantity"))
        no_quantity = _as_float(no_row.get("quantity"))
        redeem_quantity = min(yes_quantity, no_quantity)
        if redeem_quantity <= EPSILON:
            return 0.0, 0.0
        yes_cost_basis = _as_float(yes_row.get("cost_basis_usd"))
        no_cost_basis = _as_float(no_row.get("cost_basis_usd"))
        redeemed_yes_cost = yes_cost_basis * (redeem_quantity / yes_quantity) if yes_quantity > EPSILON else 0.0
        redeemed_no_cost = no_cost_basis * (redeem_quantity / no_quantity) if no_quantity > EPSILON else 0.0
        yes_row["quantity"] = yes_quantity - redeem_quantity
        no_row["quantity"] = no_quantity - redeem_quantity
        yes_row["cost_basis_usd"] = max(0.0, yes_cost_basis - redeemed_yes_cost)
        no_row["cost_basis_usd"] = max(0.0, no_cost_basis - redeemed_no_cost)
        if yes_row["quantity"] <= EPSILON:
            inventory.pop(yes_token_id, None)
        if no_row["quantity"] <= EPSILON:
            inventory.pop(no_token_id, None)
        return redeem_quantity, redeemed_yes_cost + redeemed_no_cost
