from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import datetime, timezone

from . import config, network
from .calibration import load_calibration
from .forecast import flush_cache
from .ledger import PositionLedger
from .live_fetcher import LiveFetcher
from .order_placer import OrderPlacer
from .reconciliation import Reconciler
from .strategy import Decision, evaluate_market


YES_TO_NO_FALLBACK_REASONS = frozenset(
    {
        "below_min_entry_price",
        "below_min_forecast_probability",
        "below_min_edge",
        "calibration_rejected",
        "missing_two_sided_book",
    }
)


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
        self.reconciler = Reconciler(
            fetcher=self.fetcher,
            order_placer=self.order_placer,
            ledger=self.ledger,
            logger_=self.logger,
        )
        self.reconciliation_ready = self.runtime.dry_run or not self.runtime.reconcile_on_startup
        self.calibration = None
        self.running = True

    def bootstrap(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger.load()
        self.calibration = load_calibration()
        self.logger.info(
            "startup model=%s variant=%s no_side=%s dry_run=%s clob_host=%s "
            "poll_interval_seconds=%s offline_retry_seconds=%s reconcile_on_startup=%s positions=%s",
            self.runtime.model_name,
            self.runtime.model_variant,
            self.runtime.enable_no_side,
            self.runtime.dry_run,
            self.runtime.clob_host,
            self.runtime.poll_interval_seconds,
            self.runtime.offline_retry_seconds,
            self.runtime.reconcile_on_startup,
            len(self.ledger.positions),
        )
        self._log_artifact_status()
        if self.runtime.reconcile_on_startup and not self.runtime.dry_run:
            self._ensure_reconciled()

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
                try:
                    cycle_ok = self.run_one_cycle()
                except Exception as exc:
                    cycle_ok = False
                    self.logger.exception("cycle_unhandled_error error=%s", exc)
                if not self.running:
                    break
                sleep_seconds = (
                    self.runtime.poll_interval_seconds
                    if cycle_ok
                    else self.runtime.offline_retry_seconds
                )
                if not cycle_ok:
                    self.logger.warning("cycle_retry_after seconds=%s", sleep_seconds)
                time.sleep(sleep_seconds)
        finally:
            self.shutdown()

    def run_once(self) -> None:
        self.bootstrap()
        try:
            self.run_one_cycle()
        finally:
            self.shutdown()

    def run_one_cycle(self) -> bool:
        if not self._ensure_reconciled():
            return False

        cycle_started = datetime.now(timezone.utc)
        self.logger.info("cycle_start at=%s", cycle_started.isoformat())
        try:
            markets = self.fetcher.fetch_active_markets(limit=config.live_market_limit())
        except Exception as exc:
            self.logger.exception("cycle_fetch_error error=%s", exc)
            return False

        entered_positions = self.ledger.entered_positions(include_dry_run=self.runtime.dry_run)
        self.logger.info("cycle_markets count=%s", len(markets))
        for market in markets:
            market_id = str(market.get("id") or market.get("conditionId") or "")
            try:
                yes_decision = self._evaluate_side(
                    market=market,
                    side="YES",
                    cycle_started=cycle_started,
                    entered_positions=entered_positions,
                )
                if yes_decision.action == "ENTER":
                    self._record_entry(yes_decision, entered_positions)
                    continue

                if (
                    not self.runtime.enable_no_side
                    or yes_decision.reason not in YES_TO_NO_FALLBACK_REASONS
                ):
                    self._log_skip(market_id, yes_decision)
                    continue

                no_decision = self._evaluate_side(
                    market=market,
                    side="NO",
                    cycle_started=cycle_started,
                    entered_positions=entered_positions,
                )
                if no_decision.action == "ENTER":
                    self._record_entry(no_decision, entered_positions)
                    continue

                self.logger.info(
                    "decision_skip market_id=%s reason=no_qualified_side details=%s",
                    market_id,
                    {
                        "yes_reason": yes_decision.reason,
                        "yes_details": yes_decision.details,
                        "no_reason": no_decision.reason,
                        "no_details": no_decision.details,
                    },
                )
            except Exception as exc:
                self.logger.exception("market_error market_id=%s error=%s", market_id, exc)
        self.ledger.save()
        self.logger.info("cycle_end positions=%s", len(self.ledger.positions))
        return True

    def _ensure_reconciled(self) -> bool:
        if self.reconciliation_ready:
            return True
        try:
            self.reconciler.reconcile(market_limit=config.live_market_limit())
        except Exception as exc:
            self.logger.exception("startup_reconcile_error error=%s", exc)
            return False
        self.reconciliation_ready = True
        return True

    def _evaluate_side(
        self,
        *,
        market: dict,
        side: str,
        cycle_started: datetime,
        entered_positions: dict,
    ) -> Decision:
        preflight = evaluate_market(
            market,
            None,
            side=side,
            as_of=cycle_started,
            entered_positions=entered_positions,
            calibration=self.calibration,
            max_position_usd=self.runtime.max_position_usd,
        )
        if preflight.reason != "missing_live_price":
            return preflight

        market_id = str(market.get("id") or market.get("conditionId") or "")
        token_id = preflight.details["token_id"]
        try:
            midpoint = self.fetcher.fetch_midpoint(token_id)
        except Exception as exc:
            self.logger.exception(
                "price_error market_id=%s side=%s token_id=%s error=%s",
                market_id,
                side,
                token_id,
                exc,
            )
            return Decision.skip(
                "price_error",
                market_id=market_id,
                side=side,
                token_id=token_id,
                error=str(exc),
            )
        if midpoint is None:
            return Decision.skip(
                "missing_two_sided_book",
                market_id=market_id,
                side=side,
                token_id=token_id,
            )

        return evaluate_market(
            market,
            midpoint,
            side=side,
            as_of=cycle_started,
            entered_positions=entered_positions,
            calibration=self.calibration,
            max_position_usd=self.runtime.max_position_usd,
        )

    def _record_entry(self, decision: Decision, entered_positions: dict) -> None:
        assert decision.plan is not None
        plan = decision.plan
        try:
            result = self.order_placer.place_order(
                token_id=plan.token_id,
                market_price=plan.market_price,
                position_usd=plan.position_usd,
            )
        except Exception as exc:
            if not self.runtime.dry_run and network.is_retryable_exception(exc):
                row = self.ledger.record(
                    plan,
                    dry_run=False,
                    order_response={
                        "posted": "unknown",
                        "reason": "order_submission_interrupted",
                        "error": str(exc),
                    },
                )
                entered_positions[plan.market_id] = row
                self.ledger.save()
                self.logger.exception(
                    "order_submit_unknown market_id=%s side=%s token_id=%s error=%s",
                    plan.market_id,
                    plan.side,
                    plan.token_id,
                    exc,
                )
                return
            raise
        row = self.ledger.record(
            plan,
            dry_run=self.runtime.dry_run,
            order_response=result.response,
        )
        entered_positions[plan.market_id] = row
        self.ledger.save()
        self.logger.info(
            "decision_enter market_id=%s side=%s token_id=%s price=%.4f entry=%.4f "
            "shares=%.4f position_usd=%.2f forecast_prob=%.4f edge=%.4f posted=%s",
            plan.market_id,
            plan.side,
            plan.token_id,
            plan.market_price,
            plan.entry_price,
            plan.shares,
            plan.position_usd,
            plan.forecast_prob,
            plan.edge,
            result.posted,
        )

    def _log_skip(self, market_id: str, decision: Decision) -> None:
        self.logger.info(
            "decision_skip market_id=%s reason=%s details=%s",
            market_id,
            decision.reason,
            decision.details,
        )

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
