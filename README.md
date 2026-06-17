# Polymarket Paper Portfolio

Local paper-only portfolio instance for Polymarket binary YES+NO complete-set arbitrage. It starts with a simulated `$1,000` bankroll, reads public Gamma/CLOB market data, sizes paired YES and NO fills from executable ask-book depth, applies configurable slippage, fees, tax, and merge/redeem costs, then immediately redeems completed pairs back to cash.

The portfolio does not place orders, derive API credentials, read private keys, reconcile wallet state, call merge/redeem contracts, or provide a live trading mode. V1 executes only standard binary YES+NO complete-set paper trades; neg-risk event mechanics are not executed by the portfolio runner.

## Setup

```powershell
uv sync --extra dev
uv run pytest -p no:cacheprovider
```

Create a local environment file if you want to override defaults:

```powershell
Copy-Item .env.example .env
notepad .env
```

## Run

Run the continuous paper portfolio:

```powershell
uv run poly-cond-arb
```

The explicit form is equivalent:

```powershell
uv run poly-cond-arb run
```

Watch current runtime and portfolio performance:

```powershell
uv run poly-cond-arb status
```

Print one status snapshot for scripts or quick checks:

```powershell
uv run poly-cond-arb status --once
```

Measure public Polymarket endpoint latency and print a simulation suggestion:

```powershell
uv run poly-cond-arb latency
```

Add `--include-websocket` to also sample market WebSocket connect and first-message latency, and `--save` to write the raw report to `data/polymarket_latency_report.json`.

Reset the local simulated portfolio:

```powershell
uv run poly-cond-arb reset --yes
```

## Runtime Behavior

The runner is paper-only. It never places orders, uses API credentials, reads wallet secrets, signs transactions, or calls merge/redeem contracts.

Paper sizing mirrors Polymarket's API order-size floor: each simulated YES and NO order must be at least 5 outcome shares. If market metadata advertises a stricter minimum, that higher per-market minimum takes precedence.

`COND_ARB_TRADE_CEILING_USD` is a per-execution maximum capital spend. Profitable depth above the ceiling is clamped down to that spend limit instead of being rejected, and the 5-share floor is checked against the final simulated YES and NO order sizes after the clamp. `COND_ARB_PAPER_MIN_CASH_RESERVE_USD` keeps a cash buffer before new paper executions are sized; by default it equals the current trade ceiling, so the runner stops opening new paper trades before cash is drained to zero.

Paper fills use live-market-data-driven simulated execution by default while staying strictly paper-only. A signal-time opportunity is rechecked at the simulated fill timestamp using public YES/NO ask books from the WebSocket cache or fresh REST `/books`/`/book` snapshots. The simulator records public source type, timestamps, source revisions/hashes, snapshot readiness/generation, recent `price_change` deltas, `last_trade_price` prints, `tick_size_change` metadata, request latency/retry/status evidence, and public/network errors. It compares signal-time and fill-time books, skips safely on missing, unready, stale, timeout, or errored public data, and treats deterministic queue/adverse/partial fallback as opt-in when no usable public evidence exists. Completed paper fills store a top-level `simulation` object with `live_public_data`, `inferred`, and `fallback` sections plus fill timestamp, book source, queue inputs, rate-limit/local-pressure evidence, public-depth eligibility, and calibrated slippage metadata. Simulated failures append `paper_portfolio_execution_failed` audit events and do not mutate the portfolio.

Open unmatched inventory stays in the local paper portfolio with cost basis and mark-to-market fields. `run` reconciles public resolution events and Gamma market metadata, settles resolved leftovers to 1/0 when the winner is known, and records `paper_portfolio_settlement` events. `status` remains read-only and now shows pending settlement, latest settlement counters, committed capital, open mark-to-market value, and active open trades from the saved portfolio/runtime state.

The simulator does not claim private live-order truth. Public CLOB REST `/book` and `/books` snapshots can expose bids, asks, timestamp/hash-style revisions, min order size, tick size, neg-risk, and last-trade fields, and a token may return `404 No orderbook` even when market metadata exists. Public market WebSocket events such as `book`, `price_change`, `last_trade_price`, `tick_size_change`, and optional `best_bid_ask` provide observable book state, level-size changes, trade prints, timestamps, and hashes where present. Public Gamma discovery supplies CLOB token IDs, `orderMinSize`, `orderPriceMinTickSize`, active/closed/accepting-order flags, best bid/ask, and last trade fields. Rate-limit docs describe Cloudflare throttling and endpoint ceilings, but responses may not include granular rate-limit headers; the runner records local request cadence, retries, backoff, status/error outcomes, and documented bucket estimates as evidence. Exact private queue position, private order acceptance/rejection details, cancel lifecycle, and whether a hypothetical paper order would have been maker/taker filled are not observable without private live orders, so those fields are labeled inferred or fallback.

