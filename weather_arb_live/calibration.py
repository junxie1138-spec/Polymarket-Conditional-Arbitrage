"""Calibrated win-rate filter (Point 3).

Replaces the raw MIN_EDGE gate with an empirical lookup: admit an entry only
when the observed historical win rate on similar trades exceeds a threshold.

**Look-ahead guard:** the lookup is built from the first `TRAIN_SPLIT_MONTHS`
of trades (by `target_date`) in a baseline run's ``backtest_trades.csv``. The
remaining months are unseen and are the evaluation set. Missing / sparse keys
default to ``True`` so the filter degrades gracefully to raw-MIN_EDGE-only.

Lookup key: (city_lower, bracket_type, price_bucket_5c, lead_bucket).
"""
from __future__ import annotations
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

from . import config

CALIBRATION_PATH = config.CALIBRATION_PATH
TRAIN_SPLIT_MONTHS = 12
DEFAULT_MIN_WIN_RATE = 0.55
DEFAULT_MIN_N = 50


# ---------- key derivation -------------------------------------------------

def _price_bucket(price: float) -> str:
    if price < 0.25:
        return "<0.25"
    floor = int(price / 0.05) * 0.05
    return f"{floor:.2f}-{floor + 0.05:.2f}"


def _lead_bucket(days: float | int) -> str:
    d = int(days)
    if d <= 1:
        return "1"
    if d <= 3:
        return "2-3"
    return "4-7"


def _bracket_type(bracket_low: float | None, bracket_high: float | None) -> str:
    if bracket_low is None and bracket_high is None:
        return "unknown"
    if bracket_low is None:
        return "below"
    if bracket_high is None:
        return "above"
    if bracket_low == bracket_high:
        return "exactly"
    return "between"


def _make_key(
    city: str, bracket_low: float | None, bracket_high: float | None,
    price: float, lead_days: float | int,
) -> str:
    return (
        f"{city.lower()}|{_bracket_type(bracket_low, bracket_high)}|"
        f"{_price_bucket(float(price))}|{_lead_bucket(lead_days)}"
    )


def _wilson_lower_95(wins: int, n: int) -> float:
    """Wilson score lower bound of a binomial proportion at 95% confidence."""
    if n == 0:
        return 0.0
    z = 1.96
    p = wins / n
    denom = 1.0 + z * z / n
    center = p + z * z / (2.0 * n)
    margin = z * math.sqrt(p * (1.0 - p) / n + z * z / (4.0 * n * n))
    return max(0.0, (center - margin) / denom)


# ---------- training -------------------------------------------------------

def build_calibration_from_trades(
    csv_path: str,
    train_split_months: int = TRAIN_SPLIT_MONTHS,
) -> dict:
    """Build the calibration lookup from a baseline run's trades CSV.

    Splits rows by ``target_date``: the first ``train_split_months`` months of
    dated trades form the training window; later trades are the evaluation
    window and are NOT included in the table.
    """
    import pandas as pd

    from .market_parser import parse_market_question

    if not Path(csv_path).exists():
        raise FileNotFoundError(csv_path)
    df = pd.read_csv(csv_path)
    if df.empty:
        return {}

    df["target_date"] = pd.to_datetime(df["target_date"])
    df = df.sort_values("target_date").reset_index(drop=True)
    earliest: datetime = df["target_date"].iloc[0].to_pydatetime()
    cutoff = earliest + pd.DateOffset(months=train_split_months)
    train = df[df["target_date"] < cutoff]

    buckets: dict[str, dict[str, int]] = {}
    skipped_unparseable = 0
    for _, row in train.iterrows():
        td = row["target_date"]
        end_hint = td.date() if hasattr(td, "date") else None
        parsed = parse_market_question(row.get("question") or "", end_date_hint=end_hint)
        if not parsed:
            skipped_unparseable += 1
            continue
        key = _make_key(
            city=row["city"],
            bracket_low=parsed.get("bracket_low"),
            bracket_high=parsed.get("bracket_high"),
            price=row["entry_price"],
            lead_days=row["days_to_expiry"],
        )
        rec = buckets.setdefault(key, {"n": 0, "wins": 0})
        rec["n"] += 1
        if bool(row["resolved_yes"]):
            rec["wins"] += 1

    table: dict = {}
    for key, rec in buckets.items():
        n = rec["n"]
        wins = rec["wins"]
        table[key] = {
            "n": n,
            "wins": wins,
            "win_rate": wins / n if n else 0.0,
            "lower_95_ci": _wilson_lower_95(wins, n),
        }
    print(
        f"calibration: trained on {len(train)} rows "
        f"({skipped_unparseable} unparseable); {len(table)} keys; "
        f"train window = {earliest.date()} .. {cutoff.date()}"
    )
    return table


def save_calibration(table: dict, path: str | Path = CALIBRATION_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(table, f, indent=2)
    print(f"calibration: saved {len(table)} keys to {path}")


# ---------- runtime filter -------------------------------------------------

@dataclass
class Calibration:
    lookup: dict
    min_win_rate: float = DEFAULT_MIN_WIN_RATE
    min_n: int = DEFAULT_MIN_N
    use_lower_ci: bool = False
    misses: int = 0
    sparse: int = 0
    admits: int = 0
    rejects: int = 0

    def passes(
        self,
        city: str,
        bracket_low: float | None,
        bracket_high: float | None,
        price: float,
        lead_days: float | int,
    ) -> bool:
        key = _make_key(city, bracket_low, bracket_high, price, lead_days)
        stats = self.lookup.get(key)
        if stats is None:
            self.misses += 1
            return True  # no historical data → permissive, matches baseline
        if stats["n"] < self.min_n:
            self.sparse += 1
            return True
        rate = stats["lower_95_ci"] if self.use_lower_ci else stats["win_rate"]
        if rate >= self.min_win_rate:
            self.admits += 1
            return True
        self.rejects += 1
        return False


def load_calibration(
    path: str | Path = CALIBRATION_PATH,
    min_win_rate: float = DEFAULT_MIN_WIN_RATE,
    min_n: int = DEFAULT_MIN_N,
    use_lower_ci: bool = False,
) -> Calibration | None:
    path = Path(path)
    if not path.exists():
        return None
    with path.open(encoding="utf-8") as f:
        data = json.load(f)
    return Calibration(
        lookup=data, min_win_rate=min_win_rate, min_n=min_n, use_lower_ci=use_lower_ci,
    )


# ---------- CLI ------------------------------------------------------------

def _cli(argv: Iterable[str] | None = None):
    import argparse
    parser = argparse.ArgumentParser(description="Build calibration table from a trades CSV")
    parser.add_argument(
        "--build-from",
        default="output/baseline_legacy/backtest_trades.csv",
        help="Path to baseline backtest_trades.csv",
    )
    parser.add_argument("--out", default=CALIBRATION_PATH, help="Where to write the table")
    parser.add_argument(
        "--train-months", type=int, default=TRAIN_SPLIT_MONTHS,
        help="Number of leading months to use as the training window",
    )
    args = parser.parse_args(argv)
    table = build_calibration_from_trades(args.build_from, train_split_months=args.train_months)
    save_calibration(table, path=args.out)


if __name__ == "__main__":
    _cli()
