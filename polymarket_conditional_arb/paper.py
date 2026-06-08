from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .arb_models import ConditionalArbOpportunity


class PaperLedgerLoadError(RuntimeError):
    pass


class PaperConditionalArbLedger:
    def __init__(self, path: str | Path | None = None):
        self.path = Path(path or config.paper_ledger_path())
        self.opportunities: dict[str, dict[str, Any]] = {}

    def load(self) -> "PaperConditionalArbLedger":
        if not self.path.exists():
            self.opportunities = {}
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
        invalid = [key for key, value in data.items() if not isinstance(key, str) or not isinstance(value, dict)]
        if invalid:
            raise PaperLedgerLoadError(
                f"paper ledger {self.path} contains invalid rows: {invalid[:3]}"
            )
        self.opportunities = data
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(self.opportunities, f, indent=2, sort_keys=True)
        tmp.replace(self.path)

    def has_opportunity(self, opportunity_id: str) -> bool:
        return opportunity_id in self.opportunities

    def record(self, opportunity: ConditionalArbOpportunity, *, as_of: datetime | None = None) -> dict[str, Any]:
        if self.has_opportunity(opportunity.opportunity_id):
            raise ValueError(f"paper opportunity already exists: {opportunity.opportunity_id}")
        now = as_of or datetime.now(timezone.utc)
        row = opportunity.to_record()
        row.update(
            {
                "status": "paper_alert_recorded",
                "recorded_at": now.isoformat(),
                "mode": "paper_alert_only",
            }
        )
        self.opportunities[opportunity.opportunity_id] = row
        self.save()
        return row
