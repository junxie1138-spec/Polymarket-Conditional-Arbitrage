from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from . import config
from .strategy import TradePlan


class PositionLedger:
    def __init__(self, path: str | Path = config.POSITIONS_PATH):
        self.path = Path(path)
        self.positions: dict[str, dict[str, Any]] = {}

    def load(self) -> "PositionLedger":
        if not self.path.exists():
            self.positions = {}
            return self
        try:
            with self.path.open(encoding="utf-8") as f:
                data = json.load(f)
            self.positions = data if isinstance(data, dict) else {}
        except Exception:
            self.positions = {}
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.positions, f, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def entered_positions(self, *, include_dry_run: bool) -> dict[str, dict[str, Any]]:
        if include_dry_run:
            return dict(self.positions)
        return {
            market_id: row
            for market_id, row in self.positions.items()
            if not bool(row.get("dry_run"))
        }

    def record(
        self,
        plan: TradePlan,
        *,
        dry_run: bool,
        order_response: dict | None = None,
    ) -> dict[str, Any]:
        row = asdict(plan)
        row["entry_time"] = plan.entry_time.isoformat()
        row["dry_run"] = dry_run
        row["order_response"] = order_response
        self.positions[plan.market_id] = row
        return row
