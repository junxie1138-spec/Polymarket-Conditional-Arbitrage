from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any

from . import config
from .arb_models import BinaryMarket
from .arb_strategy import ArbStrategyParams, evaluate_binary_merge_arbitrage
from .event_log import LiveEventLog, utc_iso
from .live_fetcher import LiveFetcher
from .paper import PaperMergeLedger, PaperTradingEngine


class LiveTradingDisabledError(RuntimeError):
    pass


def setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("weather_arb_live.merge_arb")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(config.LOG_DIR / "merge_arb_bot.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


class BinaryMergeArbBot:
    def __init__(
        self,
        *,
        fetcher: LiveFetcher | None = None,
        ledger: PaperMergeLedger | None = None,
        logger: logging.Logger | None = None,
        event_log: LiveEventLog | None = None,
        params: ArbStrategyParams | None = None,
    ):
        self.fetcher = fetcher or LiveFetcher()
        self.ledger = ledger or PaperMergeLedger()
        self.logger = logger or logging.getLogger("weather_arb_live.merge_arb")
        self.event_log = event_log or LiveEventLog()
        self.params = params or ArbStrategyParams.from_config()
        self.paper_engine = PaperTradingEngine(self.ledger)
        self.running = True

    def bootstrap(self) -> None:
        if config.merge_arb_live_trading_enabled():
            raise LiveTradingDisabledError(
                "MERGE_ARB_LIVE_TRADING_ENABLED is not supported in v1; "
                "binary merge arbitrage runs in paper mode only until paired "
                "execution and onchain merge handling are implemented."
            )
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger.load()
        self.logger.info(
            "startup mode=paper min_profit=%.4f min_return_bps=%.2f max_position_usd=%.2f "
            "slippage_bps=%.2f gas=%.4f taker_fee_bps=%.2f positions=%s",
            self.params.min_net_profit_usd,
            self.params.min_net_return_bps,
            self.params.max_paper_position_usd,
            self.params.slippage_buffer_bps,
            self.params.gas_cost_usd,
            self.params.taker_fee_bps,
            len(self.ledger.positions),
        )
        self.event_log.append_event(
            "merge_arb_bot_started",
            {
                "mode": "paper",
                "min_net_profit_usd": self.params.min_net_profit_usd,
                "min_net_return_bps": self.params.min_net_return_bps,
                "max_paper_position_usd": self.params.max_paper_position_usd,
                "positions": len(self.ledger.positions),
            },
        )

    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            self.logger.info("shutdown_signal signal=%s", signum)
            self.running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    def run_once(self) -> bool:
        self.bootstrap()
        try:
            return self.run_one_cycle()
        finally:
            self.shutdown()

    def run_forever(self) -> None:
        self.bootstrap()
        self.install_signal_handlers()
        try:
            while self.running:
                ok = self.run_one_cycle()
                sleep_seconds = (
                    config.poll_interval_seconds()
                    if ok
                    else config.offline_retry_seconds()
                )
                time.sleep(sleep_seconds)
        finally:
            self.shutdown()

    def run_one_cycle(self) -> bool:
        cycle_started = datetime.now(timezone.utc)
        self.logger.info("cycle_start at=%s", cycle_started.isoformat())
        self.event_log.append_event(
            "merge_arb_cycle_started",
            {"cycle_started_at_utc": utc_iso(cycle_started)},
        )
        try:
            markets = self.fetcher.fetch_active_markets(
                tag_slug=None,
                limit=config.live_market_limit(),
            )
        except Exception as exc:
            self.logger.exception("cycle_fetch_error error=%s", exc)
            self.event_log.append_event(
                "merge_arb_cycle_failed",
                {
                    "cycle_started_at_utc": utc_iso(cycle_started),
                    "stage": "fetch_active_markets",
                    "error": str(exc),
                },
            )
            return False

        scanned = 0
        binary_markets = 0
        entered = 0
        skipped: dict[str, int] = {}
        entered_positions = self.ledger.entered_positions()
        for raw_market in markets:
            scanned += 1
            market = BinaryMarket.from_gamma_market(raw_market)
            if market is None:
                skipped["invalid_binary_mapping"] = skipped.get("invalid_binary_mapping", 0) + 1
                continue
            binary_markets += 1
            try:
                yes_asks, no_asks = self.fetcher.fetch_binary_ask_books(market)
                evaluation_time = datetime.now(timezone.utc)
                decision = evaluate_binary_merge_arbitrage(
                    market,
                    yes_asks,
                    no_asks,
                    as_of=evaluation_time,
                    entered_positions=entered_positions,
                    params=self.params,
                )
                if decision.action != "ENTER":
                    reason = decision.reason or "unknown"
                    skipped[reason] = skipped.get(reason, 0) + 1
                    self._log_skip(market, decision.reason, decision.details)
                    continue
                assert decision.opportunity is not None
                row = self.paper_engine.execute(decision.opportunity, as_of=cycle_started)
                entered_positions[market.market_id] = row
                entered += 1
                self._log_entry(row)
            except Exception as exc:
                skipped["market_error"] = skipped.get("market_error", 0) + 1
                self.logger.exception("market_error market_id=%s error=%s", market.market_id, exc)
                self.event_log.append_event(
                    "merge_arb_market_error",
                    {
                        "market_id": market.market_id,
                        "condition_id": market.condition_id,
                        "error": str(exc),
                    },
                )

        self.ledger.save()
        self.logger.info(
            "cycle_end scanned=%s binary=%s entered=%s skipped=%s positions=%s",
            scanned,
            binary_markets,
            entered,
            skipped,
            len(self.ledger.positions),
        )
        self.event_log.append_event(
            "merge_arb_cycle_completed",
            {
                "cycle_started_at_utc": utc_iso(cycle_started),
                "markets_scanned": scanned,
                "binary_markets": binary_markets,
                "paper_positions_entered": entered,
                "skip_counts": skipped,
                "positions": len(self.ledger.positions),
            },
        )
        return True

    def _log_skip(self, market: BinaryMarket, reason: str | None, details: dict[str, Any]) -> None:
        self.logger.info(
            "decision_skip market_id=%s reason=%s details=%s",
            market.market_id,
            reason,
            details,
        )

    def _log_entry(self, row: dict[str, Any]) -> None:
        self.logger.info(
            "paper_enter market_id=%s size=%.4f gross_cost=%.4f realized_pnl=%.4f return_bps=%.2f",
            row.get("market_id"),
            row.get("merged_quantity") or 0.0,
            row.get("gross_cost") or 0.0,
            row.get("realized_pnl") or 0.0,
            row.get("net_return_bps") or 0.0,
        )
        self.event_log.append_event(
            "merge_arb_paper_position_opened",
            {
                "market_id": row.get("market_id"),
                "condition_id": row.get("condition_id"),
                "question": row.get("question"),
                "yes_token_id": row.get("yes_token_id"),
                "no_token_id": row.get("no_token_id"),
                "merged_quantity": row.get("merged_quantity"),
                "gross_cost": row.get("gross_cost"),
                "estimated_fees": row.get("estimated_fees"),
                "gas_cost": row.get("gas_cost"),
                "slippage_buffer": row.get("slippage_buffer"),
                "realized_pnl": row.get("realized_pnl"),
                "net_return_bps": row.get("net_return_bps"),
            },
        )

    def shutdown(self) -> None:
        self.ledger.save()
        self.logger.info("shutdown_complete positions=%s", len(self.ledger.positions))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the Polymarket binary merge arbitrage paper bot")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    args = parser.parse_args(argv)

    logger = setup_logging()
    bot = BinaryMergeArbBot(logger=logger)
    if args.once:
        bot.run_once()
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
