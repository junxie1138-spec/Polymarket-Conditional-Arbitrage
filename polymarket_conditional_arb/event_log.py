from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import config

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    dt = value or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return jsonable(asdict(value))
    if isinstance(value, datetime):
        return utc_iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): jsonable(val) for key, val in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [jsonable(item) for item in value]
    try:
        json.dumps(value)
    except TypeError:
        return str(value)
    return value


class AppendOnlyJsonl:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.RLock()

    def append(self, record: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(jsonable(record), separators=(",", ":"), sort_keys=True) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8", newline="\n") as f:
                f.write(line)
                f.flush()
                os.fsync(f.fileno())


class ConditionalArbEventLog:
    def __init__(self, path: str | Path | None = None):
        self.events = AppendOnlyJsonl(path or config.event_log_path())

    def append_event(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        *,
        timestamp_utc: datetime | str | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        merged = dict(payload or {})
        merged.update(fields)
        event_timestamp = (
            utc_iso(timestamp_utc)
            if isinstance(timestamp_utc, datetime)
            else str(timestamp_utc) if timestamp_utc else utc_iso()
        )
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": event_timestamp,
            "event_type": event_type,
        }
        record.update(merged)
        self.events.append(record)
        return record
