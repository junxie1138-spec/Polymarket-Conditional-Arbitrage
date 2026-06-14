from __future__ import annotations

import hashlib
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from . import config
from .arb_models import BinaryMarket, BookLevel, OrderBookSide
from .event_log import AppendOnlyJsonl, jsonable, utc_iso

EPSILON = 1e-9
SCHEMA_VERSION = 1
PORTFOLIO_SCHEMA_VERSION = 1
LOGGER = logging.getLogger(__name__)
FillTimeBookReader = Callable[[BinaryMarket, datetime], tuple[OrderBookSide | None, OrderBookSide | None]]


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
) -> dict[str, float]:
    return {
        "fees_usd": gross_cost * params.taker_fee_rate,
        "slippage_usd": gross_cost * params.slippage_buffer_rate,
        "tax_usd": gross_cost * params.tax_rate,
        "merge_usd": params.merge_cost_usd if merge_cost_usd is None else merge_cost_usd,
    }


def _inventory_rows(state: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    rows = state.get("inventory")
    if not isinstance(rows, dict):
        return {}
    return {
        str(token_id): dict(row)
        for token_id, row in rows.items()
        if isinstance(row, Mapping) and _as_float(row.get("quantity")) > EPSILON
    }


def _redeemable_inventory_value(state: Mapping[str, Any]) -> float:
    inventory = _inventory_rows(state)
    by_market: dict[str, dict[str, float]] = {}
    for row in inventory.values():
        market_id = str(row.get("market_id") or "")
        outcome = str(row.get("outcome") or "").upper()
        if outcome not in {"YES", "NO"}:
            continue
        by_market.setdefault(market_id, {"YES": 0.0, "NO": 0.0})[outcome] += _as_float(row.get("quantity"))
    return sum(min(row["YES"], row["NO"]) for row in by_market.values())


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
    state["total_equity"] = state["cash"] + _redeemable_inventory_value(state)
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
    jitter = 0.0
    if simulation.latency_jitter_ms > 0.0:
        jitter_draw = _stage_random(simulation, market, book_fingerprint, "latency_jitter")
        jitter = (jitter_draw - 0.5) * 2.0 * simulation.latency_jitter_ms
    submit_latency_ms = max(0.0, simulation.latency_ms + jitter)
    signing_latency_ms = max(0.0, simulation.signing_latency_ms)
    settlement_latency_ms = max(0.0, simulation.settlement_latency_ms)
    fill_latency_ms = submit_latency_ms + signing_latency_ms
    fill_time = signal_time + timedelta(milliseconds=fill_latency_ms)
    settlement_time = fill_time + timedelta(milliseconds=settlement_latency_ms)
    return fill_time, {
        "seed": simulation.seed,
        "enabled": simulation.enabled,
        "signal_timestamp_utc": utc_iso(signal_time),
        "submit_latency_ms": submit_latency_ms,
        "latency_jitter_ms": jitter,
        "signing_latency_ms": signing_latency_ms,
        "fill_latency_ms": fill_latency_ms,
        "settlement_latency_ms": settlement_latency_ms,
        "fill_timestamp_utc": utc_iso(fill_time),
        "settlement_timestamp_utc": utc_iso(settlement_time),
    }


def _book_timestamps(yes_asks: OrderBookSide, no_asks: OrderBookSide) -> dict[str, str | None]:
    return {
        "yes_book": utc_iso(yes_asks.updated_at) if yes_asks.updated_at else None,
        "no_book": utc_iso(no_asks.updated_at) if no_asks.updated_at else None,
    }


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

    full_fill_costs = _cost_breakdown(gross_cost, params)
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
    yes_filled_quantity = quantity
    no_filled_quantity = quantity
    if (
        params.simulation.partial_fill_probability > 0.0
        and _stage_random(params.simulation, market, fingerprint, "partial_fill") <= params.simulation.partial_fill_probability
    ):
        partial_applied = True
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
    costs = _cost_breakdown(gross_cost, params, merge_cost_usd=merge_cost)
    capital_used = gross_cost + sum(costs.values())
    redeemed_value = matched_quantity
    net_profit = redeemed_value - capital_used
    net_return_bps = (net_profit / capital_used) * 10_000.0 if capital_used > 0 else 0.0

    simulation_metadata["partial_fill"] = {
        "applied": partial_applied,
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

    throttle_saturated = False
    target_quantity = _as_float(signal_execution.get("quantity"))
    if simulation.throttle_max_submissions_per_second > 0:
        throttle_draw = _stage_random(simulation, market, signal_fingerprint, "throttle")
        throttle_probability = min(1.0, 1.0 / max(1, simulation.throttle_max_submissions_per_second))
        throttle_saturated = simulation.throttle_max_submissions_per_second <= 1 or throttle_draw <= throttle_probability
        simulation_metadata["throttle"] = {
            "saturated": throttle_saturated,
            "draw": throttle_draw,
            "saturation_probability": throttle_probability,
            "max_submissions_per_second": simulation.throttle_max_submissions_per_second,
            "quantity_ratio": simulation.throttle_quantity_ratio,
        }
        if throttle_saturated:
            target_quantity *= max(0.0, min(1.0, simulation.throttle_quantity_ratio))
            if target_quantity + EPSILON < market.effective_min_order_size:
                return _simulation_failure_decision(
                    "simulation_throttle_min_size",
                    market=market,
                    simulation=simulation_metadata,
                    book_fingerprint=signal_fingerprint,
                    degraded_quantity=target_quantity,
                    min_quantity=market.effective_min_order_size,
                )
    else:
        simulation_metadata["throttle"] = {
            "saturated": False,
            "max_submissions_per_second": simulation.throttle_max_submissions_per_second,
            "quantity_ratio": simulation.throttle_quantity_ratio,
        }

    queued_signal_yes, queued_signal_no = _apply_simulated_book_friction(
        market,
        yes_asks,
        no_asks,
        simulation=simulation,
        signal_fingerprint=signal_fingerprint,
        metadata=simulation_metadata,
    )
    queued_signal_quantity, _, _, _, _ = _simulate_paired_tranches(
        queued_signal_yes,
        queued_signal_no,
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

    for stage, probability in (
        ("submit_failure", simulation.submit_failure_probability),
        ("accept_failure", simulation.accept_failure_probability),
    ):
        if _simulation_failure_triggered(
            simulation,
            market,
            signal_fingerprint,
            stage=stage,
            probability=probability,
        ):
            return _simulation_failure_decision(
                f"simulation_{stage}",
                market=market,
                simulation=simulation_metadata,
                book_fingerprint=signal_fingerprint,
            )

    if fill_time_book_reader is not None:
        fill_yes, fill_no = fill_time_book_reader(market, fill_time)
        simulation_metadata["book_source"] = "fill_time_cache"
        if fill_yes is None or fill_no is None:
            return _simulation_failure_decision(
                "simulation_missing_fill_time_book",
                market=market,
                simulation=simulation_metadata,
                book_fingerprint=signal_fingerprint,
                yes_book_present=fill_yes is not None,
                no_book_present=fill_no is not None,
            )
    else:
        fill_yes, fill_no = yes_asks, no_asks
        simulation_metadata["book_source"] = "signal_time_adjusted"

    fill_yes, fill_no = _apply_simulated_book_friction(
        market,
        fill_yes,
        fill_no,
        simulation=simulation,
        signal_fingerprint=signal_fingerprint,
        metadata=simulation_metadata,
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

    if _simulation_failure_triggered(
        simulation,
        market,
        signal_fingerprint,
        stage="fill_failure",
        probability=simulation.fill_failure_probability,
    ):
        cancel_failed = _simulation_failure_triggered(
            simulation,
            market,
            signal_fingerprint,
            stage="cancel_failure",
            probability=simulation.cancel_failure_probability,
        )
        return _simulation_failure_decision(
            "simulation_cancel_failure" if cancel_failed else "simulation_fill_failure",
            market=market,
            simulation=simulation_metadata,
            book_fingerprint=signal_fingerprint,
        )

    return _finalize_execution_from_books(
        market,
        fill_yes,
        fill_no,
        state=state,
        params=params,
        as_of=fill_time,
        max_quantity=target_quantity,
        simulation_metadata=simulation_metadata,
        signal_execution=signal_execution,
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
        state["total_equity"] = state["cash"] + _redeemable_inventory_value(state)
        metadata = state.setdefault("metadata", {})
        if isinstance(metadata, dict):
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
        equity = cash + _redeemable_inventory_value(state)
        costs = state.get("costs") if isinstance(state.get("costs"), Mapping) else {}
        inventory = list(_inventory_rows(state).values())
        last_execution = executions[-1].get("executed_at_utc") if executions else None
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
            "unmatched_inventory": inventory,
        }

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
        preexisting_redeemed = self._redeem_completed_pairs_from_state(
            state,
            market_id=str(execution["market_id"]),
            yes_token_id=str(execution["yes_token_id"]),
            no_token_id=str(execution["no_token_id"]),
        )
        if preexisting_redeemed > EPSILON:
            state["cash"] = _as_float(state.get("cash"), params.starting_capital_usd) + preexisting_redeemed
            normalizations = state.setdefault("inventory_normalizations", [])
            if isinstance(normalizations, list):
                normalizations.append(
                    {
                        "market_id": execution["market_id"],
                        "yes_token_id": execution["yes_token_id"],
                        "no_token_id": execution["no_token_id"],
                        "redeemed_value": preexisting_redeemed,
                        "normalized_before_execution_id": execution["execution_id"],
                        "normalized_at_utc": execution["executed_at_utc"],
                    }
                )

        cash_before = _as_float(state.get("cash"), params.starting_capital_usd)
        capital_used = _as_float(execution.get("capital_used"))
        state["cash"] = cash_before - capital_used

        yes_quantity = _as_float(execution.get("yes_filled_quantity"), _as_float(execution.get("quantity")))
        no_quantity = _as_float(execution.get("no_filled_quantity"), _as_float(execution.get("quantity")))
        self._add_inventory_to_state(
            state,
            token_id=str(execution["yes_token_id"]),
            market_id=str(execution["market_id"]),
            condition_id=execution.get("condition_id"),
            outcome="YES",
            quantity=yes_quantity,
        )
        self._add_inventory_to_state(
            state,
            token_id=str(execution["no_token_id"]),
            market_id=str(execution["market_id"]),
            condition_id=execution.get("condition_id"),
            outcome="NO",
            quantity=no_quantity,
        )
        redeemed = self._redeem_completed_pairs_from_state(
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
        execution["quantity_redeemed"] = redeemed
        execution["net_profit"] = cash_after - cash_before
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
        state["total_equity"] = state["cash"] + _redeemable_inventory_value(state)

    def _add_inventory(
        self,
        *,
        token_id: str,
        market_id: str,
        condition_id: str | None,
        outcome: str,
        quantity: float,
    ) -> None:
        self._add_inventory_to_state(
            self.state,
            token_id=token_id,
            market_id=market_id,
            condition_id=condition_id,
            outcome=outcome,
            quantity=quantity,
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
    ) -> None:
        inventory = state.setdefault("inventory", {})
        if not isinstance(inventory, dict):
            inventory = {}
            state["inventory"] = inventory
        row = dict(inventory.get(token_id) or {})
        row.update(
            {
                "token_id": token_id,
                "market_id": market_id,
                "condition_id": condition_id,
                "outcome": outcome,
            }
        )
        row["quantity"] = _as_float(row.get("quantity")) + quantity
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
        inventory = state.get("inventory")
        if not isinstance(inventory, dict):
            return 0.0
        yes_row = inventory.get(yes_token_id)
        no_row = inventory.get(no_token_id)
        if not isinstance(yes_row, dict) or not isinstance(no_row, dict):
            return 0.0
        if str(yes_row.get("market_id")) != market_id or str(no_row.get("market_id")) != market_id:
            return 0.0
        redeem_quantity = min(_as_float(yes_row.get("quantity")), _as_float(no_row.get("quantity")))
        if redeem_quantity <= EPSILON:
            return 0.0
        yes_row["quantity"] = _as_float(yes_row.get("quantity")) - redeem_quantity
        no_row["quantity"] = _as_float(no_row.get("quantity")) - redeem_quantity
        if yes_row["quantity"] <= EPSILON:
            inventory.pop(yes_token_id, None)
        if no_row["quantity"] <= EPSILON:
            inventory.pop(no_token_id, None)
        return redeem_quantity
