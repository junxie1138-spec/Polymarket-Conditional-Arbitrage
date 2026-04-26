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

EVENT_VALUE_FIELDS = (
    "market_id",
    "condition_id",
    "token_id",
    "city",
    "target_date",
    "bracket",
    "side",
    "model_probability",
    "intended_edge",
    "best_bid",
    "best_ask",
    "midpoint",
    "submitted_limit_price",
    "filled_price",
    "fill_quantity",
    "fees",
    "remaining_queue_time_seconds",
    "cancelled_at_utc",
    "realized_pnl",
    "mark_to_market_pnl",
    "final_resolved_payout",
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(value: datetime | None = None) -> str:
    dt = value or utc_now()
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_timestamp_utc(value: Any) -> str | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return utc_iso(value)
    try:
        if isinstance(value, (int, float)) or str(value).strip().replace(".", "", 1).isdigit():
            raw = float(value)
            if raw > 1_000_000_000_000:
                raw /= 1000.0
            if raw > 1_000_000_000:
                return utc_iso(datetime.fromtimestamp(raw, tz=timezone.utc))
    except (OSError, OverflowError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return utc_iso(parsed)


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


def first_float(mapping: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    for key in keys:
        value = mapping.get(key)
        if value in (None, "") or isinstance(value, bool):
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return None


def first_str(mapping: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = mapping.get(key)
        if value in (None, ""):
            continue
        return str(value)
    return None


def normalized_side(value: Any) -> str | None:
    side = str(value or "").strip().upper()
    return side if side in {"YES", "NO", "BUY", "SELL"} else None


def compact_raw_payload(payload: dict[str, Any]) -> dict[str, Any]:
    keep = (
        "event_type",
        "type",
        "id",
        "order_id",
        "orderID",
        "hash",
        "market",
        "conditionId",
        "condition_id",
        "asset",
        "asset_id",
        "token_id",
        "tokenId",
        "side",
        "outcome",
        "status",
        "price",
        "avgPrice",
        "average_price",
        "matched_price",
        "fee",
        "fees",
        "size",
        "original_size",
        "remaining_size",
        "size_matched",
        "matched_size",
        "cashPnl",
        "cash_pnl",
        "curPrice",
        "current_price",
        "payout",
        "timestamp",
        "created_at",
        "updated_at",
    )
    return {key: payload[key] for key in keep if key in payload}


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


class LiveEventLog:
    def __init__(
        self,
        *,
        event_path: str | Path = config.EVENT_LOG_PATH,
        market_snapshot_path: str | Path = config.MARKET_SNAPSHOT_PATH,
        forecast_snapshot_path: str | Path = config.FORECAST_SNAPSHOT_PATH,
    ):
        self.events = AppendOnlyJsonl(event_path)
        self.market_snapshots = AppendOnlyJsonl(market_snapshot_path)
        self.forecast_snapshots = AppendOnlyJsonl(forecast_snapshot_path)
        self._context_lock = threading.RLock()
        self._context_by_market: dict[str, dict[str, Any]] = {}
        self._context_by_condition: dict[str, dict[str, Any]] = {}
        self._context_by_token: dict[str, dict[str, Any]] = {}

    def remember_market_context(self, payload: dict[str, Any]) -> None:
        context_keys = (
            "market_id",
            "condition_id",
            "token_id",
            "city",
            "target_date",
            "bracket",
            "side",
            "question",
            "model_probability",
            "intended_edge",
        )
        context = {key: payload.get(key) for key in context_keys if payload.get(key) is not None}
        if not context:
            return
        with self._context_lock:
            if context.get("market_id"):
                self._context_by_market[str(context["market_id"])] = context
            if context.get("condition_id"):
                self._context_by_condition[str(context["condition_id"])] = context
            if context.get("token_id"):
                self._context_by_token[str(context["token_id"])] = context

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
        self._enrich_with_known_context(merged)
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
        for key in EVENT_VALUE_FIELDS:
            record[key] = merged.pop(key, None)
        record.update(merged)
        self.events.append(record)
        return record

    def _enrich_with_known_context(self, payload: dict[str, Any]) -> None:
        market_id = str(payload.get("market_id") or "")
        condition_id = str(payload.get("condition_id") or "")
        token_id = str(payload.get("token_id") or "")
        with self._context_lock:
            context = (
                self._context_by_market.get(market_id)
                or self._context_by_condition.get(condition_id)
                or self._context_by_token.get(token_id)
            )
        if not context:
            return
        for key, value in context.items():
            if payload.get(key) is None:
                payload[key] = value

    def append_market_snapshot(
        self,
        payload: dict[str, Any],
        *,
        timestamp_utc: datetime | str | None = None,
    ) -> dict[str, Any]:
        return self._append_snapshot(
            self.market_snapshots,
            "market",
            payload,
            timestamp_utc=timestamp_utc,
        )

    def append_forecast_snapshot(
        self,
        payload: dict[str, Any],
        *,
        timestamp_utc: datetime | str | None = None,
    ) -> dict[str, Any]:
        return self._append_snapshot(
            self.forecast_snapshots,
            "forecast",
            payload,
            timestamp_utc=timestamp_utc,
        )

    @staticmethod
    def _append_snapshot(
        store: AppendOnlyJsonl,
        snapshot_type: str,
        payload: dict[str, Any],
        *,
        timestamp_utc: datetime | str | None = None,
    ) -> dict[str, Any]:
        snapshot_timestamp = (
            utc_iso(timestamp_utc)
            if isinstance(timestamp_utc, datetime)
            else str(timestamp_utc) if timestamp_utc else utc_iso()
        )
        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "timestamp_utc": snapshot_timestamp,
            "snapshot_type": snapshot_type,
        }
        record.update(payload)
        store.append(record)
        return record


def order_lifecycle_events_from_payload(payload: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    event_type = str(payload.get("event_type") or payload.get("type") or "").strip().lower()
    status = str(payload.get("status") or "").strip().lower().replace(" ", "_")
    side_value = normalized_side(payload.get("outcome") or payload.get("side"))

    normalized = {
        "market_id": first_str(payload, ("market", "conditionId", "condition_id")),
        "condition_id": first_str(payload, ("conditionId", "condition_id", "market")),
        "token_id": first_str(payload, ("asset_id", "asset", "token_id", "tokenId")),
        "side": side_value if side_value in {"YES", "NO"} else None,
        "order_side": side_value if side_value in {"BUY", "SELL"} else None,
        "filled_price": first_float(
            payload,
            ("matched_price", "average_price", "avgPrice", "price"),
        ),
        "fill_quantity": first_float(payload, ("size_matched", "matched_size", "size")),
        "fees": first_float(payload, ("fee", "fees")),
        "remaining_quantity": first_float(payload, ("remaining_size",)),
        "submitted_quantity": first_float(payload, ("original_size",)),
        "exchange_order_id": first_str(payload, ("order_id", "orderID", "id", "hash")),
        "exchange_status": payload.get("status"),
        "exchange_event_type": payload.get("event_type") or payload.get("type"),
        "exchange_timestamp_utc": parse_timestamp_utc(
            payload.get("timestamp") or payload.get("updated_at") or payload.get("created_at")
        ),
        "raw": compact_raw_payload(payload),
    }
    normalized = {key: value for key, value in normalized.items() if value is not None}

    lifecycle_event = _lifecycle_event_type(event_type, status, payload)
    if lifecycle_event is None:
        return []
    if lifecycle_event == "order_cancelled":
        normalized["cancelled_at_utc"] = normalized.get("exchange_timestamp_utc") or utc_iso()
    return [(lifecycle_event, normalized)]


def _lifecycle_event_type(event_type: str, status: str, payload: dict[str, Any]) -> str | None:
    if event_type in {"placement", "order_placed", "order_created"}:
        return "order_acknowledged"
    if event_type in {"cancellation", "cancel", "order_cancelled"}:
        return "order_cancelled"
    if status in {"cancelled", "canceled", "cancelled_by_user", "canceled_by_user"}:
        return "order_cancelled"
    if status in {"partially_filled", "partial", "partially_matched"}:
        return "order_partially_filled"
    if status in {"filled", "complete", "completed"}:
        return "order_filled"
    if event_type == "trade":
        return _filled_or_partial(payload, default="order_partially_filled")
    if status in {"matched", "match"}:
        return _filled_or_partial(payload, default="order_partially_filled")
    return None


def _filled_or_partial(payload: dict[str, Any], *, default: str) -> str:
    remaining = first_float(payload, ("remaining_size",))
    original = first_float(payload, ("original_size",))
    matched = first_float(payload, ("size_matched", "matched_size", "size"))
    if remaining is not None:
        return "order_filled" if remaining <= 0 else "order_partially_filled"
    if original is not None and matched is not None:
        return "order_filled" if matched >= original else "order_partially_filled"
    return default


def market_resolved_payload(payload: dict[str, Any]) -> dict[str, Any]:
    payout = first_float(
        payload,
        (
            "final_resolved_payout",
            "resolved_payout",
            "winning_payout",
            "payout",
            "redeemable_value",
        ),
    )
    return {
        "market_id": first_str(payload, ("market", "conditionId", "condition_id", "id")),
        "condition_id": first_str(payload, ("conditionId", "condition_id", "market", "id")),
        "final_resolved_payout": payout,
        "exchange_event_type": payload.get("event_type") or payload.get("type"),
        "exchange_timestamp_utc": parse_timestamp_utc(
            payload.get("timestamp") or payload.get("updated_at") or payload.get("created_at")
        ),
        "raw": compact_raw_payload(payload),
    }
