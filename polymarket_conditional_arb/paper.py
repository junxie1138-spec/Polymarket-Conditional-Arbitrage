from __future__ import annotations

import hashlib
import json
import logging
import os
import time
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
PORTFOLIO_SCHEMA_VERSION = 3
PAPER_STATE_WRITE_RETRY_ATTEMPTS = 10
PAPER_STATE_WRITE_RETRY_INITIAL_SECONDS = 0.05
PAPER_STATE_WRITE_RETRY_MAX_SECONDS = 1.0
PAPER_STATE_READ_RETRY_ATTEMPTS = 3
PAPER_STATE_READ_RETRY_INITIAL_SECONDS = 0.01
PAPER_STATE_READ_RETRY_MAX_SECONDS = 0.05
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
    min_cash_reserve_usd: float = 0.0
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
            min_cash_reserve_usd=loaded.paper_min_cash_reserve_usd,
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


def _step_plan_payload(simulation: config.PaperExecutionSimulationConfig) -> dict[str, Any]:
    return {
        "step_quantity_shares": simulation.step_quantity_shares,
        "max_step_count": simulation.max_step_count,
        "grow_step_size_after_success": simulation.grow_step_size_after_success,
        "merge_cost_per_step": simulation.merge_cost_per_step,
    }


def book_pair_fingerprint(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    tranches: tuple[dict[str, float], ...] = (),
    step_plan: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "yes_token_id": market.yes_token_id,
        "no_token_id": market.no_token_id,
        "tranches": _rounded_tranches(tranches),
    }
    if step_plan:
        payload["step_plan"] = dict(step_plan)
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


def _unmatched_inventory_rows(state: Mapping[str, Any]) -> list[dict[str, Any]]:
    inventory = _inventory_rows(state)
    by_market: dict[str, dict[str, dict[str, Any]]] = {}
    for row in inventory.values():
        market_id = str(row.get("market_id") or "")
        outcome = str(row.get("outcome") or "").upper()
        if not market_id or outcome not in {"YES", "NO"}:
            continue
        by_market.setdefault(market_id, {})[outcome] = row

    unmatched: list[dict[str, Any]] = []
    for market_id, rows in sorted(by_market.items()):
        yes_row = rows.get("YES")
        no_row = rows.get("NO")
        yes_quantity = _as_float(yes_row.get("quantity")) if yes_row is not None else 0.0
        no_quantity = _as_float(no_row.get("quantity")) if no_row is not None else 0.0
        paired = min(yes_quantity, no_quantity)
        for outcome, row, quantity in (
            ("YES", yes_row, yes_quantity),
            ("NO", no_row, no_quantity),
        ):
            if row is None:
                continue
            unmatched_quantity = max(0.0, quantity - paired)
            if unmatched_quantity <= EPSILON:
                continue
            cost_basis = _as_float(row.get("cost_basis_usd"))
            cost_per_share = cost_basis / quantity if quantity > EPSILON else 0.0
            unmatched.append(
                {
                    **dict(row),
                    "outcome": outcome,
                    "quantity": unmatched_quantity,
                    "paired_quantity": paired,
                    "inventory_status": "unmatched",
                    "unmatched_quantity": unmatched_quantity,
                    "unmatched_cost_basis_usd": unmatched_quantity * cost_per_share,
                    "unmatched_since_utc": row.get("unmatched_since_utc")
                    or row.get("last_valued_at_utc")
                    or row.get("opened_at_utc"),
                    "opened_by_execution_id": row.get("opened_by_execution_id"),
                    "last_management_action": row.get("last_management_action"),
                }
            )
    return unmatched


