from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config
from .arb_models import BinaryMarket, OrderBookSide
from .event_log import AppendOnlyJsonl, jsonable, utc_iso

EPSILON = 1e-9
SCHEMA_VERSION = 1
PORTFOLIO_SCHEMA_VERSION = 1


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


def _book_fingerprint_payload(book: OrderBookSide) -> dict[str, Any]:
    return {
        "token_id": book.token_id,
        "side": book.side,
        "source": book.source,
        "updated_at": utc_iso(book.updated_at) if book.updated_at else None,
        "levels": [
            {
                "price": round(float(level.price), 12),
                "size": round(float(level.size), 12),
            }
            for level in book.levels
        ],
    }


def book_pair_fingerprint(market: BinaryMarket, yes_asks: OrderBookSide, no_asks: OrderBookSide) -> str:
    payload = {
        "market_id": market.market_id,
        "condition_id": market.condition_id,
        "yes": _book_fingerprint_payload(yes_asks),
        "no": _book_fingerprint_payload(no_asks),
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _cost_breakdown(gross_cost: float, params: PaperPortfolioParams) -> dict[str, float]:
    return {
        "fees_usd": gross_cost * params.taker_fee_rate,
        "slippage_usd": gross_cost * params.slippage_buffer_rate,
        "tax_usd": gross_cost * params.tax_rate,
        "merge_usd": params.merge_cost_usd,
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
        step = min(available_equal_depth, remaining_budget / unit_capital_used)
        if step <= EPSILON:
            stop_reason = "cash_or_ceiling_limit"
            break

        budget_limited = step + EPSILON < available_equal_depth
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


def evaluate_binary_paper_execution(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    state: Mapping[str, Any],
    params: PaperPortfolioParams,
    as_of: datetime | None = None,
) -> PaperPortfolioDecision:
    now = _ensure_aware(as_of or _utc_now())
    fingerprint = book_pair_fingerprint(market, yes_asks, no_asks)

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

    if _known_fingerprint(state, market.market_id) == fingerprint:
        return PaperPortfolioDecision.skip(
            "unchanged_book_snapshot",
            market_id=market.market_id,
            book_fingerprint=fingerprint,
        )

    cash = _as_float(state.get("cash"), params.starting_capital_usd)
    quantity, yes_cost, no_cost, tranches, stop_reason = _simulate_paired_tranches(
        yes_asks,
        no_asks,
        cash=cash,
        params=params,
    )
    min_quantity = max(_as_float(market.min_order_size), 0.0)
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
        self.state["total_equity"] = self.state["cash"] + _redeemable_inventory_value(self.state)
        metadata = self.state.setdefault("metadata", {})
        if isinstance(metadata, dict):
            metadata["updated_at_utc"] = utc_iso()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(jsonable(self.state), f, indent=2, sort_keys=True)
        tmp.replace(self.path)

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
    ) -> PaperPortfolioDecision:
        if not self.state:
            self.load()
        execution_params = params or self.params
        decision = evaluate_binary_paper_execution(
            market,
            yes_asks,
            no_asks,
            state=self.state,
            params=execution_params,
            as_of=as_of,
        )
        if decision.action != "EXECUTE" or decision.execution is None:
            return decision

        self._apply_execution(decision.execution)
        self.save()
        self.append_event("paper_portfolio_execution", decision.execution)
        return decision

    def _apply_execution(self, execution: dict[str, Any]) -> None:
        cash_before = _as_float(self.state.get("cash"), self.params.starting_capital_usd)
        capital_used = _as_float(execution.get("capital_used"))
        self.state["cash"] = cash_before - capital_used

        quantity = _as_float(execution.get("quantity"))
        self._add_inventory(
            token_id=str(execution["yes_token_id"]),
            market_id=str(execution["market_id"]),
            condition_id=execution.get("condition_id"),
            outcome="YES",
            quantity=quantity,
        )
        self._add_inventory(
            token_id=str(execution["no_token_id"]),
            market_id=str(execution["market_id"]),
            condition_id=execution.get("condition_id"),
            outcome="NO",
            quantity=quantity,
        )
        redeemed = self._redeem_completed_pairs(
            market_id=str(execution["market_id"]),
            yes_token_id=str(execution["yes_token_id"]),
            no_token_id=str(execution["no_token_id"]),
        )
        self.state["cash"] += redeemed
        cash_after = _as_float(self.state.get("cash"))

        execution["cash_before"] = cash_before
        execution["cash_after"] = cash_after
        execution["quantity_redeemed"] = redeemed
        execution["net_profit"] = cash_after - cash_before
        costs = self.state.setdefault("costs", {})
        if isinstance(costs, dict):
            costs["fees_usd"] = _as_float(costs.get("fees_usd")) + _as_float(execution.get("estimated_fees"))
            costs["slippage_usd"] = _as_float(costs.get("slippage_usd")) + _as_float(
                execution.get("slippage_buffer")
            )
            costs["tax_usd"] = _as_float(costs.get("tax_usd")) + _as_float(execution.get("tax_cost"))
            costs["merge_usd"] = _as_float(costs.get("merge_usd")) + _as_float(execution.get("merge_cost"))
        self.state["realized_pnl"] = _as_float(self.state.get("realized_pnl")) + _as_float(
            execution.get("net_profit")
        )
        executions = self.state.setdefault("executions", [])
        if isinstance(executions, list):
            executions.append(execution)
        fingerprints = self.state.setdefault("book_fingerprints", {})
        if isinstance(fingerprints, dict):
            fingerprints[str(execution["market_id"])] = {
                "fingerprint": execution["book_fingerprint"],
                "execution_id": execution["execution_id"],
                "executed_at_utc": execution["executed_at_utc"],
            }
        self.state["last_execution_at_utc"] = execution["executed_at_utc"]
        self.state["total_equity"] = self.state["cash"] + _redeemable_inventory_value(self.state)

    def _add_inventory(
        self,
        *,
        token_id: str,
        market_id: str,
        condition_id: str | None,
        outcome: str,
        quantity: float,
    ) -> None:
        inventory = self.state.setdefault("inventory", {})
        if not isinstance(inventory, dict):
            inventory = {}
            self.state["inventory"] = inventory
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
        inventory = self.state.get("inventory")
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
