from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from collections import defaultdict
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping

from . import config
from .arb_models import BinaryMarket, ConditionalArbOpportunity, OrderBookSide
from .arb_strategy import ArbDecision, ArbStrategyParams, evaluate_binary_arbitrage, evaluate_neg_risk_event_group
from .event_log import ConditionalArbEventLog, jsonable, utc_iso
from .fetcher import GammaClobClient
from .market_data import MarketDataCache, MarketWebSocketManager, MarketWebSocketSettings
from .paper import PaperConditionalArbLedger

DIRTY_EVALUATION_DEBOUNCE_SECONDS = 0.1


@dataclass(frozen=True)
class MarketUniverse:
    events_fetched: int
    raw_markets: int
    markets: tuple[BinaryMarket, ...]
    markets_by_token: Mapping[str, tuple[BinaryMarket, ...]]
    neg_risk_groups: Mapping[str, tuple[BinaryMarket, ...]]

    @property
    def token_ids(self) -> list[str]:
        return _token_ids_for_markets(list(self.markets))


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


def _build_market_universe(
    *,
    events_fetched: int,
    raw_markets: int,
    markets: list[BinaryMarket],
) -> MarketUniverse:
    markets_by_token: dict[str, list[BinaryMarket]] = defaultdict(list)
    neg_risk_groups: dict[str, list[BinaryMarket]] = defaultdict(list)
    for market in markets:
        markets_by_token[market.yes_token_id].append(market)
        markets_by_token[market.no_token_id].append(market)
        if market.neg_risk and market.event_id:
            neg_risk_groups[market.event_id].append(market)
    return MarketUniverse(
        events_fetched=events_fetched,
        raw_markets=raw_markets,
        markets=tuple(markets),
        markets_by_token={token: tuple(rows) for token, rows in markets_by_token.items()},
        neg_risk_groups={event_id: tuple(rows) for event_id, rows in neg_risk_groups.items()},
    )


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
                "market_ws_enabled": self.config.market_ws_enabled,
                "market_ws_endpoint": self.config.market_ws_endpoint,
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
        try:
            if self.config.market_ws_enabled:
                asyncio.run(self._run_market_ws_forever())
            else:
                self._run_rest_forever()
        finally:
            self.ledger.save()

    def _run_rest_forever(self) -> None:
        while self.running:
            self.run_one_cycle()
            if self.running:
                time.sleep(self.config.poll_interval_seconds)

    async def _run_market_ws_forever(self) -> None:
        cache = MarketDataCache()
        dirty_queue: asyncio.Queue[set[str]] = asyncio.Queue()
        ws_params = replace(self.params, max_book_age_seconds=self.config.ws_stale_seconds)

        def _mark_dirty(token_ids: set[str]) -> None:
            if token_ids:
                dirty_queue.put_nowait(set(token_ids))

        def _mark_disconnected_stale(token_ids: set[str]) -> None:
            if not token_ids:
                return
            stale_at = datetime.now(timezone.utc) - timedelta(seconds=self.config.ws_stale_seconds + 1.0)
            cache.mark_tokens_stale(token_ids, stale_at=stale_at)
            dirty_queue.put_nowait(set(token_ids))

        manager = MarketWebSocketManager(
            settings=MarketWebSocketSettings(
                endpoint=self.config.market_ws_endpoint,
                heartbeat_seconds=self.config.market_ws_heartbeat_seconds,
                max_assets_per_connection=self.config.market_ws_max_assets_per_connection,
            ),
            cache=cache,
            logger=self.logger,
            on_dirty_tokens=_mark_dirty,
            on_connection_lost=_mark_disconnected_stale,
        )

        try:
            universe = await asyncio.to_thread(self._fetch_market_universe)
            await self._seed_rest_books(cache, universe.token_ids, reason="ws_bootstrap")
            await manager.start(universe.token_ids)
            self._evaluate_from_cache(
                universe,
                cache,
                dirty_token_ids=None,
                evaluation_reason="ws_bootstrap",
                params=ws_params,
            )

            loop = asyncio.get_running_loop()
            next_refresh = loop.time() + self.config.market_refresh_interval_seconds
            next_reconcile = loop.time() + self.config.rest_reconcile_interval_seconds

            while self.running:
                timeout = max(0.0, min(next_refresh, next_reconcile) - loop.time())
                timeout = min(timeout, 1.0)
                dirty_tokens: set[str] | None = None
                try:
                    first_dirty = await asyncio.wait_for(dirty_queue.get(), timeout=timeout)
                    dirty_tokens = await self._collect_dirty_tokens(dirty_queue, first_dirty)
                except TimeoutError:
                    pass

                if dirty_tokens:
                    self._evaluate_from_cache(
                        universe,
                        cache,
                        dirty_token_ids=dirty_tokens,
                        evaluation_reason="ws_dirty_update",
                        params=ws_params,
                    )

                now = loop.time()
                if now >= next_refresh:
                    try:
                        universe = await self._refresh_market_universe(universe, cache, manager, dirty_queue)
                    except Exception as exc:
                        self.logger.warning("market_universe_refresh_failed error=%s", exc)
                    next_refresh = now + self.config.market_refresh_interval_seconds

                now = loop.time()
                if now >= next_reconcile:
                    try:
                        await self._seed_rest_books(cache, universe.token_ids, reason="rest_reconcile")
                        dirty_queue.put_nowait(set(universe.token_ids))
                    except Exception as exc:
                        self.logger.warning("rest_reconcile_failed error=%s", exc)
                    next_reconcile = now + self.config.rest_reconcile_interval_seconds
        finally:
            await manager.stop()

    async def _collect_dirty_tokens(
        self,
        dirty_queue: asyncio.Queue[set[str]],
        first_dirty: set[str],
    ) -> set[str]:
        dirty_tokens = set(first_dirty)
        await asyncio.sleep(DIRTY_EVALUATION_DEBOUNCE_SECONDS)
        while True:
            try:
                dirty_tokens.update(dirty_queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        return dirty_tokens

    async def _seed_rest_books(self, cache: MarketDataCache, token_ids: list[str], *, reason: str) -> set[str]:
        if not token_ids:
            return set()
        books = await asyncio.to_thread(self.client.fetch_ask_books, token_ids)
        updated = cache.seed_ask_books(books)
        self.logger.info("rest_books_seeded reason=%s tokens=%s", reason, len(updated))
        return updated

    async def _refresh_market_universe(
        self,
        old_universe: MarketUniverse,
        cache: MarketDataCache,
        manager: MarketWebSocketManager,
        dirty_queue: asyncio.Queue[set[str]],
    ) -> MarketUniverse:
        new_universe = await asyncio.to_thread(self._fetch_market_universe)
        old_tokens = set(old_universe.token_ids)
        new_tokens = set(new_universe.token_ids)
        added_tokens = sorted(new_tokens - old_tokens)
        removed_tokens = sorted(old_tokens - new_tokens)

        if added_tokens:
            seeded = await self._seed_rest_books(cache, added_tokens, reason="market_refresh_added")
            if seeded:
                dirty_queue.put_nowait(seeded)
        await manager.update_tokens(new_universe.token_ids)
        if removed_tokens:
            cache.remove_tokens(removed_tokens)

        self.event_log.append_event(
            "conditional_arb_market_universe_refreshed",
            {
                "events_fetched": new_universe.events_fetched,
                "raw_markets": new_universe.raw_markets,
                "tradable_markets": len(new_universe.markets),
                "tokens_added": len(added_tokens),
                "tokens_removed": len(removed_tokens),
            },
        )
        self.logger.info(
            "market_universe_refreshed tradable=%s added_tokens=%s removed_tokens=%s",
            len(new_universe.markets),
            len(added_tokens),
            len(removed_tokens),
        )
        return new_universe

    def run_one_cycle(self) -> dict[str, Any]:
        universe = self._fetch_market_universe()
        books_by_token = self.client.fetch_ask_books(universe.token_ids)
        return self._evaluate_universe(
            universe,
            books_by_token,
            dirty_token_ids=None,
            evaluation_reason="rest_cycle",
            params=self.params,
        )

    def _fetch_market_universe(self) -> MarketUniverse:
        events = self.client.fetch_active_events()
        raw_markets = self.client.flatten_event_markets(events)
        tradable_markets = self.client.tradable_binary_markets(raw_markets)
        if self.config.market_limit is not None:
            tradable_markets = tradable_markets[: self.config.market_limit]
        return _build_market_universe(
            events_fetched=len(events),
            raw_markets=len(raw_markets),
            markets=tradable_markets,
        )

    def _evaluate_from_cache(
        self,
        universe: MarketUniverse,
        cache: MarketDataCache,
        *,
        dirty_token_ids: set[str] | None,
        evaluation_reason: str,
        params: ArbStrategyParams,
    ) -> dict[str, Any]:
        return self._evaluate_universe(
            universe,
            cache.ask_books_snapshot(universe.token_ids),
            dirty_token_ids=dirty_token_ids,
            evaluation_reason=evaluation_reason,
            params=params,
        )

    def _evaluate_universe(
        self,
        universe: MarketUniverse,
        books_by_token: Mapping[str, OrderBookSide],
        *,
        dirty_token_ids: set[str] | None,
        evaluation_reason: str,
        params: ArbStrategyParams,
    ) -> dict[str, Any]:
        cycle_started = datetime.now(timezone.utc)
        self.logger.info("cycle_start reason=%s at=%s", evaluation_reason, cycle_started.isoformat())
        self.event_log.append_event(
            "conditional_arb_cycle_started",
            {
                "cycle_started_at_utc": utc_iso(cycle_started),
                "evaluation_reason": evaluation_reason,
                "dirty_tokens": len(dirty_token_ids) if dirty_token_ids is not None else None,
            },
        )

        skip_counts: dict[str, int] = {}
        neg_risk_groups = dict(universe.neg_risk_groups) if self.config.include_neg_risk else {}
        standard_markets, neg_groups_to_evaluate = self._evaluation_targets(
            universe,
            dirty_token_ids=dirty_token_ids,
            skip_counts=skip_counts,
        )
        entered_positions = dict(self.ledger.opportunities)
        opportunities: list[ConditionalArbOpportunity] = []

        for market in standard_markets:
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
                params=params,
            )
            self._handle_decision(decision, opportunities, skip_counts)

        for group in neg_groups_to_evaluate:
            decision = evaluate_neg_risk_event_group(
                list(group),
                books_by_token,
                as_of=cycle_started,
                params=params,
            )
            self._handle_decision(decision, opportunities, skip_counts)

        recorded = self._record_opportunities(opportunities, cycle_started, skip_counts)
        summary = {
            "cycle_started_at_utc": utc_iso(cycle_started),
            "evaluation_reason": evaluation_reason,
            "dirty_tokens": len(dirty_token_ids) if dirty_token_ids is not None else None,
            "events_fetched": universe.events_fetched,
            "raw_markets": universe.raw_markets,
            "tradable_markets": len(universe.markets),
            "standard_binary_markets": sum(1 for market in universe.markets if not market.neg_risk),
            "neg_risk_groups": len(neg_risk_groups),
            "evaluated_standard_binary_markets": len(standard_markets),
            "evaluated_neg_risk_groups": len(neg_groups_to_evaluate),
            "opportunities_detected": len(opportunities),
            "opportunities_recorded": recorded,
            "skip_counts": skip_counts,
        }
        _write_opportunities_snapshot(self.config.opportunities_path, opportunities, summary)
        self.event_log.append_event("conditional_arb_cycle_completed", summary)
        self.logger.info(
            "cycle_end reason=%s events=%s raw_markets=%s tradable=%s evaluated_binary=%s "
            "evaluated_neg_groups=%s opportunities=%s recorded=%s skipped=%s",
            evaluation_reason,
            universe.events_fetched,
            universe.raw_markets,
            len(universe.markets),
            len(standard_markets),
            len(neg_groups_to_evaluate),
            len(opportunities),
            recorded,
            skip_counts,
        )
        return {
            "summary": summary,
            "opportunities": [opportunity.to_record() for opportunity in opportunities],
        }

    def _evaluation_targets(
        self,
        universe: MarketUniverse,
        *,
        dirty_token_ids: set[str] | None,
        skip_counts: dict[str, int],
    ) -> tuple[list[BinaryMarket], list[tuple[BinaryMarket, ...]]]:
        if dirty_token_ids is None:
            if self.config.include_neg_risk:
                missing_grouping = sum(1 for market in universe.markets if market.neg_risk and not market.event_id)
                if missing_grouping:
                    skip_counts["missing_grouping_metadata"] = missing_grouping
                return (
                    [market for market in universe.markets if not market.neg_risk],
                    list(universe.neg_risk_groups.values()),
                )
            return [market for market in universe.markets if not market.neg_risk], []

        standard_by_market_id: dict[str, BinaryMarket] = {}
        neg_group_ids: set[str] = set()
        missing_grouping_seen: set[str] = set()
        for token_id in dirty_token_ids:
            for market in universe.markets_by_token.get(token_id, ()):
                if market.neg_risk:
                    if self.config.include_neg_risk and market.event_id:
                        neg_group_ids.add(market.event_id)
                    elif self.config.include_neg_risk:
                        missing_grouping_seen.add(market.market_id)
                    continue
                standard_by_market_id[market.market_id] = market

        if missing_grouping_seen:
            skip_counts["missing_grouping_metadata"] = len(missing_grouping_seen)
        return (
            list(standard_by_market_id.values()),
            [universe.neg_risk_groups[event_id] for event_id in sorted(neg_group_ids)],
        )

    def _record_opportunities(
        self,
        opportunities: list[ConditionalArbOpportunity],
        as_of: datetime,
        skip_counts: dict[str, int],
    ) -> int:
        recorded = 0
        for opportunity in opportunities:
            if self.ledger.has_opportunity(opportunity.opportunity_id):
                skip_counts["already_recorded"] = skip_counts.get("already_recorded", 0) + 1
                continue
            self.ledger.record(opportunity, as_of=as_of)
            recorded += 1
            self.event_log.append_event(
                "conditional_arb_opportunity_recorded",
                opportunity.to_record(),
            )
        return recorded

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
    parser.add_argument("--once", action="store_true", help="Run one REST snapshot cycle and exit")
    parser.add_argument("--limit", type=int, default=loaded.market_limit, help="Maximum tradable markets to scan")
    parser.add_argument(
        "--poll-interval-seconds",
        type=int,
        default=loaded.poll_interval_seconds,
        help="Seconds between REST polling cycles when market WebSocket is disabled",
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
    parser.add_argument("--market-ws", dest="market_ws_enabled", action="store_true", default=None)
    parser.add_argument("--no-market-ws", dest="market_ws_enabled", action="store_false")
    parser.add_argument("--json", action="store_true", help="Print cycle result as JSON")
    parser.add_argument("--data-dir", type=Path, default=loaded.data_dir, help="Data output directory")
    parser.add_argument("--clob-host", default=loaded.clob_host, help="CLOB host override")
    return parser


def _config_from_args(args: argparse.Namespace) -> config.ScanConfig:
    loaded = config.load_scan_config()
    include_neg_risk = loaded.include_neg_risk if args.include_neg_risk is None else bool(args.include_neg_risk)
    market_ws_enabled = (
        loaded.market_ws_enabled if args.market_ws_enabled is None else bool(args.market_ws_enabled)
    )
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
        market_ws_enabled=market_ws_enabled,
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
