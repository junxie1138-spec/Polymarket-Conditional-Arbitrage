from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import datetime, timezone
from typing import Any

from . import config, network
from .calibration import load_calibration
from .event_log import LiveEventLog, first_float, first_str, order_lifecycle_events_from_payload, utc_iso
from .forecast import _fetch_forecast_window, estimate_forecast_prob, flush_cache
from .ledger import PositionLedger
from .live_fetcher import LiveFetcher
from .market_parser import _parse_end_date, parse_market_question
from .order_placer import OrderPlacer, build_order_intent
from .reconciliation import Reconciler
from .strategy import (
    Decision,
    entered_position_for_market,
    evaluate_market,
    market_volume_usd,
    resolution_datetime,
    token_from_market,
    token_ids_from_market,
)
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
        event_log: LiveEventLog | None = None,
    ):
        self.runtime = config.load_runtime_config()
        self.logger = logger or logging.getLogger("weather_arb_live")
        self.event_log = event_log or LiveEventLog()
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
            event_log=self.event_log,
        )
        self.reconciliation_ready = self.runtime.dry_run or not self.runtime.reconcile_on_startup
        self.market_stream = market_stream
        if self.market_stream is None and self.runtime.market_ws_enabled:
            self.market_stream = PolymarketMarketStream(
                cache=self.price_cache,
                base_url=self.runtime.polymarket_ws_base_url,
                logger_=self.logger,
                max_tokens=self.runtime.ws_market_max_tokens,
                event_log=self.event_log,
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
                    event_log=self.event_log,
                )
            else:
                self.logger.warning("user_ws_disabled reason=missing_api_credentials")
        self._last_market_stream_tokens: tuple[str, ...] = ()
        self._last_user_stream_markets: tuple[str, ...] = ()
        self._next_safety_reconcile_at = time.monotonic() + self.runtime.safety_reconcile_interval_seconds
        self._last_reconnect_reconcile_at = 0.0
        self._seen_stream_reconnects = 0
        self._quote_context: dict[tuple[str, str], dict[str, Any]] = {}
        self._touched_market_context: dict[str, dict[str, Any]] = {}
        self._last_snapshot_at: dict[str, datetime] = {}
        self._last_active_markets: list[dict] = []
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
        self.event_log.append_event(
            "bot_started",
            {
                "dry_run": self.runtime.dry_run,
                "model_name": self.runtime.model_name,
                "model_variant": self.runtime.model_variant,
                "clob_host": self.runtime.clob_host,
                "poll_interval_seconds": self.runtime.poll_interval_seconds,
                "max_position_usd": self.runtime.max_position_usd,
                "positions": len(self.ledger.positions),
            },
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
                self._sleep_with_snapshot_ticks(sleep_seconds)
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
        self.event_log.append_event("cycle_started", {"cycle_started_at_utc": utc_iso(cycle_started)})
        try:
            markets = self.fetcher.fetch_active_markets(limit=config.live_market_limit())
        except Exception as exc:
            self.logger.exception("cycle_fetch_error error=%s", exc)
            self.event_log.append_event(
                "cycle_failed",
                {
                    "cycle_started_at_utc": utc_iso(cycle_started),
                    "stage": "fetch_active_markets",
                    "error": str(exc),
                },
            )
            return False

        self.logger.info("cycle_markets count=%s", len(markets))
        self._last_active_markets = markets
        self._sync_stream_subscriptions(markets)
        if not self._ensure_periodic_safety_reconcile(markets):
            return False
        if not self._ensure_reconciled_after_stream_reconnect(markets):
            return False
        entered_positions = self.ledger.entered_positions(include_dry_run=self.runtime.dry_run)
        self._touch_existing_position_markets(markets, entered_positions)
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
                    self._record_entry(yes_decision, entered_positions, market=market)
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
                    self._record_entry(no_decision, entered_positions, market=market)
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
                self.event_log.append_event(
                    "decision_skipped",
                    {
                        "market_id": market_id,
                        "skip_reason": "no_qualified_side",
                        "decision_details": {
                            "yes_reason": yes_decision.reason,
                            "yes_details": yes_decision.details,
                            "no_reason": no_decision.reason,
                            "no_details": no_decision.details,
                        },
                    },
                )
            except Exception as exc:
                self.logger.exception("market_error market_id=%s error=%s", market_id, exc)
                self.event_log.append_event(
                    "market_evaluation_failed",
                    {
                        "market_id": market_id,
                        "error": str(exc),
                    },
                )
        self._record_due_snapshots(markets=markets, as_of=cycle_started)
        self.ledger.save()
        self.logger.info("cycle_end positions=%s", len(self.ledger.positions))
        self.event_log.append_event(
            "cycle_completed",
            {
                "cycle_started_at_utc": utc_iso(cycle_started),
                "markets_scanned": len(markets),
                "positions": len(self.ledger.positions),
            },
        )
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
            quote = self._fetch_quote(token_id)
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
        quote_payload = self._quote_payload(quote)
        midpoint = quote_payload.get("midpoint")
        if midpoint is None:
            return Decision.skip(
                "missing_two_sided_book",
                market_id=market_id,
                side=side,
                token_id=token_id,
            )
        self._quote_context[(market_id, side)] = quote_payload

        return evaluate_market(
            market,
            midpoint,
            side=side,
            as_of=cycle_started,
            entered_positions=entered_positions,
            calibration=self.calibration,
            max_position_usd=self.runtime.max_position_usd,
        )

    def _fetch_quote(self, token_id: str):
        if hasattr(self.fetcher, "fetch_quote"):
            return self.fetcher.fetch_quote(token_id)
        midpoint = self.fetcher.fetch_midpoint(token_id)
        if midpoint is None:
            return None
        return {
            "token_id": token_id,
            "best_bid": None,
            "best_ask": None,
            "midpoint": midpoint,
            "source": "midpoint_fallback",
            "updated_at": None,
        }

    @staticmethod
    def _quote_payload(quote) -> dict[str, Any]:
        if quote is None:
            return {}

        def get(name: str):
            if isinstance(quote, dict):
                return quote.get(name)
            return getattr(quote, name, None)

        updated_at = get("updated_at")
        updated_at_utc = None
        if updated_at is not None:
            try:
                updated_at_utc = utc_iso(datetime.fromtimestamp(float(updated_at), tz=timezone.utc))
            except (OSError, TypeError, ValueError):
                updated_at_utc = None
        return {
            "token_id": get("token_id"),
            "best_bid": get("best_bid"),
            "best_ask": get("best_ask"),
            "midpoint": get("midpoint"),
            "quote_source": get("source"),
            "quote_updated_at_utc": updated_at_utc,
        }

    def _place_order_with_event_hook(self, plan, on_submit_attempt):
        kwargs = {
            "token_id": plan.token_id,
            "market_price": plan.market_price,
            "position_usd": plan.position_usd,
        }
        if self._order_placer_supports_submit_hook():
            kwargs["on_submit_attempt"] = on_submit_attempt
        return self.order_placer.place_order(**kwargs)

    def _order_placer_supports_submit_hook(self) -> bool:
        import inspect

        try:
            parameters = inspect.signature(self.order_placer.place_order).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.name == "on_submit_attempt" or parameter.kind == inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )

    def _plan_event_payload(
        self,
        plan,
        *,
        quote_payload: dict[str, Any],
        market: dict | None,
    ) -> dict[str, Any]:
        condition_id = str((market or {}).get("conditionId") or getattr(plan, "condition_id", None) or "") or None
        midpoint = quote_payload.get("midpoint", plan.market_price)
        best_bid = quote_payload.get("best_bid")
        best_ask = quote_payload.get("best_ask")
        spread = best_ask - best_bid if best_bid is not None and best_ask is not None else None
        raw_edge_at_midpoint = plan.forecast_prob - midpoint if midpoint is not None else None
        entry_spread_cost = plan.entry_price - midpoint if midpoint is not None else None
        return {
            "market_id": plan.market_id,
            "condition_id": condition_id,
            "token_id": plan.token_id,
            "city": plan.city,
            "target_date": plan.target_date,
            "bracket": self._bracket_from_plan(plan, market),
            "side": plan.side,
            "question": plan.question,
            "model_probability": plan.forecast_prob,
            "intended_edge": plan.edge,
            "model_edge_at_midpoint": raw_edge_at_midpoint,
            "entry_spread_cost": entry_spread_cost,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": spread,
            "submitted_limit_price": plan.entry_price,
            "submitted_quantity": plan.shares,
            "position_usd": plan.position_usd,
            "lead_days": plan.lead_days,
            "model_name": self.runtime.model_name,
            "model_variant": self.runtime.model_variant,
            **{key: value for key, value in quote_payload.items() if key not in {"token_id", "midpoint", "best_bid", "best_ask"}},
        }

    def _intent_event_payload(self, intent) -> dict[str, Any]:
        return {
            "submitted_limit_price": intent.limit_price,
            "submitted_quantity": intent.shares,
            "position_usd": intent.position_usd,
            "order_side": intent.side,
            "order_type": intent.order_type,
            "market_price": intent.market_price,
        }

    @staticmethod
    def _order_response_event_payload(response: dict | None) -> dict[str, Any]:
        if not isinstance(response, dict):
            return {}
        return {
            "exchange_order_id": first_str(response, ("order_id", "orderID", "id", "hash")),
            "exchange_status": first_str(response, ("status",)),
            "filled_price": first_float(response, ("matched_price", "average_price", "avgPrice", "filled_price")),
            "fill_quantity": first_float(response, ("size_matched", "matched_size", "filled_size", "fill_quantity")),
            "fees": first_float(response, ("fee", "fees")),
            "remaining_quantity": first_float(response, ("remaining_size",)),
        }

    def _record_order_response_lifecycle_events(
        self,
        base_payload: dict[str, Any],
        response: dict | None,
    ) -> None:
        if not isinstance(response, dict):
            return
        for event_type, event_payload in order_lifecycle_events_from_payload(response):
            if event_type == "order_acknowledged":
                continue
            self.event_log.append_event(
                event_type,
                {
                    **base_payload,
                    **event_payload,
                    "source": "order_response",
                },
            )

    def _touch_existing_position_markets(self, markets: list[dict], entered_positions: dict) -> None:
        for market in markets:
            row = entered_position_for_market(market, entered_positions)
            if row is not None:
                self._touch_market_from_row(row, market=market)

    def _touch_market_from_plan(
        self,
        plan,
        *,
        market: dict | None,
        quote_payload: dict[str, Any],
    ) -> None:
        context = {
            **self._plan_event_payload(plan, quote_payload=quote_payload, market=market),
            "shares": plan.shares,
            "entry_price": plan.entry_price,
        }
        self._touched_market_context[plan.market_id] = context
        self.event_log.remember_market_context(context)

    def _touch_market_from_row(self, row: dict[str, Any], *, market: dict | None) -> None:
        market_id = str(row.get("market_id") or (market or {}).get("id") or (market or {}).get("conditionId") or "")
        if not market_id:
            return
        side = row.get("side")
        token_id = row.get("token_id") or (token_from_market(market, side) if market and side in {"YES", "NO"} else None)
        context = {
            "market_id": market_id,
            "condition_id": row.get("condition_id") or (market or {}).get("conditionId"),
            "token_id": token_id,
            "city": row.get("city"),
            "target_date": row.get("target_date"),
            "bracket": self._bracket_from_row(row, market=market),
            "side": side,
            "question": row.get("question") or (market or {}).get("question"),
            "model_probability": row.get("forecast_prob"),
            "intended_edge": row.get("edge"),
            "submitted_limit_price": row.get("entry_price"),
            "submitted_quantity": row.get("shares"),
            "position_usd": row.get("position_usd"),
            "shares": row.get("shares"),
            "entry_price": row.get("entry_price"),
            "model_name": self.runtime.model_name,
            "model_variant": self.runtime.model_variant,
        }
        self._touched_market_context[market_id] = context
        self.event_log.remember_market_context(context)

    def _record_due_snapshots(self, *, markets: list[dict], as_of: datetime) -> None:
        interval = self.runtime.event_snapshot_interval_seconds
        if interval <= 0 or not self._touched_market_context:
            return
        by_market_id, by_condition_id, by_token_id = self._market_indexes(markets)
        now = datetime.now(timezone.utc)
        for market_id, context in list(self._touched_market_context.items()):
            last_snapshot_at = self._last_snapshot_at.get(market_id)
            if last_snapshot_at is not None and (now - last_snapshot_at).total_seconds() < interval:
                continue
            market = self._find_touched_market(context, by_market_id, by_condition_id, by_token_id)
            if market is None:
                continue
            try:
                market_snapshot = self._market_snapshot_payload(market, context, as_of=now)
                self.event_log.append_market_snapshot(market_snapshot, timestamp_utc=now)
                forecast_snapshot = self._forecast_snapshot_payload(market, context, as_of=as_of)
                if forecast_snapshot is not None:
                    self.event_log.append_forecast_snapshot(forecast_snapshot, timestamp_utc=now)
                self._last_snapshot_at[market_id] = now
            except Exception as exc:
                self.logger.warning("event_snapshot_failed market_id=%s error=%s", market_id, exc)

    def _sleep_with_snapshot_ticks(self, sleep_seconds: float) -> None:
        interval = self.runtime.event_snapshot_interval_seconds
        if interval <= 0 or not self._last_active_markets or not self._touched_market_context:
            time.sleep(sleep_seconds)
            return

        deadline = time.monotonic() + sleep_seconds
        while self.running:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, max(1.0, interval)))
            if not self.running or not self._last_active_markets:
                continue
            self._record_due_snapshots(
                markets=self._last_active_markets,
                as_of=datetime.now(timezone.utc),
            )

    @staticmethod
    def _market_indexes(markets: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, dict]]:
        by_market_id: dict[str, dict] = {}
        by_condition_id: dict[str, dict] = {}
        by_token_id: dict[str, dict] = {}
        for market in markets:
            market_id = str(market.get("id") or market.get("conditionId") or "")
            condition_id = str(market.get("conditionId") or "")
            if market_id:
                by_market_id[market_id] = market
            if condition_id:
                by_condition_id[condition_id] = market
            for token_id in token_ids_from_market(market):
                by_token_id[token_id] = market
        return by_market_id, by_condition_id, by_token_id

    @staticmethod
    def _find_touched_market(
        context: dict[str, Any],
        by_market_id: dict[str, dict],
        by_condition_id: dict[str, dict],
        by_token_id: dict[str, dict],
    ) -> dict | None:
        market_id = str(context.get("market_id") or "")
        condition_id = str(context.get("condition_id") or "")
        token_id = str(context.get("token_id") or "")
        return by_market_id.get(market_id) or by_condition_id.get(condition_id) or by_token_id.get(token_id)

    def _market_snapshot_payload(
        self,
        market: dict,
        context: dict[str, Any],
        *,
        as_of: datetime,
    ) -> dict[str, Any]:
        side = context.get("side")
        token_id = context.get("token_id") or (token_from_market(market, side) if side in {"YES", "NO"} else None)
        quote_payload = self._quote_payload(self._fetch_quote(token_id)) if token_id else {}
        midpoint = quote_payload.get("midpoint")
        shares = self._safe_float(context.get("shares") or context.get("submitted_quantity"))
        position_usd = self._safe_float(context.get("position_usd"))
        mark_to_market_pnl = (
            midpoint * shares - position_usd
            if midpoint is not None and shares is not None and position_usd is not None
            else None
        )
        resolution_dt = resolution_datetime(market)
        best_bid = quote_payload.get("best_bid")
        best_ask = quote_payload.get("best_ask")
        metadata = self._parsed_market_metadata(market)
        return {
            **context,
            "city": context.get("city") or metadata.get("city"),
            "target_date": context.get("target_date") or metadata.get("target_date"),
            "bracket": context.get("bracket") or metadata.get("bracket"),
            "token_id": token_id,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "midpoint": midpoint,
            "spread": best_ask - best_bid if best_bid is not None and best_ask is not None else None,
            "mark_to_market_pnl": mark_to_market_pnl,
            "volume_usd": market_volume_usd(market),
            "resolution_time_utc": utc_iso(resolution_dt) if resolution_dt else None,
            "hours_to_resolution": (
                (resolution_dt - as_of).total_seconds() / 3600.0 if resolution_dt is not None else None
            ),
            "raw_market": self._compact_market(market),
            **{key: value for key, value in quote_payload.items() if key not in {"token_id", "midpoint", "best_bid", "best_ask"}},
        }

    def _forecast_snapshot_payload(
        self,
        market: dict,
        context: dict[str, Any],
        *,
        as_of: datetime,
    ) -> dict[str, Any] | None:
        parsed = self._parse_market(market)
        if parsed is None:
            return None
        target_date = parsed["date"]
        as_of_date = as_of.date()
        yes_prob = estimate_forecast_prob(
            lat=parsed["lat"],
            lon=parsed["lon"],
            tz=parsed["tz"],
            target_date=target_date,
            bracket_low=parsed["bracket_low"],
            bracket_high=parsed["bracket_high"],
            unit=parsed["unit"],
            as_of_date=as_of_date,
            metric=parsed.get("metric", "max"),
            temp_std_f=config.TEMP_STD_F,
            ensemble_sigma=False,
            use_empirical=config.USE_EMPIRICAL,
            city=parsed.get("city"),
        )
        side = context.get("side")
        model_probability = None
        if yes_prob is not None:
            model_probability = yes_prob if side != "NO" else 1.0 - yes_prob
        forecast_window = _fetch_forecast_window(
            parsed["lat"],
            parsed["lon"],
            parsed["tz"],
            as_of_date,
            target_date,
            parsed["unit"],
            model=config.DEFAULT_MODEL,
        )
        base_context = {
            key: value
            for key, value in context.items()
            if key not in {"best_bid", "best_ask", "midpoint", "spread", "quote_source", "quote_updated_at_utc"}
        }
        return {
            **base_context,
            "city": parsed.get("city"),
            "target_date": target_date.isoformat(),
            "bracket": self._bracket_from_parsed(parsed),
            "side": side,
            "model_probability": model_probability,
            "yes_model_probability": yes_prob,
            "forecast_temp": (
                (forecast_window or {}).get("temp_max")
                if parsed.get("metric", "max") == "max"
                else (forecast_window or {}).get("temp_min")
            ),
            "forecast_temp_max": (forecast_window or {}).get("temp_max"),
            "forecast_temp_min": (forecast_window or {}).get("temp_min"),
            "forecast_model": config.DEFAULT_MODEL,
            "forecast_lead_days": (target_date - as_of_date).days,
            "metric": parsed.get("metric", "max"),
            "temp_std_f": config.TEMP_STD_F,
            "use_empirical": config.USE_EMPIRICAL,
            "model_name": self.runtime.model_name,
            "model_variant": self.runtime.model_variant,
        }

    def _parse_market(self, market: dict) -> dict | None:
        return parse_market_question(
            market.get("question") or "",
            end_date_hint=_parse_end_date(market.get("endDate") or market.get("_event_endDate")),
        )

    def _parsed_market_metadata(self, market: dict | None) -> dict[str, Any]:
        if not market:
            return {}
        parsed = self._parse_market(market)
        if parsed is None:
            return {}
        return {
            "city": parsed.get("city"),
            "target_date": parsed.get("date").isoformat() if parsed.get("date") else None,
            "bracket": self._bracket_from_parsed(parsed),
        }

    def _bracket_from_plan(self, plan, market: dict | None) -> dict[str, Any] | None:
        if any(getattr(plan, key, None) is not None for key in ("bracket_low", "bracket_high", "bracket_unit", "metric")):
            return {
                "low": getattr(plan, "bracket_low", None),
                "high": getattr(plan, "bracket_high", None),
                "unit": getattr(plan, "bracket_unit", None),
                "metric": getattr(plan, "metric", None),
            }
        metadata = self._parsed_market_metadata(market)
        return metadata.get("bracket")

    def _bracket_from_row(self, row: dict[str, Any], *, market: dict | None) -> dict[str, Any] | None:
        bracket = row.get("bracket")
        if isinstance(bracket, dict):
            return bracket
        if any(row.get(key) is not None for key in ("bracket_low", "bracket_high", "bracket_unit", "metric")):
            return {
                "low": row.get("bracket_low"),
                "high": row.get("bracket_high"),
                "unit": row.get("bracket_unit"),
                "metric": row.get("metric"),
            }
        metadata = self._parsed_market_metadata(market)
        return metadata.get("bracket")

    @staticmethod
    def _bracket_from_parsed(parsed: dict[str, Any]) -> dict[str, Any]:
        return {
            "low": parsed.get("bracket_low"),
            "high": parsed.get("bracket_high"),
            "unit": parsed.get("unit"),
            "metric": parsed.get("metric"),
        }

    @staticmethod
    def _compact_market(market: dict) -> dict[str, Any]:
        keep = (
            "id",
            "conditionId",
            "question",
            "endDate",
            "_event_endDate",
            "volume",
            "volumeNum",
            "volumeClob",
            "liquidity",
            "closed",
            "active",
        )
        return {key: market[key] for key in keep if key in market}

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value in (None, "") or isinstance(value, bool):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _record_entry(self, decision: Decision, entered_positions: dict, *, market: dict | None = None) -> None:
        assert decision.plan is not None
        plan = decision.plan
        quote_payload = self._quote_context.get((plan.market_id, plan.side), {})
        event_payload = self._plan_event_payload(plan, quote_payload=quote_payload, market=market)
        self._touch_market_from_plan(plan, market=market, quote_payload=quote_payload)
        self.event_log.append_event("signal_generated", event_payload, timestamp_utc=plan.entry_time)

        submit_attempts = 0

        def on_submit_attempt(intent, attempt: int) -> None:
            nonlocal submit_attempts
            submit_attempts += 1
            self.event_log.append_event(
                "order_submitted",
                {
                    **event_payload,
                    **self._intent_event_payload(intent),
                    "order_attempt": attempt,
                    "dry_run": intent.dry_run,
                },
            )

        try:
            result = self._place_order_with_event_hook(plan, on_submit_attempt)
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
                self.event_log.append_event(
                    "order_submission_status_unknown",
                    {
                        **event_payload,
                        "submitted_limit_price": plan.entry_price,
                        "submitted_quantity": plan.shares,
                        "error": str(exc),
                    },
                )
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
        if submit_attempts == 0:
            on_submit_attempt(result.intent, 0 if result.intent.dry_run else -1)
        ack_payload = {
            **event_payload,
            **self._intent_event_payload(result.intent),
            **self._order_response_event_payload(result.response),
            "posted": result.posted,
            "dry_run": result.intent.dry_run,
            "raw_order_response": result.response,
        }
        self.event_log.append_event("order_acknowledged", ack_payload)
        self._record_order_response_lifecycle_events(event_payload, result.response)
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
        details = decision.details if isinstance(decision.details, dict) else {}
        self.event_log.append_event(
            "decision_skipped",
            {
                "market_id": market_id,
                "token_id": details.get("token_id"),
                "side": details.get("side"),
                "skip_reason": decision.reason,
                "decision_details": details,
            },
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