REST polling retries market-universe and order-book fetches with capped exponential backoff before portfolio evaluation starts. Partial `/books` responses are preserved by token ID, and only missing or malformed token books fall back to single-token `/book` requests. Once paper execution begins, the cycle is not retried as a unit, so a post-execution logging or completion failure cannot replay the same trade. In WebSocket mode, startup cache rebuilds, REST book seeding, market refreshes, and WebSocket manager startup use the same finite recovery policy. Each retry is logged with the operation, attempt, error, and backoff seconds; after recovery, the successful fetch or bootstrap summary is logged.

WebSocket startup separates executor readiness from full-universe coverage. The runner uses `data/market_universe_cache.json` immediately only when it is fresh under `COND_ARB_UNIVERSE_CACHE_MAX_AGE_SECONDS` and was built from full Gamma active-event pagination. If that full cache is missing, stale, corrupt, or not a full-discovery cache, WebSocket mode starts from a priority Gamma slice ordered by `volume24hr`, capped by `COND_ARB_FAST_START_EVENT_LIMIT` and `COND_ARB_FAST_START_TOKEN_LIMIT`. That priority slice is not written as the full `market_universe_cache.json`. WebSocket subscriptions start before REST bootstrap seeding completes, REST books are seeded in chunks, and each ready YES/NO market pair can be evaluated as soon as its chunk is seeded. The WebSocket client keeps the 500-asset chunking default but now raises its receive-frame cap to `8 MiB` by default through `COND_ARB_MARKET_WS_MAX_MESSAGE_SIZE_BYTES`, so large initial Polymarket book frames do not loop on client-side `1009 message too big` closes. REST-only mode keeps the full-cache/full-discovery startup contract, but also seeds and evaluates completed REST book chunks incrementally.

After the executor is `ONLINE`, full-universe refreshes continue in the background until `Coverage` is full. Those refreshes merge priority markets first, seed only added token books, update WebSocket subscriptions in place when possible, and then mark added markets dirty against the merged universe. Periodic REST reconciliation is background recovery work: it seeds REST books in chunks, marks only refreshed tokens dirty, and schedules the next reconcile from the time the seed finishes. Dirty WebSocket updates are not hidden behind an active full-universe reconcile; ready dirty markets evaluate immediately, while missing or stale YES/NO mates schedule a targeted pair backfill instead of a full-universe fetch. If a targeted `dirty_pair_backfill` batch stays in flight longer than `COND_ARB_REST_BOOK_SEED_BATCH_STALL_SECONDS`, the runner logs `rest_book_seed_batch_stalled` and mirrors that warning into runtime `last_error` without cancelling the request or mutating paper state. The console and `logs/conditional_arb_scan.log` report each `market_events_page_fetched`, `market_universe_fetch_complete`, `market_universe_cache_written`, background `market_universe_refresh_scheduled`, and `market_universe_refreshed` progress. REST book-seed failures are logged with compact failed-token samples and category counts instead of full token lists. Ctrl+C stops after the current request, REST book batch, or market-evaluation boundary.

`data/paper_portfolio_runtime.json` is local operational metadata written by `run` without acquiring another lock. It records host, PID, heartbeat, `warmup` / `online` / `stopping` phase, executor status, priority/full coverage status, cache progress, REST book-seed progress and failure samples, WebSocket connection/error/reconnect counters, coalesced dirty WebSocket backlog, last cycle summary, live-driven simulation failure counts, and the latest error. Runtime status writes are best-effort telemetry: a transient write or replace failure on this file is retried, recorded in the runtime payload when possible, and logged as `runtime_status_write_failed`, but it does not restart warmup or retry a successful REST book seed. During warmup or background seeding, `poly-cond-arb status` shows elapsed warmup time plus REST seed completed/remaining token backlog, received books, failed tokens, observed token rate, ETA, compact failed-token samples/categories, and the current REST book batch with its in-flight age when a batch is pending. The portfolio section separates `Realized win`, which counts wins only among executions that redeemed or otherwise realized PnL, from `Execution win`, which keeps the all-executions win rate for compatibility. `Committed` is open inventory cost basis, `Open value` is current inventory mark-to-market value, and `Active trades` counts distinct markets with non-zero open inventory. The execution section shows `Dirty backlog` as `none`, `N tokens`, or `full universe`, reports REST reconcile progress separately, and surfaces the last cycle's live simulation failure counts, latest live simulation failure reason, and targeted backfill stall warnings when a batch runs past its telemetry threshold. Large WebSocket churn during a REST reconcile means recovery is in progress, not that millions of distinct markets are queued. `status` watches the runtime file every 2 seconds by default and repaints the same two-column ASCII terminal dashboard instead of appending repeated snapshots. Each dashboard includes a UTC `Updated` timestamp, live state and freshness badges, PID/host metadata, health, portfolio, warmup, cost, and execution sections, plus a progress bar when REST seed progress is available; `--refresh-seconds N` changes the watch cadence, and `--once` remains the stable single-snapshot mode for scripts or quick checks.

