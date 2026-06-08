from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

from . import config
from .arb_models import ArbOpportunity, BinaryMarket, OrderBookSide


@dataclass(frozen=True)
class ArbStrategyParams:
    min_net_profit_usd: float
    min_net_return_bps: float
    max_paper_position_usd: float
    slippage_buffer_bps: float
    gas_cost_usd: float
    taker_fee_bps: float
    max_book_age_seconds: float

    @classmethod
    def from_config(cls) -> "ArbStrategyParams":
        return cls(
            min_net_profit_usd=config.min_net_profit_usd(),
            min_net_return_bps=config.min_net_return_bps(),
            max_paper_position_usd=config.max_paper_position_usd(),
            slippage_buffer_bps=config.slippage_buffer_bps(),
            gas_cost_usd=config.gas_cost_usd(),
            taker_fee_bps=config.taker_fee_bps(),
            max_book_age_seconds=config.merge_arb_max_book_age_seconds(),
        )


@dataclass(frozen=True)
class ArbDecision:
    action: str
    reason: str | None = None
    opportunity: ArbOpportunity | None = None
    details: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def skip(cls, reason: str, **details: Any) -> "ArbDecision":
        return cls(action="SKIP", reason=reason, details=details)

    @classmethod
    def enter(cls, opportunity: ArbOpportunity) -> "ArbDecision":
        return cls(action="ENTER", opportunity=opportunity)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


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


def _stale_seconds(book: OrderBookSide, as_of: datetime) -> float | None:
    if book.updated_at is None:
        return None
    return (_ensure_aware(as_of) - _ensure_aware(book.updated_at)).total_seconds()


def _profit_for(
    *,
    quantity: float,
    yes_cost: float,
    no_cost: float,
    params: ArbStrategyParams,
) -> tuple[float, float, float, float, float]:
    gross_cost = yes_cost + no_cost
    estimated_fees = gross_cost * (params.taker_fee_bps / 10_000.0)
    slippage_buffer = gross_cost * (params.slippage_buffer_bps / 10_000.0)
    net_profit = quantity - gross_cost - estimated_fees - params.gas_cost_usd - slippage_buffer
    capital_at_risk = gross_cost + estimated_fees + params.gas_cost_usd + slippage_buffer
    net_return_bps = (net_profit / capital_at_risk) * 10_000.0 if capital_at_risk > 0 else 0.0
    return gross_cost, estimated_fees, slippage_buffer, net_profit, net_return_bps


def _paired_depth_candidates(
    yes_asks: OrderBookSide,
    no_asks: OrderBookSide,
    *,
    max_quantity: float,
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
        remaining_cap = max_quantity - quantity
        if remaining_cap <= 1e-12:
            break
        step = min(yes_remaining, no_remaining, remaining_cap)
        if step <= 0.0:
            break

        yes_price = yes_asks.levels[yes_index].price
        no_price = no_asks.levels[no_index].price
        yes_cost += step * yes_price
        no_cost += step * no_price
        quantity += step
        candidates.append((quantity, yes_cost, no_cost))

        yes_remaining -= step
        no_remaining -= step
        if yes_remaining <= 1e-12:
            yes_index += 1
            if yes_index < len(yes_asks.levels):
                yes_remaining = yes_asks.levels[yes_index].size
        if no_remaining <= 1e-12:
            no_index += 1
            if no_index < len(no_asks.levels):
                no_remaining = no_asks.levels[no_index].size
    return candidates


def evaluate_binary_merge_arbitrage(
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
        if age is not None and (age < -1e-6 or age > strategy_params.max_book_age_seconds):
            return ArbDecision.skip(
                "stale_book",
                market_id=market.market_id,
                side=label,
                age_seconds=age,
                max_age_seconds=strategy_params.max_book_age_seconds,
            )

    max_quantity = max(0.0, strategy_params.max_paper_position_usd)
    if max_quantity <= 0.0:
        return ArbDecision.skip("invalid_position_cap", market_id=market.market_id)

    min_quantity = max(float(market.min_order_size or 0.0), 0.0)
    candidates = _paired_depth_candidates(yes_asks, no_asks, max_quantity=max_quantity)
    if not candidates:
        return ArbDecision.skip("insufficient_depth", market_id=market.market_id)

    best_details: dict[str, Any] = {}
    selected: tuple[float, float, float, float, float, float, float, float] | None = None
    for quantity, yes_cost, no_cost in candidates:
        if quantity + 1e-12 < min_quantity:
            continue
        gross_cost, fees, slippage, net_profit, net_return_bps = _profit_for(
            quantity=quantity,
            yes_cost=yes_cost,
            no_cost=no_cost,
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
        if (
            net_profit + 1e-12 >= strategy_params.min_net_profit_usd
            and net_return_bps + 1e-12 >= strategy_params.min_net_return_bps
        ):
            selected = (quantity, yes_cost, no_cost, gross_cost, fees, slippage, net_profit, net_return_bps)

    if selected is None:
        max_quantity_seen = candidates[-1][0]
        if max_quantity_seen + 1e-12 < min_quantity:
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

    quantity, yes_cost, no_cost, gross_cost, fees, slippage, net_profit, net_return_bps = selected
    opportunity = ArbOpportunity(
        market=market,
        executable_size=quantity,
        yes_vwap=yes_cost / quantity,
        no_vwap=no_cost / quantity,
        yes_cost=yes_cost,
        no_cost=no_cost,
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
