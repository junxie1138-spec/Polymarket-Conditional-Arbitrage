from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from scipy.optimize import linprog

from . import config
from .arb_models import BinaryMarket, ConditionalArbOpportunity, OpportunityLeg, OrderBookSide

EPSILON = 1e-9


@dataclass(frozen=True)
class ArbStrategyParams:
    min_net_profit_usd: float
    min_net_return_bps: float
    max_capital_usd: float
    slippage_buffer_bps: float
    gas_cost_usd: float
    taker_fee_bps: float
    max_book_age_seconds: float

    @property
    def taker_fee_rate(self) -> float:
        return self.taker_fee_bps / 10_000.0

    @property
    def slippage_buffer_rate(self) -> float:
        return self.slippage_buffer_bps / 10_000.0

    @property
    def linear_cost_rate(self) -> float:
        return 1.0 + self.taker_fee_rate + self.slippage_buffer_rate

    @classmethod
    def from_config(cls, scan_config: config.ScanConfig | None = None) -> "ArbStrategyParams":
        loaded = scan_config or config.load_scan_config()
        return cls(
            min_net_profit_usd=loaded.min_net_profit_usd,
            min_net_return_bps=loaded.min_net_return_bps,
            max_capital_usd=loaded.max_capital_usd,
            slippage_buffer_bps=loaded.slippage_buffer_bps,
            gas_cost_usd=loaded.gas_cost_usd,
            taker_fee_bps=loaded.taker_fee_bps,
            max_book_age_seconds=loaded.max_book_age_seconds,
        )


@dataclass(frozen=True)
class ArbDecision:
    action: str
    reason: str | None = None
    opportunity: ConditionalArbOpportunity | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(cls, reason: str, **details: Any) -> "ArbDecision":
        return cls(action="SKIP", reason=reason, details=details)

    @classmethod
    def enter(cls, opportunity: ConditionalArbOpportunity) -> "ArbDecision":
        return cls(action="ENTER", opportunity=opportunity)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _stale_seconds(book: OrderBookSide, as_of: datetime) -> float | None:
    if book.updated_at is None:
        return None
    return (_ensure_aware(as_of) - _ensure_aware(book.updated_at)).total_seconds()


def _capital_limited_gross_cap(params: ArbStrategyParams) -> float:
    usable = params.max_capital_usd - params.gas_cost_usd
    if usable <= 0:
        return 0.0
    return usable / params.linear_cost_rate


def _profit_for(
    *,
    collateral_redeemed: float,
    gross_cost: float,
    params: ArbStrategyParams,
) -> tuple[float, float, float, float]:
    estimated_fees = gross_cost * params.taker_fee_rate
    slippage_buffer = gross_cost * params.slippage_buffer_rate
    net_profit = collateral_redeemed - gross_cost - estimated_fees - params.gas_cost_usd - slippage_buffer
    capital_at_risk = gross_cost + estimated_fees + params.gas_cost_usd + slippage_buffer
    net_return_bps = (net_profit / capital_at_risk) * 10_000.0 if capital_at_risk > 0 else 0.0
    return estimated_fees, slippage_buffer, net_profit, net_return_bps


def _position_key_matches_market(row: Mapping[str, Any], market: BinaryMarket) -> bool:
    row_market_id = str(row.get("market_id") or "")
    row_condition_id = str(row.get("condition_id") or "")
    row_yes_token_id = str(row.get("yes_token_id") or "")
    row_no_token_id = str(row.get("no_token_id") or "")
    return (
        row_market_id == market.market_id
        or bool(market.condition_id and row_condition_id == market.condition_id)
        or row_yes_token_id == market.yes_token_id
        or row_no_token_id == market.no_token_id
    )


