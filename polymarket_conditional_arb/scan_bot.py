from __future__ import annotations

import argparse
import json
import logging
import signal
import time
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import config
from .arb_models import BinaryMarket, ConditionalArbOpportunity
from .arb_strategy import ArbDecision, ArbStrategyParams, evaluate_binary_arbitrage, evaluate_neg_risk_event_group
from .event_log import ConditionalArbEventLog, jsonable, utc_iso
from .fetcher import GammaClobClient
from .paper import PaperConditionalArbLedger


def setup_logging(scan_config: config.ScanConfig) -> logging.Logger:
    scan_config.log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("polymarket_conditional_arb.scan")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(scan_config.scan_log_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


def _token_ids_for_markets(markets: list[BinaryMarket]) -> list[str]:
    token_ids: list[str] = []
    for market in markets:
        token_ids.extend([market.yes_token_id, market.no_token_id])
    return token_ids


def _write_opportunities_snapshot(path: Path, opportunities: list[ConditionalArbOpportunity], summary: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utc_iso(),
        "summary": summary,
        "opportunities": [opportunity.to_record() for opportunity in opportunities],
    }
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(jsonable(payload), f, indent=2, sort_keys=True)
    tmp.replace(path)


class ConditionalArbScanner:
    def __init__(
        self,
        *,
        scan_config: config.ScanConfig | None = None,
        client: GammaClobClient | None = None,
        ledger: PaperConditionalArbLedger | None = None,
        event_log: ConditionalArbEventLog | None = None,
        logger: logging.Logger | None = None,
        params: ArbStrategyParams | None = None,
    ):
        self.config = scan_config or config.load_scan_config()
        self.client = client or GammaClobClient(clob_host=self.config.clob_host)
        self.ledger = ledger or PaperConditionalArbLedger(self.config.paper_ledger_path)
        self.event_log = event_log or ConditionalArbEventLog(self.config.event_log_path)
        self.logger = logger or logging.getLogger("polymarket_conditional_arb.scan")
        self.params = params or ArbStrategyParams.from_config(self.config)
        self.running = True

    def bootstrap(self) -> None:
        self.config.data_dir.mkdir(parents=True, exist_ok=True)
        self.config.log_dir.mkdir(parents=True, exist_ok=True)
        self.ledger.load()
        self.event_log.append_event(
            "conditional_arb_scanner_started",
            {
                "mode": "paper_alert_only",
                "clob_host": self.config.clob_host,
                "market_limit": self.config.market_limit,
                "include_neg_risk": self.config.include_neg_risk,
                "min_net_profit_usd": self.params.min_net_profit_usd,
                "min_net_return_bps": self.params.min_net_return_bps,
                "max_capital_usd": self.params.max_capital_usd,
            },
        )

    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            self.logger.info("shutdown_signal signal=%s", signum)
            self.running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    def run_once(self) -> dict[str, Any]:
        self.bootstrap()
        try:
            return self.run_one_cycle()
        finally:
            self.ledger.save()

    def run_forever(self) -> None:
        self.bootstrap()
        self.install_signal_handlers()
        while self.running:
            self.run_one_cycle()
            if self.running:
                time.sleep(self.config.poll_interval_seconds)
        self.ledger.save()

    def run_one_cycle(self) -> dict[str, Any]:
        cycle_started = datetime.now(timezone.utc)
        self.logger.info("cycle_start at=%s", cycle_started.isoformat())
        self.event_log.append_event(
            "conditional_arb_cycle_started",
            {"cycle_started_at_utc": utc_iso(cycle_started)},
        )

        skip_counts: dict[str, int] = {}
        events = self.client.fetch_active_events()
        raw_markets = self.client.flatten_event_markets(events)
        tradable_markets = self.client.tradable_binary_markets(raw_markets)
        if self.config.market_limit is not None:
            tradable_markets = tradable_markets[: self.config.market_limit]

        books_by_token = self.client.fetch_ask_books(_token_ids_for_markets(tradable_markets))
        entered_positions = dict(self.ledger.opportunities)
        opportunities: list[ConditionalArbOpportunity] = []

        for market in tradable_markets:
            if market.neg_risk:
                continue
            yes_book = books_by_token.get(market.yes_token_id)
            no_book = books_by_token.get(market.no_token_id)
            if yes_book is None or no_book is None:
                skip_counts["missing_ask_book"] = skip_counts.get("missing_ask_book", 0) + 1
                continue
            decision = evaluate_binary_arbitrage(
                market,
                yes_book,
                no_book,
                as_of=cycle_started,
                entered_positions=entered_positions,
                params=self.params,
            )
            self._handle_decision(decision, opportunities, skip_counts)

        neg_risk_groups: dict[str, list[BinaryMarket]] = defaultdict(list)
        if self.config.include_neg_risk:
            for market in tradable_markets:
                if market.neg_risk and market.event_id:
                    neg_risk_groups[market.event_id].append(market)
                elif market.neg_risk:
                    skip_counts["missing_grouping_metadata"] = skip_counts.get("missing_grouping_metadata", 0) + 1

            for group in neg_risk_groups.values():
                decision = evaluate_neg_risk_event_group(
                    group,
                    books_by_token,
                    as_of=cycle_started,
                    params=self.params,
                )
                self._handle_decision(decision, opportunities, skip_counts)

        recorded = 0
        for opportunity in opportunities:
            if self.ledger.has_opportunity(opportunity.opportunity_id):
                skip_counts["already_recorded"] = skip_counts.get("already_recorded", 0) + 1
                continue
            self.ledger.record(opportunity, as_of=cycle_started)
            recorded += 1
            self.event_log.append_event(
                "conditional_arb_opportunity_recorded",
                opportunity.to_record(),
            )

        summary = {
            "cycle_started_at_utc": utc_iso(cycle_started),
            "events_fetched": len(events),
            "raw_markets": len(raw_markets),
            "tradable_markets": len(tradable_markets),
            "standard_binary_markets": sum(1 for market in tradable_markets if not market.neg_risk),
            "neg_risk_groups": len(neg_risk_groups),
            "opportunities_detected": len(opportunities),
            "opportunities_recorded": recorded,
            "skip_counts": skip_counts,
        }
        _write_opportunities_snapshot(self.config.opportunities_path, opportunities, summary)
        self.event_log.append_event("conditional_arb_cycle_completed", summary)
        self.logger.info(
            "cycle_end events=%s raw_markets=%s tradable=%s opportunities=%s recorded=%s skipped=%s",
            len(events),
            len(raw_markets),
            len(tradable_markets),
            len(opportunities),
            recorded,
            skip_counts,
        )
        return {
            "summary": summary,
            "opportunities": [opportunity.to_record() for opportunity in opportunities],
        }

    def _handle_decision(
        self,
        decision: ArbDecision,
        opportunities: list[ConditionalArbOpportunity],
        skip_counts: dict[str, int],
    ) -> None:
        if decision.action == "ENTER" and decision.opportunity is not None:
            opportunities.append(decision.opportunity)
            self.event_log.append_event(
                "conditional_arb_opportunity_detected",
                decision.opportunity.to_record(),
            )
            self.logger.info(
                "opportunity kind=%s id=%s net_profit=%.4f return_bps=%.2f",
                decision.opportunity.kind,
                decision.opportunity.opportunity_id,
                decision.opportunity.net_profit,
                decision.opportunity.net_return_bps,
            )
            return
        reason = decision.reason or "unknown"
        skip_counts[reason] = skip_counts.get(reason, 0) + 1


def build_parser() -> argparse.ArgumentParser:
    loaded = config.load_scan_config()
    parser = argparse.ArgumentParser(description="Scan Polymarket for paper-only conditional arbitrage")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    parser.add_argument("--limit", type=int, default=loaded.market_limit, help="Maximum tradable markets to scan")
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=loaded.poll_interval_seconds,
        help="Seconds between cycles in continuous mode",
    )
    parser.add_argument(
        "--min-net-profit-usd",
        type=float,
        default=loaded.min_net_profit_usd,
        help="Minimum net profit after fees, slippage buffer, and gas",
    )
    parser.add_argument(
        "--min-net-return-bps",
        type=float,
        default=loaded.min_net_return_bps,
        help="Minimum net return on capital in basis points",
    )
    parser.add_argument(
        "--max-capital-usd",
        type=float,
        default=loaded.max_capital_usd,
        help="Maximum paper capital to allocate per opportunity",
    )
    parser.add_argument("--include-neg-risk", dest="include_neg_risk", action="store_true", default=None)
    parser.add_argument("--no-neg-risk", dest="include_neg_risk", action="store_false")
    parser.add_argument("--json", action="store_true", help="Print cycle result as JSON")
    parser.add_argument("--data-dir", type=Path, default=loaded.data_dir, help="Data output directory")
    parser.add_argument("--clob-host", default=loaded.clob_host, help="CLOB host override")
    return parser


def _config_from_args(args: argparse.Namespace) -> config.ScanConfig:
    loaded = config.load_scan_config()
    include_neg_risk = loaded.include_neg_risk if args.include_neg_risk is None else bool(args.include_neg_risk)
    data_dir = Path(args.data_dir)
    return replace(
        loaded,
        data_dir=data_dir,
        log_dir=config.log_dir(),
        clob_host=str(args.clob_host).rstrip("/"),
        market_limit=args.limit if args.limit and args.limit > 0 else None,
        poll_interval_seconds=max(1, int(args.poll_interval_seconds)),
        min_net_profit_usd=max(0.0, float(args.min_net_profit_usd)),
        min_net_return_bps=max(0.0, float(args.min_net_return_bps)),
        max_capital_usd=max(0.01, float(args.max_capital_usd)),
        include_neg_risk=include_neg_risk,
    )


def main(argv: list[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)
    scan_config = _config_from_args(args)
    logger = setup_logging(scan_config)
    scanner = ConditionalArbScanner(scan_config=scan_config, logger=logger)
    if args.once:
        result = scanner.run_once()
        if args.json:
            print(json.dumps(jsonable(result), sort_keys=True))
    else:
        scanner.run_forever()


if __name__ == "__main__":
    main()
