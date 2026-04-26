from __future__ import annotations

import argparse
import json
import math
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from xml.sax.saxutils import escape

from . import config


REPORTS_DIR = config.PROJECT_ROOT / "reports"


def parse_utc_timestamp(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def parse_report_date(value: str | None) -> date:
    if value:
        return date.fromisoformat(value)
    return datetime.now(UTC).date()


def report_window(day: date) -> tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, tzinfo=UTC)
    return start, start + timedelta(days=1)


def read_jsonl_for_day(path: Path, day: date) -> tuple[list[dict[str, Any]], int]:
    start, end = report_window(day)
    rows: list[dict[str, Any]] = []
    skipped = 0
    if not path.exists():
        return rows, skipped

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            raw = line.strip()
            if not raw:
                continue
            try:
                row = json.loads(raw)
            except json.JSONDecodeError:
                skipped += 1
                continue
            timestamp = parse_utc_timestamp(row.get("timestamp_utc"))
            if timestamp is None:
                skipped += 1
                continue
            if start <= timestamp < end:
                rows.append(row)
    return rows, skipped


def as_float(value: Any) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(result) or math.isinf(result):
        return None
    return result


def compact_json(value: Any) -> str:
    if value in (None, ""):
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
    return str(value)


def avg(values: Iterable[float | None]) -> float | None:
    clean = [value for value in values if value is not None]
    if not clean:
        return None
    return sum(clean) / len(clean)


def total(values: Iterable[float | None]) -> float:
    return sum(value for value in values if value is not None)


def pct(numerator: float, denominator: float) -> float | None:
    if denominator == 0:
        return None
    return numerator / denominator


def spread(row: dict[str, Any]) -> float | None:
    bid = as_float(row.get("best_bid"))
    ask = as_float(row.get("best_ask"))
    if bid is None or ask is None:
        return None
    return ask - bid


def fill_notional(row: dict[str, Any]) -> float | None:
    price = as_float(row.get("filled_price"))
    quantity = as_float(row.get("fill_quantity"))
    if price is None or quantity is None:
        return None
    return price * quantity


def entry_spread_cost(row: dict[str, Any]) -> float | None:
    midpoint = as_float(row.get("midpoint"))
    fill_price = as_float(row.get("filled_price"))
    if midpoint is None or fill_price is None:
        return None
    return abs(fill_price - midpoint)


def estimated_net_edge(row: dict[str, Any]) -> float | None:
    edge = as_float(row.get("intended_edge"))
    if edge is None:
        return None
    cost = entry_spread_cost(row) or 0.0
    quantity = as_float(row.get("fill_quantity"))
    fees = as_float(row.get("fees")) or 0.0
    fee_per_share = fees / quantity if quantity else 0.0
    return edge - cost - fee_per_share


@dataclass
class ReportData:
    day: date
    events: list[dict[str, Any]]
    market_snapshots: list[dict[str, Any]]
    forecast_snapshots: list[dict[str, Any]]
    skipped_lines: int


def load_report_data(day: date) -> ReportData:
    events, skipped_events = read_jsonl_for_day(config.EVENT_LOG_PATH, day)
    market_snapshots, skipped_market = read_jsonl_for_day(config.MARKET_SNAPSHOT_PATH, day)
    forecast_snapshots, skipped_forecast = read_jsonl_for_day(config.FORECAST_SNAPSHOT_PATH, day)
    return ReportData(
        day=day,
        events=events,
        market_snapshots=market_snapshots,
        forecast_snapshots=forecast_snapshots,
        skipped_lines=skipped_events + skipped_market + skipped_forecast,
    )


