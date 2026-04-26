from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable, Literal, Mapping

from . import config
from .forecast import estimate_forecast_prob
from .market_parser import _parse_end_date, parse_market_question


@dataclass(frozen=True)
class TradePlan:
    market_id: str
    token_id: str
    side: str
    question: str
    city: str
    target_date: str
    market_price: float
    entry_price: float
    shares: float
    position_usd: float
    forecast_prob: float
    edge: float
    lead_days: int
    entry_time: datetime
    condition_id: str | None = None
    bracket_low: float | None = None
    bracket_high: float | None = None
    bracket_unit: str | None = None
    metric: str | None = None


@dataclass(frozen=True)
class Decision:
    action: str
    reason: str | None = None
    plan: TradePlan | None = None
    details: dict = field(default_factory=dict)

    @classmethod
    def skip(cls, reason: str, **details) -> "Decision":
        return cls(action="SKIP", reason=reason, details=details)

    @classmethod
    def enter(cls, plan: TradePlan) -> "Decision":
        return cls(action="ENTER", plan=plan)


ForecastProbabilityFn = Callable[..., float | None]
ContractSide = Literal["YES", "NO"]


def token_ids_from_market(market: dict) -> list[str]:
    token_ids = market.get("clobTokenIds")
    if isinstance(token_ids, str):
        try:
            token_ids = json.loads(token_ids)
        except Exception:
            return []
    if isinstance(token_ids, list):
        return [str(token_id) for token_id in token_ids]
    return []


def entered_position_for_market(market: dict, entered_positions: Mapping[str, dict] | None) -> dict | None:
    if not entered_positions:
        return None

    market_id = str(market.get("id") or market.get("conditionId") or "")
    condition_id = str(market.get("conditionId") or "")
    market_keys = {value for value in (market_id, condition_id) if value}
    market_tokens = set(token_ids_from_market(market))

    for key, row in entered_positions.items():
        row_market_id = str(row.get("market_id") or "")
        row_condition_id = str(row.get("condition_id") or "")
        row_token_id = str(row.get("token_id") or "")
        if str(key) in market_keys:
            return row
        if row_market_id and row_market_id in market_keys:
            return row
        if row_condition_id and row_condition_id in market_keys:
            return row
        if row_token_id and row_token_id in market_tokens:
            return row
    return None


def yes_token_from_market(market: dict) -> str | None:
    token_ids = token_ids_from_market(market)
    if token_ids:
        return token_ids[0]
    return None


def no_token_from_market(market: dict) -> str | None:
    token_ids = token_ids_from_market(market)
    if len(token_ids) >= 2:
        return token_ids[1]
    return None


def token_from_market(market: dict, side: ContractSide) -> str | None:
    if side == "YES":
        return yes_token_from_market(market)
    return no_token_from_market(market)


def market_volume_usd(market: dict) -> float:
    for key in ("volumeNum", "volumeClob", "volume"):
        value = market.get(key)
        if value is None:
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def resolution_datetime(market: dict) -> datetime | None:
    for key in ("closedTime", "endDate", "_event_endDate"):
        value = market.get(key)
        if not value:
            continue
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed
        except Exception:
            continue
    return None


