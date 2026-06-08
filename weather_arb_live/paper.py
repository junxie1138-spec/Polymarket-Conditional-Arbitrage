from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config
from .arb_models import ArbOpportunity


class PaperLedgerLoadError(RuntimeError):
    pass


@dataclass(frozen=True)
class PaperLegFill:
    token_id: str
    outcome: str
    quantity: float
    vwap: float
    cost: float


@dataclass(frozen=True)
class PaperPosition:
    market_id: str
    condition_id: str | None
    question: str
    yes_token_id: str
    no_token_id: str
    opened_at: datetime
    status: str
    paired_fills: tuple[PaperLegFill, PaperLegFill]
    unmerged_quantity: float
    merged_quantity: float
    merge_value: float
    gross_cost: float
    estimated_fees: float
    gas_cost: float
    slippage_buffer: float
    realized_pnl: float
    net_return_bps: float
    audit_trail: list[dict[str, Any]] = field(default_factory=list)


def _jsonable(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def paper_position_from_opportunity(
    opportunity: ArbOpportunity,
    *,
    opened_at: datetime | None = None,
) -> PaperPosition:
    now = opened_at or datetime.now(timezone.utc)
    market = opportunity.market
    yes_fill = PaperLegFill(
        token_id=market.yes_token_id,
        outcome="YES",
        quantity=opportunity.executable_size,
        vwap=opportunity.yes_vwap,
        cost=opportunity.yes_cost,
    )
    no_fill = PaperLegFill(
        token_id=market.no_token_id,
        outcome="NO",
        quantity=opportunity.executable_size,
        vwap=opportunity.no_vwap,
        cost=opportunity.no_cost,
    )
    return PaperPosition(
        market_id=market.market_id,
        condition_id=market.condition_id,
        question=market.question,
        yes_token_id=market.yes_token_id,
        no_token_id=market.no_token_id,
        opened_at=now,
        status="merged",
        paired_fills=(yes_fill, no_fill),
        unmerged_quantity=0.0,
        merged_quantity=opportunity.executable_size,
        merge_value=opportunity.merge_value,
        gross_cost=opportunity.gross_cost,
        estimated_fees=opportunity.estimated_fees,
        gas_cost=opportunity.gas_cost,
        slippage_buffer=opportunity.slippage_buffer,
        realized_pnl=opportunity.net_profit,
        net_return_bps=opportunity.net_return_bps,
        audit_trail=[
            {
                "event": "opportunity_detected",
                "at": opportunity.detected_at.isoformat(),
                "source_timestamps": opportunity.source_timestamps,
                "details": opportunity.details,
            },
            {
                "event": "paired_fill_simulated",
                "at": now.isoformat(),
                "style": "all_or_none",
            },
            {
                "event": "merge_simulated",
                "at": now.isoformat(),
                "merged_quantity": opportunity.executable_size,
                "merge_value": opportunity.merge_value,
            },
        ],
    )


class PaperMergeLedger:
    def __init__(self, path: str | Path = config.MERGE_ARB_POSITIONS_PATH):
        self.path = Path(path)
        self.positions: dict[str, dict[str, Any]] = {}

    def load(self) -> "PaperMergeLedger":
        if not self.path.exists():
            self.positions = {}
            return self
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperLedgerLoadError(f"failed to load paper ledger {self.path}: {exc}") from exc
        if not isinstance(data, dict):
            raise PaperLedgerLoadError(
                f"paper ledger {self.path} must contain a JSON object, got {type(data).__name__}"
            )
        invalid_keys = [
            key
            for key, value in data.items()
            if not isinstance(key, str) or not isinstance(value, dict)
        ]
        if invalid_keys:
            raise PaperLedgerLoadError(
                f"paper ledger {self.path} contains invalid rows: {invalid_keys[:3]}"
            )
        self.positions = data
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.positions, f, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def entered_positions(self) -> dict[str, dict[str, Any]]:
        return dict(self.positions)

    def has_market(self, market_id: str, condition_id: str | None = None) -> bool:
        if market_id in self.positions:
            return True
        if condition_id and condition_id in self.positions:
            return True
        for row in self.positions.values():
            if row.get("market_id") == market_id:
                return True
            if condition_id and row.get("condition_id") == condition_id:
                return True
        return False

    def record_position(self, position: PaperPosition) -> dict[str, Any]:
        if self.has_market(position.market_id, position.condition_id):
            raise ValueError(f"paper position already exists for market_id={position.market_id}")
        row = _jsonable(asdict(position))
        self.positions[position.market_id] = row
        return row


class PaperTradingEngine:
    def __init__(self, ledger: PaperMergeLedger):
        self.ledger = ledger

    def execute(self, opportunity: ArbOpportunity, *, as_of: datetime | None = None) -> dict[str, Any]:
        position = paper_position_from_opportunity(opportunity, opened_at=as_of)
        row = self.ledger.record_position(position)
        self.ledger.save()
        return row