def build_workbook_sheets(data: ReportData) -> dict[str, list[list[Any]]]:
    events = data.events
    counts = Counter(str(row.get("event_type") or "") for row in events)
    signals = [row for row in events if row.get("event_type") == "signal_generated"]
    fills = [row for row in events if row.get("event_type") == "order_filled"]
    submitted = [row for row in events if row.get("event_type") == "order_submitted"]
    cancelled = [row for row in events if row.get("event_type") == "order_cancelled"]

    summary = [
        ["Metric", "Value"],
        ["Report date UTC", data.day.isoformat()],
        ["Events", len(events)],
        ["Market snapshots", len(data.market_snapshots)],
        ["Forecast snapshots", len(data.forecast_snapshots)],
        ["Skipped malformed/incomplete lines", data.skipped_lines],
        ["Signals generated", len(signals)],
        ["Orders submitted", len(submitted)],
        ["Orders acknowledged", counts.get("order_acknowledged", 0)],
        ["Orders partially filled", counts.get("order_partially_filled", 0)],
        ["Orders filled", len(fills)],
        ["Orders cancelled", len(cancelled)],
        ["Fill rate vs submitted", pct(len(fills), len(submitted))],
        ["Average model probability", avg(as_float(row.get("model_probability")) for row in signals)],
        ["Average intended edge", avg(as_float(row.get("intended_edge")) for row in signals)],
        ["Average quoted spread", avg(spread(row) for row in signals)],
        ["Average entry spread cost", avg(entry_spread_cost(row) for row in fills)],
        ["Average estimated net edge on fills", avg(estimated_net_edge(row) for row in fills)],
        ["Filled quantity", total(as_float(row.get("fill_quantity")) for row in fills)],
        ["Filled notional", total(fill_notional(row) for row in fills)],
        ["Fees", total(as_float(row.get("fees")) for row in events)],
        ["Realized PnL", total(as_float(row.get("realized_pnl")) for row in events)],
        ["Mark-to-market PnL", total(as_float(row.get("mark_to_market_pnl")) for row in events)],
        ["Final resolved payout", total(as_float(row.get("final_resolved_payout")) for row in events)],
    ]

    event_counts = [["Event Type", "Count"]]
    event_counts.extend([event_type, count] for event_type, count in counts.most_common())

    market_rows = build_market_quality_rows(events)
    signal_rows = build_signal_review_rows(events)

    raw_headers = [
        "timestamp_utc",
        "event_type",
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
        "exchange_order_id",
        "order_status",
    ]
    raw_events = [raw_headers]
    for row in sorted(events, key=lambda item: str(item.get("timestamp_utc") or "")):
        raw_events.append([compact_json(row.get(header)) for header in raw_headers])

    latest_market_snapshots = latest_snapshot_rows(data.market_snapshots)
    latest_forecast_snapshots = latest_snapshot_rows(data.forecast_snapshots)

    return {
        "Summary": summary,
        "Event Counts": event_counts,
        "Market Quality": market_rows,
        "Signal Review": signal_rows,
        "Latest Markets": latest_market_snapshots,
        "Latest Forecasts": latest_forecast_snapshots,
        "Raw Events": raw_events,
    }