def _inventory_by_market_outcome(state: Mapping[str, Any], market_id: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for row in _inventory_rows(state).values():
        if str(row.get("market_id") or "") != market_id:
            continue
        outcome = str(row.get("outcome") or "").upper()
        if outcome in {"YES", "NO"}:
            rows[outcome] = row
    return rows


def _unmatched_inventory_market_ids(state: Mapping[str, Any]) -> set[str]:
    return {str(row.get("market_id") or "") for row in _unmatched_inventory_rows(state) if row.get("market_id")}


def _unmatched_inventory_event_ids(state: Mapping[str, Any]) -> set[str]:
    event_ids: set[str] = set()
    executions = state.get("executions") if isinstance(state.get("executions"), list) else []
    event_by_market = {
        str(row.get("market_id") or ""): str(row.get("event_id") or "")
        for row in executions
        if isinstance(row, Mapping)
    }
    for row in _unmatched_inventory_rows(state):
        event_id = str(row.get("event_id") or "") or event_by_market.get(str(row.get("market_id") or ""), "")
        if event_id:
            event_ids.add(event_id)
    return event_ids


def unmatched_inventory_risk_summary(state: Mapping[str, Any]) -> dict[str, Any]:
    rows = _unmatched_inventory_rows(state)
    total_cost = sum(_as_float(row.get("unmatched_cost_basis_usd")) for row in rows)
    market_ids = sorted({str(row.get("market_id") or "") for row in rows if row.get("market_id")})
    event_ids = sorted(_unmatched_inventory_event_ids(state))
    return {
        "unmatched_position_count": len(rows),
        "unmatched_market_count": len(market_ids),
        "unmatched_event_count": len(event_ids),
        "unmatched_cost_basis_usd_total": total_cost,
        "unmatched_market_ids": market_ids,
        "unmatched_event_ids": event_ids,
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
    spendable_cash = max(0.0, cash - max(0.0, params.min_cash_reserve_usd))
    return min(spendable_cash, params.trade_ceiling_usd)


def _recent_partial_failure_rate(state: Mapping[str, Any], *, window: int = 20) -> float:
    failures = state.get("simulation_failures")
    if not isinstance(failures, list):
        return 0.0
    recent = [
        row
        for row in failures[-max(1, int(window)) :]
        if isinstance(row, Mapping)
    ]
    if not recent:
        return 0.0
    partial_failures = sum(
        1
        for row in recent
        if str(row.get("reason") or "") in {"rejected_partial_pair", "zero_fill"}
    )
    return partial_failures / len(recent)


def _effective_profit_thresholds(
    params: PaperPortfolioParams,
    *,
    simulation: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    effective_slippage_bps: float = 0.0,
) -> tuple[float, float, dict[str, Any]]:
    min_profit = max(0.0, params.min_net_profit_usd)
    min_return_bps = max(0.0, params.min_net_return_bps)
    metadata: dict[str, Any] = {
        "static_min_net_profit_usd": min_profit,
        "static_min_net_return_bps": min_return_bps,
        "dynamic_enabled": bool(params.simulation.dynamic_thresholds_enabled),
        "risk_adjustments": {},
    }
    if not params.simulation.dynamic_thresholds_enabled:
        metadata["effective_min_net_profit_usd"] = min_profit
        metadata["effective_min_net_return_bps"] = min_return_bps
        return min_profit, min_return_bps, metadata

    risk_profit = 0.0
    risk_return_bps = 0.0
    adjustments = metadata["risk_adjustments"]
    sim = simulation or {}

    book_comparison = sim.get("live_public_data", {}).get("book_comparison") if isinstance(sim, Mapping) else None
    if isinstance(book_comparison, Mapping):
        changed_legs = sum(
            1
            for row in book_comparison.values()
            if isinstance(row, Mapping) and bool(row.get("levels_changed"))
        )
        if changed_legs:
            adjustments["book_levels_changed"] = changed_legs
            risk_profit += 0.05 * changed_legs
            risk_return_bps += 10.0 * changed_legs

    fill_time = sim.get("live_public_data", {}).get("fill_time") if isinstance(sim, Mapping) else None
    if isinstance(fill_time, Mapping) and fill_time.get("fallback_reason"):
        adjustments["public_data_fallback"] = str(fill_time["fallback_reason"])
        risk_profit += 0.10
        risk_return_bps += 15.0

    telemetry = sim.get("telemetry") if isinstance(sim, Mapping) else None
    if isinstance(telemetry, Mapping):
        p95_ms = _as_float(telemetry.get("p95_latency_ms"))
        if p95_ms > 0.0:
            latency_excess = max(0.0, p95_ms - 250.0)
            if latency_excess > 0.0:
                adjustments["latency_p95_ms"] = p95_ms
                risk_profit += min(0.50, latency_excess / 1000.0 * 0.10)
                risk_return_bps += min(75.0, latency_excess / 100.0 * 5.0)

    jitter_ms = _as_float(sim.get("latency_jitter_ms")) if isinstance(sim, Mapping) else 0.0
    if jitter_ms > 50.0:
        adjustments["latency_jitter_ms"] = jitter_ms
        risk_profit += min(0.25, (jitter_ms - 50.0) / 1000.0 * 0.10)
        risk_return_bps += min(50.0, (jitter_ms - 50.0) / 100.0 * 3.0)

    slippage_excess = max(0.0, effective_slippage_bps - params.slippage_buffer_bps)
    if slippage_excess > 0.0:
        adjustments["calibrated_slippage_excess_bps"] = slippage_excess
        risk_profit += min(0.50, slippage_excess / 10_000.0 * params.trade_ceiling_usd)
        risk_return_bps += min(100.0, slippage_excess)

    partial_failure_rate = _recent_partial_failure_rate(state or {})
    if partial_failure_rate > 0.0:
        adjustments["recent_partial_failure_rate"] = partial_failure_rate
        risk_profit += min(0.50, partial_failure_rate * 0.25)
        risk_return_bps += min(100.0, partial_failure_rate * 50.0)

    min_profit = max(min_profit, risk_profit)
    min_return_bps = max(min_return_bps, risk_return_bps)
    metadata["effective_min_net_profit_usd"] = min_profit
    metadata["effective_min_net_return_bps"] = min_return_bps
    return min_profit, min_return_bps, metadata


def _profit_threshold_failure(
    *,
    net_profit: float,
    net_return_bps: float,
    params: PaperPortfolioParams,
    simulation: Mapping[str, Any] | None = None,
    state: Mapping[str, Any] | None = None,
    effective_slippage_bps: float = 0.0,
) -> tuple[bool, dict[str, Any]]:
    min_profit, min_return_bps, metadata = _effective_profit_thresholds(
        params,
        simulation=simulation,
        state=state,
        effective_slippage_bps=effective_slippage_bps,
    )
    failed = (
        net_profit <= EPSILON
        or net_profit + EPSILON < min_profit
        or net_return_bps + EPSILON < min_return_bps
    )
    return failed, metadata


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


def _active_execution_state(state: Mapping[str, Any]) -> dict[str, Any] | None:
    active = state.get("active_execution")
    return dict(active) if isinstance(active, Mapping) else None


def _normalize_step_history(raw_steps: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_steps, list):
        return []
    steps: list[dict[str, Any]] = []
    for row in raw_steps:
        if isinstance(row, Mapping):
            steps.append(dict(row))
    return steps


def _merge_cost_for_step(params: PaperPortfolioParams, step_index: int) -> float:
    if params.simulation.merge_cost_per_step:
        return params.merge_cost_usd
    return params.merge_cost_usd if step_index == 1 else 0.0


def _step_target_quantity(
    *,
    remaining_quantity: float,
    params: PaperPortfolioParams,
    current_step_quantity: float,
) -> float:
    return max(
        0.0,
        min(
            remaining_quantity,
            max(
                params.simulation.step_quantity_shares,
                current_step_quantity,
            ),
        ),
    )


def _execute_paper_step(
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
    fingerprint = book_pair_fingerprint(
        market,
        yes_asks,
        no_asks,
        tranches=tranches,
        step_plan=_step_plan_payload(params.simulation),
    )
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
    full_threshold_failed, full_thresholds = _profit_threshold_failure(
        net_profit=full_fill_net_profit,
        net_return_bps=full_fill_net_return_bps,
        params=params,
        simulation=simulation_metadata,
        state=state,
        effective_slippage_bps=effective_slippage_bps,
    )
    simulation_metadata["profit_thresholds"] = full_thresholds
    if full_threshold_failed:
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

    matched_quantity = min(yes_filled_quantity, no_filled_quantity)
    unmatched_yes_quantity = max(0.0, yes_filled_quantity - matched_quantity)
    unmatched_no_quantity = max(0.0, no_filled_quantity - matched_quantity)
    simulation_metadata["partial_fill"] = {
        "applied": partial_applied,
        "source": side_fill_source,
        "policy": params.simulation.pair_fill_policy,
        "target_quantity": quantity,
        "yes_filled_quantity": yes_filled_quantity,
        "no_filled_quantity": no_filled_quantity,
        "matched_quantity": matched_quantity,
        "unmatched_yes_quantity": unmatched_yes_quantity,
        "unmatched_no_quantity": unmatched_no_quantity,
    }
    if yes_filled_quantity <= EPSILON and no_filled_quantity <= EPSILON:
        return _simulation_failure_decision(
            "zero_fill",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            requested_quantity=quantity,
            yes_filled_quantity=yes_filled_quantity,
            no_filled_quantity=no_filled_quantity,
        )
    if abs(yes_filled_quantity - no_filled_quantity) > EPSILON:
        return _simulation_failure_decision(
            "rejected_partial_pair",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            requested_quantity=quantity,
            filled_pair_quantity=matched_quantity,
            yes_filled_quantity=yes_filled_quantity,
            no_filled_quantity=no_filled_quantity,
            unmatched_yes_quantity=unmatched_yes_quantity,
            unmatched_no_quantity=unmatched_no_quantity,
        )
    if matched_quantity <= EPSILON:
        return _simulation_failure_decision(
            "zero_fill",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            requested_quantity=quantity,
            yes_filled_quantity=yes_filled_quantity,
            no_filled_quantity=no_filled_quantity,
        )
    if matched_quantity + EPSILON < min_quantity:
        return _simulation_failure_decision(
            "rejected_partial_pair",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            requested_quantity=quantity,
            filled_pair_quantity=matched_quantity,
            min_quantity=min_quantity,
        )

    yes_actual_cost = _fill_cost(yes_asks, matched_quantity)
    no_actual_cost = _fill_cost(no_asks, matched_quantity)
    gross_cost = yes_actual_cost + no_actual_cost
    merge_cost = _merge_cost_for_step(params, int(_as_float(simulation_metadata.get("step_index"), 1.0)))
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
    threshold_failed, thresholds = _profit_threshold_failure(
        net_profit=net_profit,
        net_return_bps=net_return_bps,
        params=params,
        simulation=simulation_metadata,
        state=state,
        effective_slippage_bps=effective_slippage_bps,
    )
    simulation_metadata["profit_thresholds"] = thresholds
    if threshold_failed:
        return _simulation_failure_decision(
            "simulation_not_profitable_at_fill",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=fingerprint,
            requested_quantity=quantity,
            filled_pair_quantity=matched_quantity,
            gross_cost=gross_cost,
            capital_used=capital_used,
            net_profit=net_profit,
            net_return_bps=net_return_bps,
        )

    fill_status = "full_pair_success" if matched_quantity >= quantity - EPSILON else "paired_partial_success"
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
        "execution_status": fill_status,
        "fill_status": fill_status,
        "requested_quantity": quantity,
        "filled_pair_quantity": matched_quantity,
        "quantity": matched_quantity,
        "quantity_redeemed": matched_quantity,
        "yes_filled_quantity": matched_quantity,
        "no_filled_quantity": matched_quantity,
        "unmatched_yes_quantity": 0.0,
        "unmatched_no_quantity": 0.0,
        "yes_vwap": yes_actual_cost / matched_quantity if matched_quantity > EPSILON else 0.0,
        "no_vwap": no_actual_cost / matched_quantity if matched_quantity > EPSILON else 0.0,
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


def _step_completed_reason(stop_reason: str) -> bool:
    return stop_reason in {
        "cash_or_ceiling_limit",
        "edge_disappeared",
        "depth_exhausted",
        "target_quantity_limit",
        "simulation_queue_unfilled",
        "simulation_queue_min_size",
        "simulation_local_pressure_min_size",
    }


def _step_fingerprint(parent_fingerprint: str, step_index: int, step_quantity: float) -> str:
    payload = json.dumps(
        {
            "parent_book_fingerprint": parent_fingerprint,
            "step_index": step_index,
            "step_quantity": round(float(step_quantity), 12),
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _slice_tranches(
    raw_tranches: Any,
    *,
    offset_quantity: float,
    step_quantity: float,
) -> list[dict[str, float]]:
    if not isinstance(raw_tranches, list):
        return []
    remaining_offset = max(0.0, offset_quantity)
    remaining_step = max(0.0, step_quantity)
    sliced: list[dict[str, float]] = []
    for tranche in raw_tranches:
        if not isinstance(tranche, Mapping):
            continue
        tranche_quantity = max(0.0, _as_float(tranche.get("quantity")))
        if tranche_quantity <= EPSILON:
            continue
        if remaining_offset >= tranche_quantity - EPSILON:
            remaining_offset -= tranche_quantity
            continue
        quantity_available = tranche_quantity - remaining_offset
        remaining_offset = 0.0
        take = min(quantity_available, remaining_step)
        if take <= EPSILON:
            break
        yes_price = _as_float(tranche.get("yes_price"))
        no_price = _as_float(tranche.get("no_price"))
        if yes_price <= EPSILON or no_price <= EPSILON:
            continue
        sliced.append(
            {
                "quantity": take,
                "yes_price": yes_price,
                "no_price": no_price,
                "unit_gross_cost": _as_float(tranche.get("unit_gross_cost"), yes_price + no_price),
            }
        )
        remaining_step -= take
        if remaining_step <= EPSILON:
            break
    return sliced


def _scaled_partial_fill(
    partial_fill: Any,
    *,
    ratio: float,
    target_quantity: float,
    yes_filled_quantity: float,
    no_filled_quantity: float,
) -> dict[str, Any]:
    base = dict(partial_fill) if isinstance(partial_fill, Mapping) else {}
    matched_quantity = min(yes_filled_quantity, no_filled_quantity)
    base.update(
        {
            "target_quantity": target_quantity,
            "yes_filled_quantity": yes_filled_quantity,
            "no_filled_quantity": no_filled_quantity,
            "policy": base.get("policy") or config.DEFAULT_PAPER_PAIR_FILL_POLICY,
            "matched_quantity": matched_quantity,
            "unmatched_yes_quantity": max(0.0, yes_filled_quantity - matched_quantity),
            "unmatched_no_quantity": max(0.0, no_filled_quantity - matched_quantity),
            "parent_scale_ratio": ratio,
        }
    )
    return base


def _build_step_execution(
    planned_execution: Mapping[str, Any],
    *,
    active_execution_id: str,
    step_index: int,
    offset_quantity: float,
    step_quantity: float,
    yes_fill_quantity: float,
    no_fill_quantity: float,
    params: PaperPortfolioParams,
) -> dict[str, Any]:
    planned = deepcopy(dict(planned_execution))
    planned_quantity = max(EPSILON, _as_float(planned.get("quantity")))
    step_fingerprint = _step_fingerprint(str(planned.get("book_fingerprint") or ""), step_index, step_quantity)

    planned_yes_filled = max(0.0, _as_float(planned.get("yes_filled_quantity"), planned_quantity))
    planned_no_filled = max(0.0, _as_float(planned.get("no_filled_quantity"), planned_quantity))
    raw_yes_filled_quantity = min(max(0.0, yes_fill_quantity), step_quantity)
    raw_no_filled_quantity = min(max(0.0, no_fill_quantity), step_quantity)
    matched_quantity = min(raw_yes_filled_quantity, raw_no_filled_quantity)
    yes_filled_quantity = matched_quantity
    no_filled_quantity = matched_quantity
    yes_ratio = yes_filled_quantity / planned_yes_filled if planned_yes_filled > EPSILON else 0.0
    no_ratio = no_filled_quantity / planned_no_filled if planned_no_filled > EPSILON else 0.0
    yes_cost = _as_float(planned.get("yes_cost")) * yes_ratio
    no_cost = _as_float(planned.get("no_cost")) * no_ratio
    gross_cost = yes_cost + no_cost
    planned_gross_cost = _as_float(planned.get("gross_cost"))
    gross_ratio = gross_cost / planned_gross_cost if planned_gross_cost > EPSILON else 0.0
    estimated_fees = _as_float(planned.get("estimated_fees")) * gross_ratio
    slippage_buffer = _as_float(planned.get("slippage_buffer")) * gross_ratio
    tax_cost = _as_float(planned.get("tax_cost")) * gross_ratio
    merge_cost = _merge_cost_for_step(params, step_index)
    capital_used = gross_cost + estimated_fees + slippage_buffer + tax_cost + merge_cost
    net_profit = matched_quantity - capital_used
    net_return_bps = (net_profit / capital_used) * 10_000.0 if capital_used > EPSILON else 0.0
    target_ratio = step_quantity / planned_quantity if planned_quantity > EPSILON else 0.0

    details = dict(planned.get("details")) if isinstance(planned.get("details"), Mapping) else {}
    details["parent_execution_id"] = active_execution_id
    details["parent_book_fingerprint"] = planned.get("book_fingerprint")
    details["step_index"] = step_index
    details["step_offset_quantity"] = offset_quantity
    details["tranches"] = _slice_tranches(
        details.get("tranches"),
        offset_quantity=offset_quantity,
        step_quantity=step_quantity,
    )

    simulation = dict(planned.get("simulation")) if isinstance(planned.get("simulation"), Mapping) else {}
    partial_fill = simulation.get("partial_fill")
    simulation["partial_fill"] = _scaled_partial_fill(
        partial_fill,
        ratio=target_ratio,
        target_quantity=step_quantity,
        yes_filled_quantity=raw_yes_filled_quantity,
        no_filled_quantity=raw_no_filled_quantity,
    )
    simulation["stepped_execution"] = {
        "active_execution_id": active_execution_id,
        "parent_book_fingerprint": planned.get("book_fingerprint"),
        "step_index": step_index,
        "step_offset_quantity": offset_quantity,
        "step_quantity": step_quantity,
        "target_scale_ratio": target_ratio,
        "yes_fill_scale_ratio": yes_ratio,
        "no_fill_scale_ratio": no_ratio,
        "gross_cost_scale_ratio": gross_ratio,
    }
    simulation["step_index"] = step_index

    step_execution = {
        **planned,
        "execution_id": f"{active_execution_id}:step:{step_index}",
        "parent_execution_id": active_execution_id,
        "book_fingerprint": step_fingerprint,
        "execution_status": "full_pair_success" if matched_quantity >= step_quantity - EPSILON else "paired_partial_success",
        "fill_status": "full_pair_success" if matched_quantity >= step_quantity - EPSILON else "paired_partial_success",
        "requested_quantity": step_quantity,
        "filled_pair_quantity": matched_quantity,
        "quantity": matched_quantity,
        "quantity_redeemed": matched_quantity,
        "yes_filled_quantity": yes_filled_quantity,
        "no_filled_quantity": no_filled_quantity,
        "unmatched_yes_quantity": 0.0,
        "unmatched_no_quantity": 0.0,
        "yes_vwap": yes_cost / yes_filled_quantity if yes_filled_quantity > EPSILON else 0.0,
        "no_vwap": no_cost / no_filled_quantity if no_filled_quantity > EPSILON else 0.0,
        "yes_cost": yes_cost,
        "no_cost": no_cost,
        "gross_cost": gross_cost,
        "estimated_fees": estimated_fees,
        "slippage_buffer": slippage_buffer,
        "tax_cost": tax_cost,
        "merge_cost": merge_cost,
        "capital_used": capital_used,
        "redeemed_value": matched_quantity,
        "net_profit": net_profit,
        "net_return_bps": net_return_bps,
        "ceiling_used_usd": capital_used,
        "stop_reason": "target_quantity_limit",
        "simulation": simulation,
        "details": details,
    }
    return step_execution


def _step_summary_rows(steps: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "execution_id": step.get("execution_id"),
            "step_index": step.get("simulation", {}).get("step_index") if isinstance(step.get("simulation"), Mapping) else None,
            "book_fingerprint": step.get("book_fingerprint"),
            "quantity": _as_float(step.get("quantity")),
            "yes_filled_quantity": _as_float(step.get("yes_filled_quantity"), _as_float(step.get("quantity"))),
            "no_filled_quantity": _as_float(step.get("no_filled_quantity"), _as_float(step.get("quantity"))),
            "quantity_redeemed": _as_float(step.get("quantity_redeemed")),
            "capital_used": _as_float(step.get("capital_used")),
            "net_profit": _as_float(step.get("net_profit")),
            "cash_before": step.get("cash_before"),
            "cash_after": step.get("cash_after"),
        }
        for step in steps
    ]


def _aggregate_step_executions(active: Mapping[str, Any], *, params: PaperPortfolioParams) -> dict[str, Any]:
    planned = deepcopy(dict(active.get("planned_execution") if isinstance(active.get("planned_execution"), Mapping) else {}))
    steps = _normalize_step_history(active.get("steps"))
    if not steps:
        return planned
    max_step_count = _active_execution_max_step_count(active, params=params)

    def total(field: str, default: float = 0.0) -> float:
        return sum(_as_float(step.get(field), default) for step in steps)

    step_requested_quantity = sum(
        _as_float(step.get("requested_quantity"), _as_float(step.get("quantity")))
        for step in steps
    )
    requested_quantity = _as_float(
        active.get("requested_quantity"),
        _as_float(planned.get("requested_quantity"), step_requested_quantity),
    )
    quantity = total("quantity")
    yes_filled_quantity = total("yes_filled_quantity")
    no_filled_quantity = total("no_filled_quantity")
    quantity_redeemed = total("quantity_redeemed")
    yes_cost = total("yes_cost")
    no_cost = total("no_cost")
    gross_cost = yes_cost + no_cost
    estimated_fees = total("estimated_fees")
    slippage_buffer = total("slippage_buffer")
    tax_cost = total("tax_cost")
    merge_cost = total("merge_cost")
    capital_used = gross_cost + estimated_fees + slippage_buffer + tax_cost + merge_cost
    net_profit = total("net_profit")
    net_return_bps = (net_profit / capital_used) * 10_000.0 if capital_used > EPSILON else 0.0
    weighted_slippage_bps = (
        (slippage_buffer / gross_cost) * 10_000.0 if gross_cost > EPSILON else _as_float(planned.get("effective_slippage_bps"))
    )

    details = dict(planned.get("details")) if isinstance(planned.get("details"), Mapping) else {}
    details["stepped_execution"] = {
        "step_count": len(steps),
        "max_step_count": max_step_count,
        "step_plan": dict(active.get("step_plan")) if isinstance(active.get("step_plan"), Mapping) else {},
        "steps": _step_summary_rows(steps),
    }

    simulation = dict(planned.get("simulation")) if isinstance(planned.get("simulation"), Mapping) else {}
    simulation["stepped_execution"] = {
        "active_execution_id": active.get("execution_id"),
        "step_count": len(steps),
        "completed_quantity": quantity,
        "target_quantity": _as_float(active.get("target_quantity"), _as_float(planned.get("quantity"))),
        "max_step_count": max_step_count,
        "step_plan": dict(active.get("step_plan")) if isinstance(active.get("step_plan"), Mapping) else {},
        "started_at_utc": active.get("started_at_utc"),
        "completed_at_utc": steps[-1].get("executed_at_utc"),
        "step_execution_ids": [step.get("execution_id") for step in steps],
    }

    planned.update(
        {
            "execution_id": str(active.get("execution_id") or planned.get("execution_id") or ""),
            "book_fingerprint": str(active.get("book_fingerprint") or planned.get("book_fingerprint") or ""),
            "executed_at_utc": steps[-1].get("executed_at_utc"),
            "execution_status": "full_pair_success" if quantity_redeemed >= requested_quantity - EPSILON else "paired_partial_success",
            "fill_status": "full_pair_success" if quantity_redeemed >= requested_quantity - EPSILON else "paired_partial_success",
            "requested_quantity": requested_quantity,
            "filled_pair_quantity": quantity_redeemed,
            "quantity": quantity,
            "quantity_redeemed": quantity_redeemed,
            "yes_filled_quantity": yes_filled_quantity,
            "no_filled_quantity": no_filled_quantity,
            "unmatched_yes_quantity": 0.0,
            "unmatched_no_quantity": 0.0,
            "yes_vwap": yes_cost / yes_filled_quantity if yes_filled_quantity > EPSILON else 0.0,
            "no_vwap": no_cost / no_filled_quantity if no_filled_quantity > EPSILON else 0.0,
            "yes_cost": yes_cost,
            "no_cost": no_cost,
            "gross_cost": gross_cost,
            "estimated_fees": estimated_fees,
            "slippage_buffer": slippage_buffer,
            "tax_cost": tax_cost,
            "merge_cost": merge_cost,
            "capital_used": capital_used,
            "redeemed_value": quantity_redeemed,
            "net_profit": net_profit,
            "net_return_bps": net_return_bps,
            "effective_slippage_bps": weighted_slippage_bps,
            "ceiling_used_usd": capital_used,
            "stop_reason": str(active.get("stop_reason") or planned.get("stop_reason") or "target_quantity_limit"),
            "cash_before": steps[0].get("cash_before"),
            "cash_after": steps[-1].get("cash_after"),
            "redeemed_cost_basis_usd": total("redeemed_cost_basis_usd"),
            "simulation": simulation,
            "details": details,
        }
    )
    preexisting_redeemed = total("preexisting_redeemed_value")
    preexisting_cost_basis = total("preexisting_redeemed_cost_basis_usd")
    if preexisting_redeemed > EPSILON:
        planned["preexisting_redeemed_value"] = preexisting_redeemed
        planned["preexisting_redeemed_cost_basis_usd"] = preexisting_cost_basis
    return planned


def _active_execution_max_step_count(active: Mapping[str, Any], *, params: PaperPortfolioParams) -> int:
    step_plan = active.get("step_plan")
    if isinstance(step_plan, Mapping):
        try:
            max_step_count = int(step_plan.get("max_step_count"))
        except (TypeError, ValueError):
            max_step_count = 0
        if max_step_count >= 1:
            return max_step_count
    return params.simulation.max_step_count


def _active_execution_completed(active: Mapping[str, Any], *, params: PaperPortfolioParams) -> tuple[bool, str | None]:
    steps = _normalize_step_history(active.get("steps"))
    if not steps:
        return False, None
    target_quantity = _as_float(active.get("target_quantity"))
    completed_quantity = _as_float(active.get("completed_quantity"))
    if target_quantity <= EPSILON:
        return True, "target_quantity_limit"
    if completed_quantity >= target_quantity - EPSILON:
        return True, str(active.get("stop_reason") or "target_quantity_limit")
    if len(steps) >= _active_execution_max_step_count(active, params=params):
        return True, "max_step_count"
    return False, None


def _execution_pair_is_valid(
    execution: Mapping[str, Any],
    *,
    market: BinaryMarket | None = None,
    params: PaperPortfolioParams,
) -> tuple[bool, str | None]:
    requested_quantity = _as_float(execution.get("requested_quantity"), _as_float(execution.get("quantity")))
    yes_quantity = _as_float(execution.get("yes_filled_quantity"), _as_float(execution.get("quantity")))
    no_quantity = _as_float(execution.get("no_filled_quantity"), _as_float(execution.get("quantity")))
    pair_quantity = _as_float(execution.get("filled_pair_quantity"), _as_float(execution.get("quantity_redeemed")))
    simulation = execution.get("simulation") if isinstance(execution.get("simulation"), Mapping) else {}
    partial_fill = simulation.get("partial_fill") if isinstance(simulation, Mapping) else None
    if isinstance(partial_fill, Mapping):
        raw_yes_quantity = _as_float(partial_fill.get("yes_filled_quantity"), yes_quantity)
        raw_no_quantity = _as_float(partial_fill.get("no_filled_quantity"), no_quantity)
        if raw_yes_quantity <= EPSILON and raw_no_quantity <= EPSILON:
            return False, "zero_fill"
        if abs(raw_yes_quantity - raw_no_quantity) > EPSILON:
            return False, "rejected_partial_pair"
    min_quantity = market.effective_min_order_size if market is not None else 0.0
    if yes_quantity <= EPSILON and no_quantity <= EPSILON:
        return False, "zero_fill"
    if abs(yes_quantity - no_quantity) > EPSILON:
        return False, "rejected_partial_pair"
    if pair_quantity <= EPSILON:
        return False, "zero_fill"
    if min_quantity > EPSILON and pair_quantity + EPSILON < min_quantity:
        return False, "rejected_partial_pair"
    net_profit = _as_float(execution.get("net_profit"))
    net_return_bps = _as_float(execution.get("net_return_bps"))
    threshold_failed, _thresholds = _profit_threshold_failure(
        net_profit=net_profit,
        net_return_bps=net_return_bps,
        params=params,
        simulation=execution.get("simulation") if isinstance(execution.get("simulation"), Mapping) else None,
        effective_slippage_bps=_as_float(execution.get("effective_slippage_bps")),
    )
    if threshold_failed:
        return False, "simulation_not_profitable_at_fill"
    if requested_quantity <= EPSILON:
        return False, "zero_fill"
    return True, None


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
    threshold_failed, thresholds = _profit_threshold_failure(
        net_profit=net_profit,
        net_return_bps=net_return_bps,
        params=params,
        state=state,
    )
    if threshold_failed:
        return PaperPortfolioDecision.skip(
            "not_profitable",
            market_id=market.market_id,
            quantity=quantity,
            gross_cost=gross_cost,
            capital_used=capital_used,
            net_profit=net_profit,
            net_return_bps=net_return_bps,
            profit_thresholds=thresholds,
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
        "execution_status": "full_pair_success",
        "fill_status": "full_pair_success",
        "requested_quantity": quantity,
        "filled_pair_quantity": quantity,
        "quantity": quantity,
        "quantity_redeemed": quantity,
        "yes_filled_quantity": quantity,
        "no_filled_quantity": quantity,
        "unmatched_yes_quantity": 0.0,
        "unmatched_no_quantity": 0.0,
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
    max_quantity: float | None = None,
    step_index: int | None = None,
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
    if step_index is not None:
        simulation_metadata["step_index"] = step_index

    target_quantity = _as_float(signal_execution.get("quantity"))
    if max_quantity is not None:
        target_quantity = min(target_quantity, max(0.0, max_quantity))

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

    return _execute_paper_step(
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
        last_exc: Exception | None = None
        data: Any = None
        delay = PAPER_STATE_READ_RETRY_INITIAL_SECONDS
        for attempt in range(1, PAPER_STATE_READ_RETRY_ATTEMPTS + 1):
            try:
                with self.path.open(encoding="utf-8") as f:
                    data = json.load(f)
                break
            except (OSError, json.JSONDecodeError) as exc:
                last_exc = exc
                if attempt >= PAPER_STATE_READ_RETRY_ATTEMPTS:
                    raise PaperPortfolioLoadError(f"failed to load paper portfolio {self.path}: {exc}") from exc
                if delay > 0.0:
                    time.sleep(delay)
                delay = min(
                    delay * 2.0 if delay > 0.0 else PAPER_STATE_READ_RETRY_INITIAL_SECONDS,
                    PAPER_STATE_READ_RETRY_MAX_SECONDS,
                )
        else:
            assert last_exc is not None
            raise PaperPortfolioLoadError(f"failed to load paper portfolio {self.path}: {last_exc}") from last_exc
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
        payload = jsonable(state)
        last_exc: OSError | None = None
        delay = PAPER_STATE_WRITE_RETRY_INITIAL_SECONDS
        for attempt in range(1, PAPER_STATE_WRITE_RETRY_ATTEMPTS + 1):
            try:
                with tmp.open("w", encoding="utf-8") as f:
                    json.dump(payload, f, indent=2, sort_keys=True)
                    f.flush()
                    os.fsync(f.fileno())
                tmp.replace(self.path)
                return
            except OSError as exc:
                last_exc = exc
                if attempt >= PAPER_STATE_WRITE_RETRY_ATTEMPTS:
                    raise
                if delay > 0.0:
                    time.sleep(delay)
                delay = min(
                    delay * 2.0 if delay > 0.0 else PAPER_STATE_WRITE_RETRY_INITIAL_SECONDS,
                    PAPER_STATE_WRITE_RETRY_MAX_SECONDS,
                )
        assert last_exc is not None
        raise last_exc

    def _reload_after_failed_save(self, fallback_state: Mapping[str, Any]) -> None:
        try:
            self.state = self._read_state() if self.path.exists() else deepcopy(dict(fallback_state))
        except PaperPortfolioLoadError:
            self.state = deepcopy(dict(fallback_state))

    def recover_completed_active_execution(self) -> dict[str, Any] | None:
        active = _active_execution_state(self.state)
        if active is None:
            return None
        completed, stop_reason = _active_execution_completed(active, params=self.params)
        if not completed:
            return None

        base_state = deepcopy(self.state)
        working_state = deepcopy(self.state)
        execution_id = str(active.get("execution_id") or "")
        already_recorded = False
        executions = working_state.setdefault("executions", [])
        if isinstance(executions, list):
            already_recorded = any(
                isinstance(row, Mapping) and str(row.get("execution_id") or "") == execution_id
                for row in executions
            )
        else:
            executions = []
            working_state["executions"] = executions

        recovery_active = deepcopy(active)
        if stop_reason is not None:
            recovery_active["stop_reason"] = stop_reason
        final_execution = _aggregate_step_executions(recovery_active, params=self.params)
        if not already_recorded:
            executions.append(deepcopy(final_execution))
            fingerprints = working_state.setdefault("book_fingerprints", {})
            if isinstance(fingerprints, dict):
                fingerprints[str(final_execution["market_id"])] = {
                    "fingerprint": final_execution["book_fingerprint"],
                    "execution_id": final_execution["execution_id"],
                    "executed_at_utc": final_execution["executed_at_utc"],
                }
            working_state["last_execution_at_utc"] = final_execution["executed_at_utc"]
        working_state.pop("active_execution", None)
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise
        self.state = working_state

        summary = {
            "execution_id": final_execution.get("execution_id"),
            "market_id": final_execution.get("market_id"),
            "book_fingerprint": final_execution.get("book_fingerprint"),
            "executed_at_utc": final_execution.get("executed_at_utc"),
            "step_count": len(_normalize_step_history(active.get("steps"))),
            "already_recorded": already_recorded,
        }
        try:
            self.append_event("paper_portfolio_execution_recovered", summary)
        except Exception as exc:
            LOGGER.warning(
                "paper_portfolio_execution_recovered_event_append_failed execution_id=%s error=%r",
                summary.get("execution_id"),
                exc,
            )
        return summary

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

    def _append_execution_failure_event(self, market: BinaryMarket, decision: PaperPortfolioDecision) -> None:
        simulation_details = decision.details.get("simulation")
        simulation_payload = dict(simulation_details) if isinstance(simulation_details, Mapping) else {}
        if self.state:
            failures = self.state.setdefault("simulation_failures", [])
            if isinstance(failures, list):
                failures.append(
                    {
                        "market_id": market.market_id,
                        "reason": decision.reason,
                        "book_fingerprint": decision.details.get("book_fingerprint"),
                        "timestamp_utc": utc_iso(),
                    }
                )
                del failures[:-100]
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
        execution_wins = sum(1 for execution in executions if _as_float(execution.get("net_profit")) > EPSILON)
        trade_count = len(executions)
        realized_executions = [
            execution
            for execution in executions
            if _as_float(execution.get("quantity_redeemed")) > EPSILON
            or abs(_as_float(execution.get("net_profit"))) > EPSILON
        ]
        realized_trade_count = len(realized_executions)
        realized_wins = sum(
            1 for execution in realized_executions if _as_float(execution.get("net_profit")) > EPSILON
        )
        starting_capital = _as_float(state.get("starting_capital_usd"), self.params.starting_capital_usd)
        cash = _as_float(state.get("cash"), starting_capital)
        open_position_value = _inventory_equity_value(state)
        equity = cash + open_position_value
        costs = state.get("costs") if isinstance(state.get("costs"), Mapping) else {}
        inventory = list(_inventory_rows(state).values())
        unmatched_inventory = _unmatched_inventory_rows(state)
        unmatched_risk = unmatched_inventory_risk_summary(state)
        capital_committed = sum(max(0.0, _as_float(row.get("cost_basis_usd"))) for row in inventory)
        active_trade_count = len(
            {
                market_id
                for row in inventory
                if (market_id := str(row.get("market_id") or ""))
            }
        )
        last_execution = executions[-1].get("executed_at_utc") if executions else None
        settlements = state.get("settlements") if isinstance(state.get("settlements"), list) else []
        last_settlement = settlements[-1].get("settled_at_utc") if settlements else None
        execution_win_rate = (execution_wins / trade_count) * 100.0 if trade_count else 0.0
        realized_win_rate = (realized_wins / realized_trade_count) * 100.0 if realized_trade_count else 0.0
        return {
            "starting_capital_usd": starting_capital,
            "cash": cash,
            "realized_pnl": _as_float(state.get("realized_pnl")),
            "total_equity": equity,
            "return_pct": ((equity - starting_capital) / starting_capital) * 100.0
            if starting_capital > 0
            else 0.0,
            "trade_count": trade_count,
            "win_rate_pct": execution_win_rate,
            "execution_win_rate_pct": execution_win_rate,
            "realized_win_rate_pct": realized_win_rate,
            "realized_trade_count": realized_trade_count,
            "capital_committed_usd": capital_committed,
            "open_position_value_usd": open_position_value,
            "active_trade_count": active_trade_count,
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
            "inventory": inventory,
            "unmatched_inventory": unmatched_inventory,
            **unmatched_risk,
        }

    def open_inventory_market_ids(self) -> set[str]:
        if not self.state:
            self.load()
        return _open_inventory_market_ids(self.state)

    def unmatched_inventory_market_ids(self) -> set[str]:
        if not self.state:
            self.load()
        return _unmatched_inventory_market_ids(self.state)

    def unmatched_inventory_event_ids(self) -> set[str]:
        if not self.state:
            self.load()
        return _unmatched_inventory_event_ids(self.state)

    def unmatched_inventory_risk_summary(self) -> dict[str, Any]:
        if not self.state:
            self.load()
        return unmatched_inventory_risk_summary(self.state)

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

    def complete_missing_leg_if_profitable(
        self,
        market: BinaryMarket,
        yes_asks: OrderBookSide,
        no_asks: OrderBookSide,
        *,
        as_of: datetime | None = None,
        params: PaperPortfolioParams | None = None,
    ) -> PaperPortfolioDecision:
        if not self.state:
            self.load()
        execution_params = params or self.params
        rows = _inventory_by_market_outcome(self.state, market.market_id)
        yes_row = rows.get("YES")
        no_row = rows.get("NO")
        yes_quantity = _as_float(yes_row.get("quantity")) if yes_row is not None else 0.0
        no_quantity = _as_float(no_row.get("quantity")) if no_row is not None else 0.0
        if abs(yes_quantity - no_quantity) <= EPSILON:
            return PaperPortfolioDecision.skip("no_unmatched_inventory", market_id=market.market_id)
        missing_outcome = "NO" if yes_quantity > no_quantity else "YES"
        missing_quantity = abs(yes_quantity - no_quantity)
        book = no_asks if missing_outcome == "NO" else yes_asks
        token_id = market.no_token_id if missing_outcome == "NO" else market.yes_token_id
        existing_row = yes_row if missing_outcome == "NO" else no_row
        if existing_row is None:
            return PaperPortfolioDecision.skip("missing_existing_leg_inventory", market_id=market.market_id)
        min_quantity = market.effective_min_order_size
        if missing_quantity + EPSILON < min_quantity:
            return PaperPortfolioDecision.skip(
                "inventory_management_below_min_size",
                market_id=market.market_id,
                missing_quantity=missing_quantity,
                min_quantity=min_quantity,
            )
        gross_cost = book.cost_to_fill(missing_quantity)
        if gross_cost is None:
            return PaperPortfolioDecision.skip(
                "inventory_management_insufficient_depth",
                market_id=market.market_id,
                missing_outcome=missing_outcome,
                missing_quantity=missing_quantity,
            )
        costs = _cost_breakdown(gross_cost, execution_params)
        capital_used = gross_cost + sum(costs.values())
        cash = _as_float(self.state.get("cash"), execution_params.starting_capital_usd)
        spendable_cash = max(0.0, cash - max(0.0, execution_params.min_cash_reserve_usd))
        if capital_used > spendable_cash + EPSILON:
            return PaperPortfolioDecision.skip(
                "cash_limit",
                market_id=market.market_id,
                required_capital=capital_used,
                spendable_cash=spendable_cash,
            )
        existing_cost_basis = _as_float(existing_row.get("cost_basis_usd"))
        existing_cost_for_pair = existing_cost_basis * (
            missing_quantity / _as_float(existing_row.get("quantity"))
            if _as_float(existing_row.get("quantity")) > EPSILON
            else 0.0
        )
        net_profit = missing_quantity - existing_cost_for_pair - capital_used
        net_return_bps = (
            (net_profit / (existing_cost_for_pair + capital_used)) * 10_000.0
            if existing_cost_for_pair + capital_used > EPSILON
            else 0.0
        )
        threshold_failed, thresholds = _profit_threshold_failure(
            net_profit=net_profit,
            net_return_bps=net_return_bps,
            params=execution_params,
            state=self.state,
        )
        if threshold_failed:
            return PaperPortfolioDecision.skip(
                "inventory_management_not_profitable",
                market_id=market.market_id,
                missing_outcome=missing_outcome,
                missing_quantity=missing_quantity,
                net_profit=net_profit,
                net_return_bps=net_return_bps,
                profit_thresholds=thresholds,
            )

        base_state = deepcopy(self.state)
        working_state = deepcopy(self.state)
        now = utc_iso(as_of or _utc_now())
        fingerprint = book_pair_fingerprint(
            market,
            yes_asks,
            no_asks,
            tranches=(
                {
                    "quantity": missing_quantity,
                    "yes_price": yes_asks.best_price or 0.0,
                    "no_price": no_asks.best_price or 0.0,
                    "unit_gross_cost": (yes_asks.best_price or 0.0) + (no_asks.best_price or 0.0),
                },
            ),
            step_plan={"inventory_management": "complete_missing_leg_if_profitable"},
        )
        execution_count = len(working_state.get("executions") or [])
        execution = {
            "execution_id": f"paper:{market.market_id}:{execution_count + 1}:{fingerprint[:12]}:inventory",
            "opportunity_id": f"inventory:{market.market_id}:{fingerprint[:12]}",
            "kind": "binary_complete_set",
            "mode": "paper_portfolio_instance",
            "execution_status": "inventory_management",
            "fill_status": "inventory_management",
            "market_id": market.market_id,
            "condition_id": market.condition_id,
            "event_id": market.event_id,
            "event_title": market.event_title,
            "question": market.question,
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "executed_at_utc": now,
            "book_fingerprint": fingerprint,
            "requested_quantity": missing_quantity,
            "filled_pair_quantity": missing_quantity,
            "quantity": missing_quantity,
            "quantity_redeemed": missing_quantity,
            "yes_filled_quantity": missing_quantity if missing_outcome == "YES" else 0.0,
            "no_filled_quantity": missing_quantity if missing_outcome == "NO" else 0.0,
            "unmatched_yes_quantity": 0.0,
            "unmatched_no_quantity": 0.0,
            "yes_vwap": gross_cost / missing_quantity if missing_outcome == "YES" else 0.0,
            "no_vwap": gross_cost / missing_quantity if missing_outcome == "NO" else 0.0,
            "yes_cost": gross_cost if missing_outcome == "YES" else 0.0,
            "no_cost": gross_cost if missing_outcome == "NO" else 0.0,
            "gross_cost": gross_cost,
            "estimated_fees": costs["fees_usd"],
            "slippage_buffer": costs["slippage_usd"],
            "tax_cost": costs["tax_usd"],
            "merge_cost": costs["merge_usd"],
            "capital_used": capital_used,
            "redeemed_value": missing_quantity,
            "net_profit": net_profit,
            "net_return_bps": net_return_bps,
            "trade_ceiling_usd": execution_params.trade_ceiling_usd,
            "ceiling_used_usd": capital_used,
            "stop_reason": "inventory_management",
            "source_timestamps": _book_timestamps(yes_asks, no_asks),
            "details": {
                "inventory_management": "complete_missing_leg_if_profitable",
                "missing_outcome": missing_outcome,
                "missing_token_id": token_id,
                "missing_quantity": missing_quantity,
                "existing_cost_basis_usd": existing_cost_for_pair,
                "profit_thresholds": thresholds,
            },
        }
        self._apply_execution_to_state(working_state, execution, params=execution_params)
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise

        self.state = working_state
        returned_execution = deepcopy(execution)
        try:
            self.append_event("paper_portfolio_execution", returned_execution)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            returned_execution["event_log_error"] = error
            LOGGER.warning(
                "paper_portfolio_execution_event_append_failed execution_id=%s error=%r",
                returned_execution.get("execution_id"),
                exc,
            )
        return PaperPortfolioDecision.execute(returned_execution)

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
        if execution_params.simulation.is_zero_friction:
            return self._execute_binary_complete_set_once(
                market,
                yes_asks,
                no_asks,
                as_of=as_of,
                params=execution_params,
                fill_time_book_reader=fill_time_book_reader,
            )

        active = _active_execution_state(self.state)
        if active is not None:
            if str(active.get("market_id") or "") != market.market_id:
                return PaperPortfolioDecision.skip(
                    "active_execution_in_progress",
                    market_id=market.market_id,
                    active_market_id=active.get("market_id"),
                    active_execution_id=active.get("execution_id"),
                )
            return self._continue_stepped_execution(active, market, params=execution_params)

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
                self._append_execution_failure_event(market, decision)
            return decision
        return self._start_stepped_execution(
            market,
            decision.execution,
            base_state=base_state,
            params=execution_params,
        )

    def _execute_binary_complete_set_once(
        self,
        market: BinaryMarket,
        yes_asks: OrderBookSide,
        no_asks: OrderBookSide,
        *,
        as_of: datetime | None = None,
        params: PaperPortfolioParams,
        fill_time_book_reader: FillTimeBookReader | None = None,
    ) -> PaperPortfolioDecision:
        base_state = deepcopy(self.state)
        decision = evaluate_binary_paper_execution(
            market,
            yes_asks,
            no_asks,
            state=base_state,
            params=params,
            as_of=as_of,
            fill_time_book_reader=fill_time_book_reader,
        )
        if decision.action != "EXECUTE" or decision.execution is None:
            if decision.details.get("simulation_failure"):
                self._append_execution_failure_event(market, decision)
            return decision

        working_state = deepcopy(base_state)
        execution = deepcopy(decision.execution)
        self._apply_execution_to_state(working_state, execution, params=params)
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

    def _active_execution_payload(
        self,
        market: BinaryMarket,
        planned_execution: Mapping[str, Any],
        *,
        params: PaperPortfolioParams,
    ) -> dict[str, Any]:
        execution_count = len(self.state.get("executions") or [])
        parent_fingerprint = str(planned_execution.get("book_fingerprint") or "")
        active_execution_id = f"paper:{market.market_id}:{execution_count + 1}:{parent_fingerprint[:12]}"
        now = utc_iso()
        return {
            "execution_id": active_execution_id,
            "market_id": market.market_id,
            "condition_id": market.condition_id,
            "yes_token_id": market.yes_token_id,
            "no_token_id": market.no_token_id,
            "book_fingerprint": parent_fingerprint,
            "requested_quantity": _as_float(
                planned_execution.get("requested_quantity"),
                _as_float(planned_execution.get("quantity")),
            ),
            "filled_pair_quantity": _as_float(
                planned_execution.get("filled_pair_quantity"),
                _as_float(planned_execution.get("quantity")),
            ),
            "target_quantity": _as_float(planned_execution.get("quantity")),
            "target_yes_filled_quantity": _as_float(
                planned_execution.get("yes_filled_quantity"),
                _as_float(planned_execution.get("quantity")),
            ),
            "target_no_filled_quantity": _as_float(
                planned_execution.get("no_filled_quantity"),
                _as_float(planned_execution.get("quantity")),
            ),
            "completed_quantity": 0.0,
            "completed_yes_filled_quantity": 0.0,
            "completed_no_filled_quantity": 0.0,
            "current_step_quantity": params.simulation.step_quantity_shares,
            "step_plan": _step_plan_payload(params.simulation),
            "planned_execution": deepcopy(dict(planned_execution)),
            "steps": [],
            "status": "active",
            "started_at_utc": now,
            "updated_at_utc": now,
        }

    def _start_stepped_execution(
        self,
        market: BinaryMarket,
        planned_execution: Mapping[str, Any],
        *,
        base_state: Mapping[str, Any],
        params: PaperPortfolioParams,
    ) -> PaperPortfolioDecision:
        working_state = deepcopy(dict(base_state))
        active = self._active_execution_payload(market, planned_execution, params=params)
        working_state["active_execution"] = active
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise
        self.state = working_state
        return self._continue_stepped_execution(active, market, params=params)

    def _next_step_quantities(
        self,
        active: Mapping[str, Any],
        *,
        params: PaperPortfolioParams,
    ) -> tuple[float, float, float]:
        target_quantity = _as_float(active.get("target_quantity"))
        completed_quantity = _as_float(active.get("completed_quantity"))
        remaining_quantity = max(0.0, target_quantity - completed_quantity)
        if remaining_quantity <= EPSILON:
            return 0.0, 0.0, 0.0
        current_step_quantity = _as_float(
            active.get("current_step_quantity"),
            params.simulation.step_quantity_shares,
        )
        step_quantity = _step_target_quantity(
            remaining_quantity=remaining_quantity,
            params=params,
            current_step_quantity=current_step_quantity,
        )
        target_yes = _as_float(active.get("target_yes_filled_quantity"), target_quantity)
        target_no = _as_float(active.get("target_no_filled_quantity"), target_quantity)
        yes_remaining = max(0.0, target_yes - _as_float(active.get("completed_yes_filled_quantity")))
        no_remaining = max(0.0, target_no - _as_float(active.get("completed_no_filled_quantity")))
        target_ratio = step_quantity / target_quantity if target_quantity > EPSILON else 0.0
        yes_fill = min(step_quantity, yes_remaining, max(0.0, target_yes * target_ratio))
        no_fill = min(step_quantity, no_remaining, max(0.0, target_no * target_ratio))
        if remaining_quantity - step_quantity <= EPSILON:
            yes_fill = min(step_quantity, yes_remaining)
            no_fill = min(step_quantity, no_remaining)
        return step_quantity, yes_fill, no_fill

    def _continue_stepped_execution(
        self,
        active: Mapping[str, Any],
        market: BinaryMarket,
        *,
        params: PaperPortfolioParams,
    ) -> PaperPortfolioDecision:
        current_active = deepcopy(dict(active))
        final_execution: dict[str, Any] | None = None
        step_limit_reached = False

        while True:
            steps = _normalize_step_history(current_active.get("steps"))
            completed_quantity = _as_float(current_active.get("completed_quantity"))
            target_quantity = _as_float(current_active.get("target_quantity"))
            if target_quantity <= EPSILON or completed_quantity >= target_quantity - EPSILON:
                final_execution = self._finalize_stepped_execution(current_active, params=params)
                break
            if len(steps) >= params.simulation.max_step_count:
                step_limit_reached = True
                final_execution = self._finalize_stepped_execution(
                    {**current_active, "stop_reason": "max_step_count"},
                    params=params,
                )
                break

            step_quantity, yes_fill, no_fill = self._next_step_quantities(current_active, params=params)
            if step_quantity <= EPSILON:
                final_execution = self._finalize_stepped_execution(
                    {**current_active, "stop_reason": "target_quantity_limit"},
                    params=params,
                )
                break
            planned_execution = current_active.get("planned_execution")
            if not isinstance(planned_execution, Mapping):
                return PaperPortfolioDecision.skip(
                    "active_execution_missing_plan",
                    market_id=market.market_id,
                    active_execution_id=current_active.get("execution_id"),
                )
            step_index = len(steps) + 1
            step_execution = _build_step_execution(
                planned_execution,
                active_execution_id=str(current_active.get("execution_id") or ""),
                step_index=step_index,
                offset_quantity=completed_quantity,
                step_quantity=step_quantity,
                yes_fill_quantity=yes_fill,
                no_fill_quantity=no_fill,
                params=params,
            )
            valid_step, invalid_reason = _execution_pair_is_valid(step_execution, market=market, params=params)
            if not valid_step:
                if steps:
                    final_execution = self._finalize_stepped_execution(
                        {**current_active, "stop_reason": invalid_reason or "rejected_partial_pair"},
                        params=params,
                    )
                    break
                return self._abort_stepped_execution(
                    current_active,
                    market,
                    reason=invalid_reason or "rejected_partial_pair",
                    step_execution=step_execution,
                )
            working_state = deepcopy(self.state)
            self._apply_execution_to_state(
                working_state,
                step_execution,
                params=params,
                record_execution=False,
                update_fingerprint=False,
            )
            next_steps = _normalize_step_history(current_active.get("steps"))
            next_steps.append(deepcopy(step_execution))
            next_active = deepcopy(dict(current_active))
            next_active.update(
                {
                    "steps": next_steps,
                    "completed_quantity": completed_quantity + step_quantity,
                    "completed_yes_filled_quantity": _as_float(current_active.get("completed_yes_filled_quantity")) + yes_fill,
                    "completed_no_filled_quantity": _as_float(current_active.get("completed_no_filled_quantity")) + no_fill,
                    "current_step_quantity": (
                        min(
                            max(0.0, target_quantity - completed_quantity - step_quantity),
                            step_quantity * 2.0,
                        )
                        if params.simulation.grow_step_size_after_success
                        else step_quantity
                    ),
                    "updated_at_utc": step_execution["executed_at_utc"],
                }
            )
            working_state["active_execution"] = next_active
            try:
                self._save_state(working_state)
            except Exception:
                self._reload_after_failed_save(self.state)
                raise
            self.state = working_state
            current_active = next_active

        assert final_execution is not None
        returned_execution = deepcopy(final_execution)
        details: dict[str, Any] = {}
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
        if step_limit_reached:
            details["step_limit_reached"] = True
        return PaperPortfolioDecision(
            action="EXECUTE",
            execution=returned_execution,
            details=details,
        )

    def _abort_stepped_execution(
        self,
        active: Mapping[str, Any],
        market: BinaryMarket,
        *,
        reason: str,
        step_execution: Mapping[str, Any],
    ) -> PaperPortfolioDecision:
        base_state = deepcopy(self.state)
        working_state = deepcopy(self.state)
        working_state.pop("active_execution", None)
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise
        self.state = working_state
        decision = PaperPortfolioDecision.skip(
            reason,
            market_id=market.market_id,
            active_execution_id=active.get("execution_id"),
            book_fingerprint=step_execution.get("book_fingerprint"),
            simulation=step_execution.get("simulation") if isinstance(step_execution.get("simulation"), Mapping) else {},
            simulation_failure=True,
        )
        self._append_execution_failure_event(market, decision)
        return decision

    def _finalize_stepped_execution(self, active: Mapping[str, Any], *, params: PaperPortfolioParams) -> dict[str, Any]:
        base_state = deepcopy(self.state)
        working_state = deepcopy(self.state)
        final_execution = _aggregate_step_executions(active, params=params)
        pair_quantity = _as_float(final_execution.get("filled_pair_quantity"), _as_float(final_execution.get("quantity_redeemed")))
        requested_quantity = _as_float(final_execution.get("requested_quantity"), _as_float(final_execution.get("quantity")))
        status = "full_pair_success" if pair_quantity >= requested_quantity - EPSILON else "paired_partial_success"
        final_execution["execution_status"] = status
        final_execution["fill_status"] = status
        final_execution["unmatched_yes_quantity"] = 0.0
        final_execution["unmatched_no_quantity"] = 0.0
        executions = working_state.setdefault("executions", [])
        if isinstance(executions, list):
            executions.append(deepcopy(final_execution))
        fingerprints = working_state.setdefault("book_fingerprints", {})
        if isinstance(fingerprints, dict):
            fingerprints[str(final_execution["market_id"])] = {
                "fingerprint": final_execution["book_fingerprint"],
                "execution_id": final_execution["execution_id"],
                "executed_at_utc": final_execution["executed_at_utc"],
            }
        working_state["last_execution_at_utc"] = final_execution["executed_at_utc"]
        working_state.pop("active_execution", None)
        try:
            self._save_state(working_state)
        except Exception:
            self._reload_after_failed_save(base_state)
            raise
        self.state = working_state
        return final_execution

    def _apply_execution(self, execution: dict[str, Any]) -> None:
        self._apply_execution_to_state(self.state, execution, params=self.params)

    def _apply_execution_to_state(
        self,
        state: dict[str, Any],
        execution: dict[str, Any],
        *,
        params: PaperPortfolioParams,
        record_execution: bool = True,
        update_fingerprint: bool = True,
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
            opened_by_execution_id=str(execution.get("execution_id") or ""),
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
            opened_by_execution_id=str(execution.get("execution_id") or ""),
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
        if record_execution:
            executions = state.setdefault("executions", [])
            if isinstance(executions, list):
                executions.append(execution)
        if update_fingerprint:
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
        opened_by_execution_id: str | None = None,
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
            opened_by_execution_id=opened_by_execution_id,
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
        opened_by_execution_id: str | None = None,
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
        row["inventory_status"] = row.get("inventory_status") or "open"
        row["unmatched_since_utc"] = row.get("unmatched_since_utc") or valued_at_utc
        row["opened_by_execution_id"] = row.get("opened_by_execution_id") or opened_by_execution_id
        row["last_management_action"] = row.get("last_management_action")
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
