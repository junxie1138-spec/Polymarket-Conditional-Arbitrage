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
from .ws_stream import (
    BestBidAskCache,
    PolymarketMarketStream,
    PolymarketUserStream,
    unique_market_condition_ids,
    unique_market_token_ids,
)


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
        market_stream: PolymarketMarketStream | None = None,
        user_stream: PolymarketUserStream | None = None,
    ):
        self.runtime = config.load_runtime_config()
        self.logger = logger or logging.getLogger("weather_arb_live")
        self.price_cache = BestBidAskCache()
        self.fetcher = fetcher or LiveFetcher(
            clob_host=self.runtime.clob_host,
            price_cache=self.price_cache,
            ws_stale_seconds=self.runtime.ws_market_stale_seconds,
        )
        if fetcher is not None and getattr(fetcher, "price_cache", None) is not None:
            self.price_cache = fetcher.price_cache
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
        self.market_stream = market_stream
        if self.market_stream is None and self.runtime.market_ws_enabled:
            self.market_stream = PolymarketMarketStream(
                cache=self.price_cache,
                base_url=self.runtime.polymarket_ws_base_url,
                logger_=self.logger,
                max_tokens=self.runtime.ws_market_max_tokens,
            )
        self.user_stream = user_stream
        if (
            self.user_stream is None
            and self.runtime.user_ws_enabled
            and not self.runtime.dry_run
        ):
            if PolymarketUserStream.credentials_ready():
                self.user_stream = PolymarketUserStream(
                    base_url=self.runtime.polymarket_ws_base_url,
                    logger_=self.logger,
                )
            else:
                self.logger.warning("user_ws_disabled reason=missing_api_credentials")
        self._last_market_stream_tokens: tuple[str, ...] = ()
        self._last_user_stream_markets: tuple[str, ...] = ()
        self._next_safety_reconcile_at = time.monotonic() + self.runtime.safety_reconcile_interval_seconds
        self._last_reconnect_reconcile_at = 0.0
        self._seen_stream_reconnects = 0
        self.calibration = None
        self.running = True

    def bootstrap(self) -> None:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ledger.load()
        self.calibration = load_calibration()
        self.logger.info(
            "startup model=%s variant=%s no_side=%s dry_run=%s clob_host=%s "
            "poll_interval_seconds=%s offline_retry_seconds=%s reconcile_on_startup=%s "
            "safety_reconcile_interval_seconds=%s market_ws_enabled=%s user_ws_enabled=%s "
            "ws_market_stale_seconds=%s ws_market_max_tokens=%s max_position_usd=%.2f positions=%s",
            self.runtime.model_name,
            self.runtime.model_variant,
            self.runtime.enable_no_side,
            self.runtime.dry_run,
            self.runtime.clob_host,
            self.runtime.poll_interval_seconds,
            self.runtime.offline_retry_seconds,
            self.runtime.reconcile_on_startup,
            self.runtime.safety_reconcile_interval_seconds,
            self.runtime.market_ws_enabled,
            bool(self.user_stream),
            self.runtime.ws_market_stale_seconds,
            self.runtime.ws_market_max_tokens,
            self.runtime.max_position_usd,
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

        self.logger.info("cycle_markets count=%s", len(markets))
        self._sync_stream_subscriptions(markets)
        if not self._ensure_periodic_safety_reconcile(markets):
            return False
        if not self._ensure_reconciled_after_stream_reconnect(markets):
            return False
        entered_positions = self.ledger.entered_positions(include_dry_run=self.runtime.dry_run)
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
            self.reconciler.reconcile(market_limit=config.live_market_limit(), reason="startup")
        except Exception as exc:
            self.logger.exception("startup_reconcile_error error=%s", exc)
            return False
        self.reconciliation_ready = True
        self._mark_safety_reconciled()
        return True

    def _mark_safety_reconciled(self) -> None:
        interval = self.runtime.safety_reconcile_interval_seconds
        self._next_safety_reconcile_at = time.monotonic() + interval if interval > 0 else float("inf")

    def _sync_stream_subscriptions(self, markets: list[dict]) -> None:
        token_signature = tuple(
            unique_market_token_ids(markets, max_tokens=self.runtime.ws_market_max_tokens)
        )
        if self.market_stream is not None and (token_signature or self._last_market_stream_tokens):
            changed = token_signature != self._last_market_stream_tokens
            try:
                self.market_stream.set_market_candidates(markets)
                if changed:
                    self.market_stream.warmup(self.runtime.ws_market_warmup_seconds)
                self._last_market_stream_tokens = token_signature
            except Exception as exc:
                self.logger.warning("market_ws_subscription_error error=%s", exc)

        condition_signature = tuple(unique_market_condition_ids(markets))
        if (
            self.user_stream is not None
            and not self.runtime.dry_run
            and (condition_signature or self._last_user_stream_markets)
        ):
            try:
                self.user_stream.set_market_candidates(markets)
                self._last_user_stream_markets = condition_signature
            except Exception as exc:
                self.logger.warning("user_ws_subscription_error error=%s", exc)

    def _ensure_periodic_safety_reconcile(self, markets: list[dict]) -> bool:
        if self.runtime.dry_run or self.runtime.safety_reconcile_interval_seconds <= 0:
            return True
        if time.monotonic() < self._next_safety_reconcile_at:
            return True
        return self._run_reconciliation(active_markets=markets, reason="periodic_safety")

    def _ensure_reconciled_after_stream_reconnect(self, markets: list[dict]) -> bool:
        if self.runtime.dry_run:
            return True
        reconnect_count = 0
        for stream in (self.market_stream, self.user_stream):
            reconnect_count += int(getattr(stream, "reconnect_count", 0) or 0)
        if reconnect_count <= self._seen_stream_reconnects:
            return True

        now = time.monotonic()
        min_interval = self.runtime.safety_reconcile_min_interval_seconds
        if now - self._last_reconnect_reconcile_at < min_interval:
            self.logger.warning(
                "stream_reconnect_reconcile_deferred reconnects=%s min_interval_seconds=%s",
                reconnect_count,
                min_interval,
            )
            return True
        ok = self._run_reconciliation(active_markets=markets, reason="stream_reconnect")
        if ok:
            self._seen_stream_reconnects = reconnect_count
            self._last_reconnect_reconcile_at = now
        return ok

    def _run_reconciliation(self, *, active_markets: list[dict], reason: str) -> bool:
        try:
            self.reconciler.reconcile(
                active_markets=active_markets,
                market_limit=config.live_market_limit(),
                reason=reason,
            )
        except Exception as exc:
            self.logger.exception("%s_reconcile_error error=%s", reason, exc)
            return False
        self._mark_safety_reconciled()
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
        for stream_name, stream in (("user", self.user_stream), ("market", self.market_stream)):
            if stream is None or not hasattr(stream, "stop"):
                continue
            try:
                stream.stop()
            except Exception as exc:
                self.logger.warning("%s_ws_stop_failed error=%s", stream_name, exc)
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