The status dashboard keeps the live state as one header badge. Historical backend status entries are hidden by default and are only printed in a separate `STATUS LOG` section when `poly-cond-arb status --show-log` is used.

`data/paper_portfolio_instance.json` is the source of truth for restart and resume. Paper executions are applied to a cloned state, written atomically through a temporary file, and only then swapped into memory. If the state write fails, the in-memory portfolio rolls back to the last persisted state. The append-only event log is useful for audit history, but a failed execution event append is reported without undoing or retrying the already persisted paper trade. `status` and restart recovery read the current state file, and leftover `paper_portfolio_instance.json.tmp` files are ignored on load.

The WebSocket cache evaluates price-change deltas only after a current snapshot exists for that token. Disconnect/stale marking clears snapshot readiness and increments runtime stale-token batch counters; REST bootstrap, targeted pair backfill, and reconciliation reseed the cache before dirty WebSocket updates are trusted again. Dirty updates are coalesced into one pending token set. Repeated skipped price-change deltas are warning-throttled so stale or unseeded tokens remain visible without flooding `logs/conditional_arb_scan.log`. The cache also retains bounded recent public evidence per token, including `price_change`, `last_trade_price`, `tick_size_change`, and `best_bid_ask` events, so paper fill audits can explain which observed public data supported or blocked a simulated execution.

`run` and `reset --yes` acquire `paper_portfolio_instance.json.lock` in the data directory. A second runner or reset fails fast while that lock is active. If a lock belongs to a dead process on the same host, the runner removes it and continues; locks from another host or locks with unverifiable metadata are left in place and cause a clear failure. `status` does not take the lock and does not mutate state.

## Outputs

The files below are generated local operational state and are ignored by Git. Treat them as the active paper instance; use `uv run poly-cond-arb reset --yes` to intentionally reset the simulated portfolio instead of deleting runtime files during normal operation.

- `logs/conditional_arb_scan.log`: human-readable portfolio runner log.
- `data/paper_portfolio_instance.json`: current cash, equity, realized PnL, costs, executions, inventory, book fingerprints, live-public-data simulation metadata, and run metadata.
- `data/paper_portfolio_events.jsonl`: append-only portfolio lifecycle, cycle, execution, settlement, and simulated execution-failure events.
- `data/paper_portfolio_runtime.json`: current runner heartbeat, phase, executor and coverage status, warmup/cache counts, REST book-seed progress including active batch telemetry and compact failure samples/categories, WebSocket health counters, dirty WebSocket backlog counters, best-effort runtime write failure counters, last cycle summary, settlement counters, and simulation failure counts.
- `data/polymarket_latency_report.json`: optional saved public latency probe results and simulation calibration suggestion.
- `data/paper_portfolio_instance.json.lock`: local process lock for `run` and `reset --yes`.
- `data/market_universe_cache.json`: warm-start cache of public tradable market metadata, kept in priority order.

## Environment Variables