def entered_binary_position_for_market(
    market: BinaryMarket,
    entered_positions: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[str, Any] | None:
    if not entered_positions:
        return None
    keys = {market.market_id}
    if market.condition_id:
        keys.add(market.condition_id)
    for key, row in entered_positions.items():
        if str(key) in keys:
            return row
        if _position_key_matches_market(row, market):
            return row
    return None


def _paired_depth_candidates(
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    max_gross_cost: float,
) -> list[tuple[float, float, float]]:
    candidates: list[tuple[float, float, float]] = []
    yes_index = 0
    no_index = 0
    yes_remaining = yes_asks.levels[0].size if yes_asks.levels else 0.0
    no_remaining = no_asks.levels[0].size if no_asks.levels else 0.0
    quantity = 0.0
    yes_cost = 0.0
    no_cost = 0.0

    while yes_index < len(yes_asks.levels) and no_index < len(no_asks.levels):
        gross_cost = yes_cost + no_cost
        remaining_gross_cap = max_gross_cost - gross_cost
        if remaining_gross_cap <= EPSILON:
            break

        yes_price = yes_asks.levels[yes_index].price
        no_price = no_asks.levels[no_index].price
        step_unit_cost = yes_price + no_price
        if step_unit_cost <= EPSILON:
            break

        step = min(yes_remaining, no_remaining, remaining_gross_cap / step_unit_cost)
        if step <= EPSILON:
            break

        yes_cost += step * yes_price
        no_cost += step * no_price
        quantity += step
        candidates.append((quantity, yes_cost, no_cost))

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

        if remaining_gross_cap - (step * step_unit_cost) <= EPSILON:
            break
    return candidates


def _passes_thresholds(
    *,
    net_profit: float,
    net_return_bps: float,
    params: ArbStrategyParams,
) -> bool:
    return (
        net_profit + EPSILON >= params.min_net_profit_usd
        and net_return_bps + EPSILON >= params.min_net_return_bps
    )


def evaluate_binary_arbitrage(
    market: BinaryMarket,
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    as_of: datetime | None = None,
    entered_positions: Mapping[str, Mapping[str, Any]] | None = None,
    params: ArbStrategyParams | None = None,
) -> ArbDecision:
    now = _ensure_aware(as_of or _utc_now())
    strategy_params = params or ArbStrategyParams.from_config()

    if not market.active or market.closed:
        return ArbDecision.skip("inactive_or_closed", market_id=market.market_id)
    if not market.accepting_orders or not market.enable_order_book:
        return ArbDecision.skip(
            "not_accepting_orders",
            market_id=market.market_id,
            accepting_orders=market.accepting_orders,
            enable_order_book=market.enable_order_book,
        )
    if len({market.yes_token_id, market.no_token_id}) != 2:
        return ArbDecision.skip("invalid_token_mapping", market_id=market.market_id)
    if entered_binary_position_for_market(market, entered_positions) is not None:
        return ArbDecision.skip("already_entered", market_id=market.market_id)
    if yes_asks.token_id != market.yes_token_id:
        return ArbDecision.skip("yes_book_token_mismatch", market_id=market.market_id)
    if no_asks.token_id != market.no_token_id:
        return ArbDecision.skip("no_book_token_mismatch", market_id=market.market_id)
    if yes_asks.side != "ask" or no_asks.side != "ask":
        return ArbDecision.skip("requires_ask_books", market_id=market.market_id)
    if not yes_asks.levels or not no_asks.levels:
        return ArbDecision.skip(
            "missing_two_sided_ask_liquidity",
            market_id=market.market_id,
            yes_levels=len(yes_asks.levels),
            no_levels=len(no_asks.levels),
        )

    for label, book in (("yes", yes_asks), ("no", no_asks)):
        age = _stale_seconds(book, now)
        if age is not None and (age < -EPSILON or age > strategy_params.max_book_age_seconds):
            return ArbDecision.skip(
                "stale_book",
                market_id=market.market_id,
                side=label,
                age_seconds=age,
                max_age_seconds=strategy_params.max_book_age_seconds,
            )

    max_gross_cost = _capital_limited_gross_cap(strategy_params)
    if max_gross_cost <= EPSILON:
        return ArbDecision.skip("invalid_capital_cap", market_id=market.market_id)

    min_quantity = max(float(market.min_order_size or 0.0), 0.0)
    candidates = _paired_depth_candidates(yes_asks, no_asks, max_gross_cost=max_gross_cost)
    if not candidates:
        return ArbDecision.skip("insufficient_depth", market_id=market.market_id)

    best_details: dict[str, Any] = {}
    selected: tuple[float, float, float, float, float, float, float] | None = None
    for quantity, yes_cost, no_cost in candidates:
        if quantity + EPSILON < min_quantity:
            continue
        gross_cost = yes_cost + no_cost
        fees, slippage, net_profit, net_return_bps = _profit_for(
            collateral_redeemed=quantity,
            gross_cost=gross_cost,
            params=strategy_params,
        )
        best_details = {
            "quantity": quantity,
            "gross_cost": gross_cost,
            "net_profit": net_profit,
            "net_return_bps": net_return_bps,
            "yes_vwap": yes_cost / quantity if quantity > 0 else None,
            "no_vwap": no_cost / quantity if quantity > 0 else None,
        }
        if _passes_thresholds(net_profit=net_profit, net_return_bps=net_return_bps, params=strategy_params):
            selected = (quantity, yes_cost, no_cost, gross_cost, fees, slippage, net_profit)

    if selected is None:
        max_quantity_seen = candidates[-1][0]
        if max_quantity_seen + EPSILON < min_quantity:
            return ArbDecision.skip(
                "insufficient_depth",
                market_id=market.market_id,
                available_equal_depth=max_quantity_seen,
                min_quantity=min_quantity,
            )
        return ArbDecision.skip(
            "not_profitable",
            market_id=market.market_id,
            min_net_profit_usd=strategy_params.min_net_profit_usd,
            min_net_return_bps=strategy_params.min_net_return_bps,
            **best_details,
        )

    quantity, yes_cost, no_cost, gross_cost, fees, slippage, net_profit = selected
    net_return_bps = (net_profit / (gross_cost + fees + strategy_params.gas_cost_usd + slippage)) * 10_000.0
    opportunity = ConditionalArbOpportunity(
        opportunity_id=f"binary:{market.market_id}",
        kind="binary_complete_set",
        event_id=market.event_id,
        event_title=market.event_title,
        markets=(market,),
        legs=(
            OpportunityLeg(
                market_id=market.market_id,
                condition_id=market.condition_id,
                token_id=market.yes_token_id,
                outcome="YES",
                quantity=quantity,
                vwap=yes_cost / quantity,
                cost=yes_cost,
            ),
            OpportunityLeg(
                market_id=market.market_id,
                condition_id=market.condition_id,
                token_id=market.no_token_id,
                outcome="NO",
                quantity=quantity,
                vwap=no_cost / quantity,
                cost=no_cost,
            ),
        ),
        collateral_redeemed=quantity,
        gross_cost=gross_cost,
        estimated_fees=fees,
        gas_cost=strategy_params.gas_cost_usd,
        slippage_buffer=slippage,
        net_profit=net_profit,
        net_return_bps=net_return_bps,
        source_timestamps={
            "yes_book": yes_asks.updated_at.isoformat() if yes_asks.updated_at else None,
            "no_book": no_asks.updated_at.isoformat() if no_asks.updated_at else None,
        },
        detected_at=now,
        details={
            "yes_best_ask": yes_asks.best_price,
            "no_best_ask": no_asks.best_price,
            "yes_source": yes_asks.source,
            "no_source": no_asks.source,
        },
    )
    return ArbDecision.enter(opportunity)


@dataclass(frozen=True)
class _LpVar:
    name: str
    kind: str
    market_index: int | None = None
    outcome: str | None = None
    price: float = 0.0


def _validate_neg_risk_group(markets: list[BinaryMarket]) -> tuple[str | None, str | None, ArbDecision | None]:
    if len(markets) < 2:
        return None, None, ArbDecision.skip("insufficient_neg_risk_group_size", markets=len(markets))
    event_id = markets[0].event_id
    if not event_id:
        return None, None, ArbDecision.skip("missing_grouping_metadata")
    if any(market.event_id != event_id for market in markets):
        return None, None, ArbDecision.skip("mixed_event_group")
    event_title = markets[0].event_title
    token_ids: list[str] = []
    for market in markets:
        if not market.is_tradable:
            return None, None, ArbDecision.skip("not_accepting_orders", market_id=market.market_id)
        token_ids.extend([market.yes_token_id, market.no_token_id])
    if len(set(token_ids)) != len(token_ids):
        return None, None, ArbDecision.skip("invalid_token_mapping", event_id=event_id)
    return event_id, event_title, None


def _check_group_books(
    markets: list[BinaryMarket],
    books_by_token: Mapping[str, OrderBookSide],
    now: datetime,
    params: ArbStrategyParams,
) -> ArbDecision | None:
    for market in markets:
        for outcome, token_id in (("YES", market.yes_token_id), ("NO", market.no_token_id)):
            book = books_by_token.get(token_id)
            if book is None:
                return ArbDecision.skip(
                    "missing_ask_book",
                    market_id=market.market_id,
                    token_id=token_id,
                    outcome=outcome,
                )
            if book.token_id != token_id or book.side != "ask":
                return ArbDecision.skip(
                    "book_token_mismatch",
                    market_id=market.market_id,
                    expected_token_id=token_id,
                    actual_token_id=book.token_id,
                    outcome=outcome,
                )
            age = _stale_seconds(book, now)
            if age is not None and (age < -EPSILON or age > params.max_book_age_seconds):
                return ArbDecision.skip(
                    "stale_book",
                    market_id=market.market_id,
                    token_id=token_id,
                    outcome=outcome,
                    age_seconds=age,
                    max_age_seconds=params.max_book_age_seconds,
                )
    return None


def evaluate_neg_risk_event_group(
    markets: list[BinaryMarket],
    books_by_token: Mapping[str, OrderBookSide],
    *,
    as_of: datetime | None = None,
    params: ArbStrategyParams | None = None,
) -> ArbDecision:
    now = _ensure_aware(as_of or _utc_now())
    strategy_params = params or ArbStrategyParams.from_config()
    event_id, event_title, validation_skip = _validate_neg_risk_group(markets)
    if validation_skip is not None:
        return validation_skip

    book_skip = _check_group_books(markets, books_by_token, now, strategy_params)
    if book_skip is not None:
        return book_skip

    assert event_id is not None
    n = len(markets)
    variables: list[_LpVar] = []
    bounds: list[tuple[float, float | None]] = []
    objective: list[float] = []
    capital_coeffs: list[float] = []
    yes_buy_vars: list[list[int]] = [[] for _ in markets]
    no_buy_vars: list[list[int]] = [[] for _ in markets]

    def add_var(var: _LpVar, *, lower: float = 0.0, upper: float | None = None, coeff: float = 0.0) -> int:
        index = len(variables)
        variables.append(var)
        bounds.append((lower, upper))
        objective.append(coeff)
        capital_coeffs.append(max(0.0, coeff) if var.kind in {"yes_buy", "no_buy"} else 0.0)
        return index

    for market_index, market in enumerate(markets):
        for level_index, level in enumerate(books_by_token[market.yes_token_id].levels):
            var_index = add_var(
                _LpVar(
                    name=f"yes_buy_{market_index}_{level_index}",
                    kind="yes_buy",
                    market_index=market_index,
                    outcome="YES",
                    price=level.price,
                ),
                upper=level.size,
                coeff=level.price * strategy_params.linear_cost_rate,
            )
            yes_buy_vars[market_index].append(var_index)
        for level_index, level in enumerate(books_by_token[market.no_token_id].levels):
            var_index = add_var(
                _LpVar(
                    name=f"no_buy_{market_index}_{level_index}",
                    kind="no_buy",
                    market_index=market_index,
                    outcome="NO",
                    price=level.price,
                ),
                upper=level.size,
                coeff=level.price * strategy_params.linear_cost_rate,
            )
            no_buy_vars[market_index].append(var_index)

    convert_vars = [
        add_var(_LpVar(name=f"convert_no_to_other_yes_{i}", kind="convert", market_index=i))
        for i in range(n)
    ]
    merge_vars = [
        add_var(_LpVar(name=f"same_condition_merge_{i}", kind="merge", market_index=i), coeff=-1.0)
        for i in range(n)
    ]
    redeem_var = add_var(_LpVar(name="complete_event_set_redemption", kind="redeem"), coeff=-1.0)
    leftover_yes_vars = [
        add_var(_LpVar(name=f"leftover_yes_{i}", kind="leftover_yes", market_index=i))
        for i in range(n)
    ]
    leftover_no_vars = [
        add_var(_LpVar(name=f"leftover_no_{i}", kind="leftover_no", market_index=i))
        for i in range(n)
    ]

    variable_count = len(variables)
    a_eq: list[list[float]] = []
    b_eq: list[float] = []
    for i in range(n):
        yes_balance = [0.0] * variable_count
        for var_index in yes_buy_vars[i]:
            yes_balance[var_index] = 1.0
        for other_i, convert_var in enumerate(convert_vars):
            if other_i != i:
                yes_balance[convert_var] = 1.0
        yes_balance[merge_vars[i]] = -1.0
        yes_balance[redeem_var] = -1.0
        yes_balance[leftover_yes_vars[i]] = -1.0
        a_eq.append(yes_balance)
        b_eq.append(0.0)

        no_balance = [0.0] * variable_count
        for var_index in no_buy_vars[i]:
            no_balance[var_index] = 1.0
        no_balance[convert_vars[i]] = -1.0
        no_balance[merge_vars[i]] = -1.0
        no_balance[leftover_no_vars[i]] = -1.0
        a_eq.append(no_balance)
        b_eq.append(0.0)

    a_ub: list[list[float]] = []
    b_ub: list[float] = []
    capital_cap_row = list(capital_coeffs)
    a_ub.append(capital_cap_row)
    b_ub.append(max(0.0, strategy_params.max_capital_usd - strategy_params.gas_cost_usd))

    min_profit_row = list(objective)
    a_ub.append(min_profit_row)
    b_ub.append(-(strategy_params.min_net_profit_usd + strategy_params.gas_cost_usd))

    min_return_rate = strategy_params.min_net_return_bps / 10_000.0
    min_return_row = [
        coeff * (1.0 + min_return_rate) if coeff > 0.0 else coeff
        for coeff in objective
    ]
    a_ub.append(min_return_row)
    b_ub.append(-(1.0 + min_return_rate) * strategy_params.gas_cost_usd)

    result = linprog(
        objective,
        A_ub=a_ub,
        b_ub=b_ub,
        A_eq=a_eq,
        b_eq=b_eq,
        bounds=bounds,
        method="highs",
    )
    if not result.success:
        return ArbDecision.skip(
            "not_profitable",
            event_id=event_id,
            solver_status=int(result.status),
            solver_message=str(result.message),
        )

    solution = result.x
    gross_cost = 0.0
    quantities_by_leg: dict[tuple[int, str], tuple[float, float]] = {}
    for index, var in enumerate(variables):
        if var.kind not in {"yes_buy", "no_buy"}:
            continue
        quantity = float(solution[index])
        if quantity <= EPSILON:
            continue
        assert var.market_index is not None and var.outcome is not None
        gross_cost += quantity * var.price
        key = (var.market_index, var.outcome)
        prev_quantity, prev_cost = quantities_by_leg.get(key, (0.0, 0.0))
        quantities_by_leg[key] = (prev_quantity + quantity, prev_cost + quantity * var.price)

    collateral_redeemed = float(solution[redeem_var]) + sum(float(solution[index]) for index in merge_vars)
    fees, slippage, net_profit, net_return_bps = _profit_for(
        collateral_redeemed=collateral_redeemed,
        gross_cost=gross_cost,
        params=strategy_params,
    )
    if collateral_redeemed <= EPSILON or not _passes_thresholds(
        net_profit=net_profit,
        net_return_bps=net_return_bps,
        params=strategy_params,
    ):
        return ArbDecision.skip(
            "not_profitable",
            event_id=event_id,
            collateral_redeemed=collateral_redeemed,
            gross_cost=gross_cost,
            net_profit=net_profit,
            net_return_bps=net_return_bps,
        )

    legs: list[OpportunityLeg] = []
    source_timestamps: dict[str, str | None] = {}
    for (market_index, outcome), (quantity, cost) in sorted(quantities_by_leg.items()):
        market = markets[market_index]
        token_id = market.yes_token_id if outcome == "YES" else market.no_token_id
        book = books_by_token[token_id]
        source_timestamps[token_id] = book.updated_at.isoformat() if book.updated_at else None
        legs.append(
            OpportunityLeg(
                market_id=market.market_id,
                condition_id=market.condition_id,
                token_id=token_id,
                outcome=outcome,  # type: ignore[arg-type]
                quantity=quantity,
                vwap=cost / quantity,
                cost=cost,
            )
        )

    conversions = [
        {
            "market_id": markets[i].market_id,
            "no_quantity_converted": float(solution[index]),
        }
        for i, index in enumerate(convert_vars)
        if float(solution[index]) > EPSILON
    ]
    merges = [
        {
            "market_id": markets[i].market_id,
            "quantity": float(solution[index]),
        }
        for i, index in enumerate(merge_vars)
        if float(solution[index]) > EPSILON
    ]
    opportunity = ConditionalArbOpportunity(
        opportunity_id=f"neg-risk:{event_id}",
        kind="neg_risk_event_set",
        event_id=event_id,
        event_title=event_title,
        markets=tuple(markets),
        legs=tuple(legs),
        collateral_redeemed=collateral_redeemed,
        gross_cost=gross_cost,
        estimated_fees=fees,
        gas_cost=strategy_params.gas_cost_usd,
        slippage_buffer=slippage,
        net_profit=net_profit,
        net_return_bps=net_return_bps,
        source_timestamps=source_timestamps,
        detected_at=now,
        details={
            "complete_event_set_redemption": float(solution[redeem_var]),
            "same_condition_merges": merges,
            "no_to_other_yes_conversions": conversions,
            "solver_objective": float(result.fun),
            "markets_in_group": len(markets),
        },
    )
    return ArbDecision.enter(opportunity)