def evaluate_market(
    market: dict,
    current_price: float | None,
    *,
    side: ContractSide = "YES",
    as_of: datetime | None = None,
    entered_positions: Mapping[str, dict] | None = None,
    calibration=None,
    forecast_probability_fn: ForecastProbabilityFn = estimate_forecast_prob,
    max_position_usd: float | None = None,
) -> Decision:
    if side not in {"YES", "NO"}:
        raise ValueError(f"unsupported contract side: {side}")

    now = as_of or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    market_id = str(market.get("id") or market.get("conditionId") or "")
    if not market_id:
        return Decision.skip("missing_market_id")

    existing_position = entered_position_for_market(market, entered_positions)
    if existing_position is not None:
        return Decision.skip(
            "already_entered",
            market_id=market_id,
            token_id=existing_position.get("token_id"),
        )

    token_id = token_from_market(market, side)
    if not token_id:
        reason = "missing_yes_token" if side == "YES" else "missing_no_token"
        return Decision.skip(reason, market_id=market_id, side=side)

    question = market.get("question") or ""
    end_date_hint = _parse_end_date(market.get("endDate") or market.get("_event_endDate"))
    parsed = parse_market_question(question, end_date_hint=end_date_hint)
    if not parsed:
        return Decision.skip("unparseable_question", market_id=market_id, question=question)

    volume = market_volume_usd(market)
    if volume < config.MIN_MARKET_VOLUME_USD:
        return Decision.skip(
            "low_volume",
            market_id=market_id,
            volume=volume,
            min_volume=config.MIN_MARKET_VOLUME_USD,
        )

    resolution_dt = resolution_datetime(market)
    if resolution_dt is None:
        return Decision.skip("missing_resolution_time", market_id=market_id)
    hours_before = (resolution_dt - now).total_seconds() / 3600.0
    if hours_before < config.MIN_HOURS_BEFORE_CLOSE:
        return Decision.skip(
            "too_close_to_resolution",
            market_id=market_id,
            hours_before=hours_before,
            min_hours=config.MIN_HOURS_BEFORE_CLOSE,
        )

    target_date = parsed["date"]
    as_of_date = now.date()
    if as_of_date >= target_date:
        return Decision.skip("target_not_future", market_id=market_id, target_date=target_date.isoformat())
    lead_days = (target_date - as_of_date).days
    if lead_days > config.MAX_LEAD_DAYS:
        return Decision.skip(
            "unsupported_lead_time",
            market_id=market_id,
            lead_days=lead_days,
            max_lead_days=config.MAX_LEAD_DAYS,
        )

    if current_price is None:
        return Decision.skip("missing_live_price", market_id=market_id, token_id=token_id, side=side)
    try:
        price = float(current_price)
    except (TypeError, ValueError):
        return Decision.skip("invalid_live_price", market_id=market_id, price=current_price, side=side)
    if price <= 0.0 or price >= 1.0:
        return Decision.skip("invalid_live_price", market_id=market_id, price=price, side=side)

    entry_price = min(0.999, price * (1.0 + config.SLIPPAGE))
    if entry_price < config.MIN_ENTRY_PRICE:
        return Decision.skip(
            "below_min_entry_price",
            market_id=market_id,
            price=price,
            entry_price=entry_price,
            min_price=config.MIN_ENTRY_PRICE,
            side=side,
        )
    if side == "NO" and entry_price > config.MAX_NO_ENTRY_PRICE:
        return Decision.skip(
            "above_max_no_entry_price",
            market_id=market_id,
            price=price,
            entry_price=entry_price,
            max_price=config.MAX_NO_ENTRY_PRICE,
            side=side,
        )

    yes_forecast_prob = forecast_probability_fn(
        lat=parsed["lat"],
        lon=parsed["lon"],
        tz=parsed["tz"],
        target_date=target_date,
        bracket_low=parsed["bracket_low"],
        bracket_high=parsed["bracket_high"],
        unit=parsed["unit"],
        as_of_date=as_of_date,
        metric=parsed.get("metric", "max"),
        temp_std_f=config.TEMP_STD_F,
        ensemble_sigma=False,
        use_empirical=config.USE_EMPIRICAL,
        city=parsed.get("city"),
    )
    if yes_forecast_prob is None:
        return Decision.skip("missing_forecast_probability", market_id=market_id)

    forecast_prob = yes_forecast_prob if side == "YES" else 1.0 - yes_forecast_prob
    if forecast_prob < config.MIN_FORECAST_PROB:
        return Decision.skip(
            "below_min_forecast_probability",
            market_id=market_id,
            forecast_prob=forecast_prob,
            min_forecast_prob=config.MIN_FORECAST_PROB,
            side=side,
        )

    edge = forecast_prob - entry_price
    if edge < config.MIN_EDGE:
        return Decision.skip(
            "below_min_edge",
            market_id=market_id,
            edge=edge,
            min_edge=config.MIN_EDGE,
            side=side,
        )

    if side == "YES" and calibration is not None and not calibration.passes(
        city=parsed.get("city") or "",
        bracket_low=parsed.get("bracket_low"),
        bracket_high=parsed.get("bracket_high"),
        price=price,
        lead_days=lead_days,
    ):
        return Decision.skip("calibration_rejected", market_id=market_id)

    position_usd = max_position_usd if max_position_usd is not None else config.max_position_usd()
    shares = position_usd / entry_price
    return Decision.enter(
        TradePlan(
            market_id=market_id,
            token_id=token_id,
            side=side,
            question=question,
            city=parsed["city"],
            target_date=target_date.isoformat(),
            market_price=price,
            entry_price=entry_price,
            shares=shares,
            position_usd=position_usd,
            forecast_prob=float(forecast_prob),
            edge=float(edge),
            lead_days=lead_days,
            entry_time=now,
            condition_id=str(market.get("conditionId") or "") or None,
            bracket_low=parsed.get("bracket_low"),
            bracket_high=parsed.get("bracket_high"),
            bracket_unit=parsed.get("unit"),
            metric=parsed.get("metric"),
        )
    )