- `COND_ARB_DATA_DIR`
- `COND_ARB_LOG_DIR`
- `COND_ARB_STARTING_CAPITAL_USD`
- `COND_ARB_TRADE_CEILING_USD`
- `COND_ARB_PAPER_MIN_CASH_RESERVE_USD`
- `COND_ARB_SLIPPAGE_BUFFER_BPS`
- `COND_ARB_TAKER_FEE_BPS`
- `COND_ARB_TAX_BPS`
- `COND_ARB_MERGE_COST_USD`
- `COND_ARB_MIN_NET_PROFIT_USD`
- `COND_ARB_MIN_NET_RETURN_BPS`
- `COND_ARB_MAX_BOOK_AGE_SECONDS`
- `COND_ARB_MARKET_LIMIT`
- `COND_ARB_POLL_INTERVAL_SECONDS`
- `COND_ARB_MARKET_WS_ENABLED`
- `COND_ARB_MARKET_WS_ENDPOINT`
- `COND_ARB_MARKET_WS_HEARTBEAT_SECONDS`
- `COND_ARB_MARKET_WS_MAX_ASSETS_PER_CONNECTION`
- `COND_ARB_MARKET_WS_MAX_MESSAGE_SIZE_BYTES`
- `COND_ARB_MARKET_REFRESH_INTERVAL_SECONDS`
- `COND_ARB_REST_RECONCILE_INTERVAL_SECONDS`
- `COND_ARB_REST_BOOK_SEED_BATCH_STALL_SECONDS`
- `COND_ARB_WS_STALE_SECONDS`
- `COND_ARB_FAST_START_EVENT_LIMIT`
- `COND_ARB_FAST_START_TOKEN_LIMIT`
- `COND_ARB_UNIVERSE_CACHE_MAX_AGE_SECONDS`
- `COND_ARB_PAPER_SIMULATION_ENABLED`
- `COND_ARB_PAPER_SIM_SEED`
- `COND_ARB_PAPER_LATENCY_MS`
- `COND_ARB_PAPER_LATENCY_JITTER_MS`
- `COND_ARB_PAPER_LATENCY_MODE`
- `COND_ARB_PAPER_LOCAL_TIMEOUT_MS`
- `COND_ARB_PAPER_TELEMETRY_LATENCY_WINDOW`
- `COND_ARB_PAPER_LATENCY_JITTER_SEED_SCOPE`
- `COND_ARB_PAPER_SIGNING_LATENCY_MS`
- `COND_ARB_PAPER_SETTLEMENT_LATENCY_MS`
- `COND_ARB_PAPER_MAX_FILL_PRICE_MOVE_BPS`
- `COND_ARB_PAPER_FILL_ELIGIBILITY_MODE`
- `COND_ARB_PAPER_ALLOW_TRADE_PRINT_FILL_SUPPORT`
- `COND_ARB_PAPER_ALLOW_DETERMINISTIC_FILL_FALLBACK`
- `COND_ARB_PAPER_SETTLEMENT_ENABLED`
- `COND_ARB_PAPER_SETTLEMENT_SOURCE`
- `COND_ARB_PAPER_UNMATCHED_OPEN_VALUATION`
- `COND_ARB_PAPER_SETTLEMENT_REQUIRE_WINNER`
- `COND_ARB_PAPER_SLIPPAGE_MODE`
- `COND_ARB_PAPER_SLIPPAGE_MAX_BPS`
- `COND_ARB_PAPER_SLIPPAGE_LOOKBACK_EVENTS`
- `COND_ARB_PAPER_SLIPPAGE_COMBINE_MODE`
- `COND_ARB_PAPER_PAIR_FILL_POLICY`
- `COND_ARB_PAPER_DYNAMIC_THRESHOLDS_ENABLED`
- `COND_ARB_PAPER_BLOCK_UNMATCHED_MARKET_REENTRY`
- `COND_ARB_PAPER_BLOCK_UNMATCHED_EVENT_REENTRY`
- `COND_ARB_PAPER_MAX_UNMATCHED_COST_USD_TOTAL`
- `COND_ARB_PAPER_UNMATCHED_INVENTORY_MANAGEMENT`
- `COND_ARB_EXECUTION_HEALTH_GATES_ENABLED`
- `COND_ARB_HEALTH_MAX_WS_RECONNECTS_PER_MINUTE`
- `COND_ARB_HEALTH_MAX_WS_ERRORS_PER_MINUTE`
- `COND_ARB_HEALTH_MAX_DIRTY_TOKENS`
- `COND_ARB_HEALTH_MAX_DIRTY_BATCHES`
- `COND_ARB_HEALTH_MAX_LATENCY_P95_MS`
- `COND_ARB_HEALTH_MAX_LATENCY_JITTER_MS`
- `COND_ARB_PAPER_QUEUE_DEPTH_RATIO`
- `COND_ARB_PAPER_QUEUE_FILL_PROBABILITY`
- `COND_ARB_PAPER_PARTIAL_FILL_PROBABILITY`
- `COND_ARB_PAPER_PARTIAL_FILL_MIN_RATIO`
- `COND_ARB_PAPER_SUBMIT_FAILURE_PROBABILITY`
- `COND_ARB_PAPER_ACCEPT_FAILURE_PROBABILITY`
- `COND_ARB_PAPER_FILL_FAILURE_PROBABILITY`
- `COND_ARB_PAPER_CANCEL_FAILURE_PROBABILITY`
- `COND_ARB_PAPER_THROTTLE_MAX_SUBMISSIONS_PER_SECOND`
- `COND_ARB_PAPER_THROTTLE_QUANTITY_RATIO`
- `COND_ARB_PAPER_ADVERSE_SELECTION_PROBABILITY`
- `COND_ARB_PAPER_ADVERSE_DEPTH_REMOVAL_RATIO`
- `COND_ARB_PAPER_ADVERSE_PRICE_MOVE_BPS`
- `POLYMARKET_CLOB_HOST`

`COND_ARB_MARKET_LIMIT=0` means no local validation cap. The WebSocket path is enabled by default; REST `/books` remains in use for startup seeding, added-token backfill, periodic reconciliation, and settlement valuation. Paper execution simulation is enabled by default; set `COND_ARB_PAPER_SIMULATION_ENABLED=false`, or leave it enabled and set all friction knobs to zero, to preserve the old optimistic fill behavior. Deterministic fill fallback is now opt-in.