def build_market_quality_rows(events: list[dict[str, Any]]) -> list[list[Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        market_key = str(row.get("market_id") or row.get("condition_id") or row.get("token_id") or "")
        if market_key:
            grouped[market_key].append(row)

    headers = [
        "Market ID",
        "City",
        "Date",
        "Bracket",
        "Signals",
        "Submitted",
        "Filled",
        "Cancelled",
        "Avg Edge",
        "Avg Spread",
        "Avg Entry Spread Cost",
        "Avg Estimated Net Edge",
        "Filled Qty",
        "Filled Notional",
        "Fees",
        "Realized PnL",
        "MTM PnL",
        "First Event UTC",
        "Last Event UTC",
    ]
    rows = [headers]
    for market_id, market_events in sorted(grouped.items()):
        counts = Counter(str(row.get("event_type") or "") for row in market_events)
        fills = [row for row in market_events if row.get("event_type") == "order_filled"]
        first = min(str(row.get("timestamp_utc") or "") for row in market_events)
        last = max(str(row.get("timestamp_utc") or "") for row in market_events)
        context = next((row for row in market_events if row.get("city") or row.get("bracket")), market_events[0])
        rows.append(
            [
                market_id,
                context.get("city"),
                context.get("target_date"),
                compact_json(context.get("bracket")),
                counts.get("signal_generated", 0),
                counts.get("order_submitted", 0),
                counts.get("order_filled", 0),
                counts.get("order_cancelled", 0),
                avg(as_float(row.get("intended_edge")) for row in market_events),
                avg(spread(row) for row in market_events),
                avg(entry_spread_cost(row) for row in fills),
                avg(estimated_net_edge(row) for row in fills),
                total(as_float(row.get("fill_quantity")) for row in fills),
                total(fill_notional(row) for row in fills),
                total(as_float(row.get("fees")) for row in market_events),
                total(as_float(row.get("realized_pnl")) for row in market_events),
                total(as_float(row.get("mark_to_market_pnl")) for row in market_events),
                first,
                last,
            ]
        )
    return rows


def build_signal_review_rows(events: list[dict[str, Any]]) -> list[list[Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in events:
        market_key = str(row.get("market_id") or row.get("condition_id") or row.get("token_id") or "")
        if market_key:
            grouped[market_key].append(row)

    rows = [
        [
            "Market ID",
            "City",
            "Date",
            "Bracket",
            "Side",
            "Max Edge",
            "Avg Spread",
            "Submitted",
            "Filled Qty",
            "Fees",
            "Realized PnL",
            "MTM PnL",
            "Review Flag",
        ]
    ]
    for market_id, market_events in sorted(grouped.items()):
        counts = Counter(str(row.get("event_type") or "") for row in market_events)
        fills = [row for row in market_events if row.get("event_type") == "order_filled"]
        context = next((row for row in market_events if row.get("event_type") == "signal_generated"), market_events[0])
        filled_qty = total(as_float(row.get("fill_quantity")) for row in fills)
        realized = total(as_float(row.get("realized_pnl")) for row in market_events)
        mtm = total(as_float(row.get("mark_to_market_pnl")) for row in market_events)
        max_edge = max(
            (value for value in (as_float(row.get("intended_edge")) for row in market_events) if value is not None),
            default=None,
        )
        avg_spread = avg(spread(row) for row in market_events)
        flag = review_flag(counts, filled_qty, realized, mtm, max_edge, avg_spread)
        rows.append(
            [
                market_id,
                context.get("city"),
                context.get("target_date"),
                compact_json(context.get("bracket")),
                context.get("side"),
                max_edge,
                avg_spread,
                counts.get("order_submitted", 0),
                filled_qty,
                total(as_float(row.get("fees")) for row in market_events),
                realized,
                mtm,
                flag,
            ]
        )
    return rows


def review_flag(
    counts: Counter[str],
    filled_qty: float,
    realized_pnl: float,
    mtm_pnl: float,
    max_edge: float | None,
    avg_spread: float | None,
) -> str:
    if counts.get("signal_generated", 0) and not counts.get("order_submitted", 0):
        return "signal_not_submitted"
    if counts.get("order_submitted", 0) and filled_qty == 0:
        return "submitted_not_filled"
    if max_edge is not None and max_edge > 0 and (realized_pnl < 0 or mtm_pnl < 0):
        return "positive_edge_negative_pnl"
    if max_edge is not None and avg_spread is not None and avg_spread >= max_edge:
        return "spread_consumed_edge"
    return ""


def latest_snapshot_rows(snapshots: list[dict[str, Any]]) -> list[list[Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in snapshots:
        market_key = str(row.get("market_id") or row.get("condition_id") or row.get("token_id") or "")
        if not market_key:
            continue
        if str(row.get("timestamp_utc") or "") >= str(latest.get(market_key, {}).get("timestamp_utc") or ""):
            latest[market_key] = row

    headers = [
        "timestamp_utc",
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
        "forecast_value",
        "raw",
    ]
    rows = [headers]
    for row in sorted(latest.values(), key=lambda item: str(item.get("market_id") or item.get("condition_id") or "")):
        rows.append([compact_json(row.get(header)) for header in headers])
    return rows


def write_xlsx(path: Path, sheets: dict[str, list[list[Any]]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types_xml(len(sheets)))
        zf.writestr("_rels/.rels", package_rels_xml())
        zf.writestr("xl/workbook.xml", workbook_xml(list(sheets)))
        zf.writestr("xl/_rels/workbook.xml.rels", workbook_rels_xml(len(sheets)))
        zf.writestr("xl/styles.xml", styles_xml())
        for index, rows in enumerate(sheets.values(), start=1):
            zf.writestr(f"xl/worksheets/sheet{index}.xml", worksheet_xml(rows))


def content_types_xml(sheet_count: int) -> str:
    overrides = [
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>',
        '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>',
    ]
    overrides.extend(
        f'<Override PartName="/xl/worksheets/sheet{index}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        for index in range(1, sheet_count + 1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(overrides)
        + "</Types>"
    )


def package_rels_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        "</Relationships>"
    )


def workbook_xml(sheet_names: list[str]) -> str:
    sheets = "".join(
        f'<sheet name="{escape(name)}" sheetId="{index}" r:id="rId{index}"/>'
        for index, name in enumerate(sheet_names, start=1)
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        f"<sheets>{sheets}</sheets>"
        "</workbook>"
    )


def workbook_rels_xml(sheet_count: int) -> str:
    rels = "".join(
        f'<Relationship Id="rId{index}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{index}.xml"/>'
        for index in range(1, sheet_count + 1)
    )
    rels += (
        f'<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + rels
        + "</Relationships>"
    )


def styles_xml() -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<fonts count="2"><font><sz val="11"/><name val="Calibri"/></font><font><b/><sz val="11"/><name val="Calibri"/></font></fonts>'
        '<fills count="1"><fill><patternFill patternType="none"/></fill></fills>'
        '<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/></cellXfs>'
        "</styleSheet>"
    )


def worksheet_xml(rows: list[list[Any]]) -> str:
    body = "".join(
        f'<row r="{row_index}">' + "".join(cell_xml(row_index, col_index, value, header=row_index == 1) for col_index, value in enumerate(row, start=1)) + "</row>"
        for row_index, row in enumerate(rows, start=1)
    )
    widths = "".join(f'<col min="{index}" max="{index}" width="18" customWidth="1"/>' for index in range(1, max_column_count(rows) + 1))
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        f"<cols>{widths}</cols>"
        f"<sheetData>{body}</sheetData>"
        "</worksheet>"
    )


def max_column_count(rows: list[list[Any]]) -> int:
    return max((len(row) for row in rows), default=1)


def cell_xml(row_index: int, col_index: int, value: Any, *, header: bool) -> str:
    ref = f"{column_name(col_index)}{row_index}"
    style = ' s="1"' if header else ""
    if value is None:
        return f'<c r="{ref}"{style}/>'
    if isinstance(value, bool):
        return f'<c r="{ref}" t="b"{style}><v>{1 if value else 0}</v></c>'
    numeric = as_float(value)
    if numeric is not None and not isinstance(value, str):
        return f'<c r="{ref}"{style}><v>{numeric}</v></c>'
    text = escape(str(value))
    return f'<c r="{ref}" t="inlineStr"{style}><is><t>{text}</t></is></c>'


def column_name(index: int) -> str:
    name = ""
    while index:
        index, remainder = divmod(index - 1, 26)
        name = chr(65 + remainder) + name
    return name


def create_daily_report(day: date, output_path: Path | None = None) -> Path:
    data = load_report_data(day)
    sheets = build_workbook_sheets(data)
    output = output_path or REPORTS_DIR / f"daily_report_{day.isoformat()}.xlsx"
    write_xlsx(output, sheets)
    return output


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create a daily Excel report from live bot event logs.")
    parser.add_argument("--date", help="UTC date to report, formatted YYYY-MM-DD. Defaults to today in UTC.")
    parser.add_argument("--output", help="Output .xlsx path. Defaults to reports/daily_report_YYYY-MM-DD.xlsx.")
    args = parser.parse_args(argv)

    day = parse_report_date(args.date)
    output = create_daily_report(day, Path(args.output) if args.output else None)
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
