from __future__ import annotations

import argparse
import logging
import signal
import time
from dataclasses import asdict
from datetime import datetime, timezone

from . import config
from .calibration import load_calibration
from .forecast import flush_cache
from .ledger import PositionLedger
from .live_fetcher import LiveFetcher
from .order_placer import OrderPlacer
from .strategy import evaluate_market


def setup_logging() -> logging.Logger:
    config.LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("weather_arb_live")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()

    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    file_handler = logging.FileHandler(config.LOG_DIR / "live_bot.log", encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)
    return logger


class LiveBot:
    def __init__(
        self,
        *,
        fetcher: LiveFetcher | None = None,
        order_placer: OrderPlacer | None = None,
        ledger: PositionLedger | None = None,
        logger: logging.Logger | None = None,
    ):
        self.runtime = config.load_runtime_config()
        self.logger = logger or logging.getLogger("weather_arb_live")
        self.fetcher = fetcher or LiveFetcher(clob_host=self.runtime.clob_host)
        self.order_placer = order_placer or OrderPlacer(
            clob_host=self.runtime.clob_host,
            dry_run=self.runtime.dry_run,
        )
        self.ledger = ledger or PositionLedger()
        self.calibration = None
        self.running = True

    def bootstrap(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger.load()
        self.calibration = load_calibration()
        self.logger.info(
            "startup dry_run=%s clob_host=%s poll_interval_seconds=%s positions=%s",
            self.runtime.dry_run,
            self.runtime.clob_host,
            self.runtime.poll_interval_seconds,
            len(self.ledger.positions),
        )
        self._log_artifact_status()

    def _log_artifact_status(self) -> None:
        artifacts = {
            "empirical_residuals": config.RESIDUALS_CACHE_PATH,
            "sigma_cache": config.SIGMA_CACHE_PATH,
            "calibration_table": config.CALIBRATION_PATH,
        }
        for name, path in artifacts.items():
            if path.exists():
                self.logger.info("artifact_loaded name=%s path=%s", name, path)
            else:
                self.logger.warning("artifact_missing name=%s path=%s", name, path)
        if self.calibration is None:
            self.logger.warning("calibration_disabled reason=missing_or_unreadable_table")

    def install_signal_handlers(self) -> None:
        def _stop(signum, _frame):
            self.logger.info("shutdown_signal signal=%s", signum)
            self.running = False

        signal.signal(signal.SIGINT, _stop)
        signal.signal(signal.SIGTERM, _stop)

    def run_forever(self) -> None:
        self.bootstrap()
        self.install_signal_handlers()
        try:
            while self.running:
                self.run_one_cycle()
                if not self.running:
                    break
                time.sleep(self.runtime.poll_interval_seconds)
        finally:
            self.shutdown()

    def run_once(self) -> None:
        self.bootstrap()
        try:
            self.run_one_cycle()
        finally:
            self.shutdown()

    def run_one_cycle(self) -> None:
        cycle_started = datetime.now(timezone.utc)
        self.logger.info("cycle_start at=%s", cycle_started.isoformat())
        try:
            markets = self.fetcher.fetch_active_markets(limit=config.live_market_limit())
        except Exception as exc:
            self.logger.exception("cycle_fetch_error error=%s", exc)
            return

        entered_positions = self.ledger.entered_positions(include_dry_run=self.runtime.dry_run)
        self.logger.info("cycle_markets count=%s", len(markets))
        for market in markets:
            market_id = str(market.get("id") or market.get("conditionId") or "")
            try:
                preflight = evaluate_market(
                    market,
                    None,
                    as_of=cycle_started,
                    entered_positions=entered_positions,
                    calibration=self.calibration,
                    max_position_usd=self.runtime.max_position_usd,
                )
                if preflight.reason != "missing_live_price":
                    self.logger.info(
                        "decision_skip market_id=%s reason=%s details=%s",
                        market_id,
                        preflight.reason,
                        preflight.details,
                    )
                    continue

                token_id = preflight.details["token_id"]
                try:
                    yes_midpoint = self.fetcher.fetch_yes_midpoint(token_id)
                except Exception as exc:
                    self.logger.exception(
                        "price_error market_id=%s token_id=%s error=%s",
                        market_id,
                        token_id,
                        exc,
                    )
                    continue
                if yes_midpoint is None:
                    self.logger.info(
                        "decision_skip market_id=%s token_id=%s reason=missing_two_sided_book",
                        market_id,
                        token_id,
                    )
                    continue

                decision = evaluate_market(
                    market,
                    yes_midpoint,
                    as_of=cycle_started,
                    entered_positions=entered_positions,
                    calibration=self.calibration,
                    max_position_usd=self.runtime.max_position_usd,
                )
                if decision.action == "SKIP":
                    self.logger.info(
                        "decision_skip market_id=%s reason=%s details=%s",
                        market_id,
                        decision.reason,
                        decision.details,
                    )
                    continue

                assert decision.plan is not None
                result = self.order_placer.place_yes_order(
                    token_id=decision.plan.token_id,
                    market_price=decision.plan.market_price,
                    position_usd=decision.plan.position_usd,
                )
                self.ledger.record(
                    decision.plan,
                    dry_run=self.runtime.dry_run,
                    order_response=result.response,
                )
                entered_positions[decision.plan.market_id] = asdict(decision.plan)
                self.logger.info(
                    "decision_enter market_id=%s token_id=%s price=%.4f entry=%.4f "
                    "shares=%.4f position_usd=%.2f forecast_prob=%.4f edge=%.4f posted=%s",
                    decision.plan.market_id,
                    decision.plan.token_id,
                    decision.plan.market_price,
                    decision.plan.entry_price,
                    decision.plan.shares,
                    decision.plan.position_usd,
                    decision.plan.forecast_prob,
                    decision.plan.edge,
                    result.posted,
                )
            except Exception as exc:
                self.logger.exception("market_error market_id=%s error=%s", market_id, exc)
        self.ledger.save()
        self.logger.info("cycle_end positions=%s", len(self.ledger.positions))

    def shutdown(self) -> None:
        try:
            flush_cache()
        except Exception as exc:
            self.logger.warning("flush_cache_failed error=%s", exc)
        self.ledger.save()
        self.logger.info("shutdown_complete positions=%s", len(self.ledger.positions))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Run the standalone Polymarket weather live bot")
    parser.add_argument("--once", action="store_true", help="Run one poll cycle and exit")
    args = parser.parse_args(argv)

    logger = setup_logging()
    bot = LiveBot(logger=logger)
    if args.once:
        bot.run_once()
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
