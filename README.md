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

Reset the local simulated portfolio:

```powershell
uv run poly-cond-arb reset --yes
```

## Runtime Behavior

The runner is paper-only. It never places orders, uses API credentials, reads wallet secrets, signs transactions, or calls merge/redeem contracts.

REST polling retries market-universe and order-book fetches with capped exponential backoff before portfolio evaluation starts. Once paper execution begins, the cycle is not retried as a unit, so a post-execution logging or completion failure cannot replay the same trade. In WebSocket mode, startup cache rebuilds, REST book seeding, market refreshes, and WebSocket manager startup use the same finite recovery policy. Each retry is logged with the operation, attempt, error, and backoff seconds; after recovery, the successful fetch or bootstrap summary is logged.

Startup is gated on a fresh full-universe cache. The runner uses `data/market_universe_cache.json` immediately only when it is fresh under `COND_ARB_UNIVERSE_CACHE_MAX_AGE_SECONDS` and was built from full Gamma active-event pagination. If the cache is missing, stale, corrupt, or not a full-discovery cache, the runner enters `WARMUP`, fetches the full active universe, writes a new full cache, seeds REST ask books for every startup token, starts WebSocket subscriptions, and only then runs the first paper evaluation.

After the runner is `ONLINE`, full-universe refreshes continue in the background. Those refreshes may seed added token books and update WebSocket subscriptions, but they are not used as a first-trade shortcut. The console and `logs/conditional_arb_scan.log` report each `market_events_page_fetched`, `market_universe_fetch_complete`, `market_universe_cache_written`, background `market_universe_refresh_scheduled`, and `market_universe_refreshed` progress. Ctrl+C stops after the current request/page boundary.

`data/paper_portfolio_runtime.json` is local operational metadata written by `run` without acquiring another lock. It records host, PID, heartbeat, `warmup` / `online` / `stopping` phase, cache progress, last cycle summary, and the latest error. `poly-cond-arb status` watches that file every 2 seconds by default and repaints one terminal dashboard instead of appending repeated snapshots. Each dashboard includes a UTC `Last refreshed` timestamp and renders `ONLINE`, `WARMUP`, or `DEAD`; `--refresh-seconds N` changes the watch cadence and `--once` prints a single read-only snapshot.

The status dashboard keeps the live state as one scalar `Current:` value. Historical backend status entries are hidden by default and are only printed in a separate `Status Log` section when `poly-cond-arb status --show-log` is used.

`data/paper_portfolio_instance.json` is the source of truth for restart and resume. Paper executions are applied to a cloned state, written atomically through a temporary file, and only then swapped into memory. If the state write fails, the in-memory portfolio rolls back to the last persisted state. The append-only event log is useful for audit history, but a failed execution event append is reported without undoing or retrying the already persisted paper trade. `status` and restart recovery read the current state file, and leftover `paper_portfolio_instance.json.tmp` files are ignored on load.

The WebSocket cache evaluates price-change deltas only after a current snapshot exists for that token. Disconnect/stale marking clears snapshot readiness; REST bootstrap and reconciliation reseed the cache before dirty WebSocket updates are trusted again.

`run` and `reset --yes` acquire `paper_portfolio_instance.json.lock` in the data directory. A second runner or reset fails fast while that lock is active. If a lock belongs to a dead process on the same host, the runner removes it and continues; locks from another host or locks with unverifiable metadata are left in place and cause a clear failure. `status` does not take the lock and does not mutate state.

## Outputs

- `logs/conditional_arb_scan.log`: human-readable portfolio runner log.
- `data/paper_portfolio_instance.json`: current cash, equity, realized PnL, costs, executions, inventory, book fingerprints, and metadata.
- `data/paper_portfolio_events.jsonl`: append-only portfolio lifecycle, cycle, and execution events.
- `data/paper_portfolio_runtime.json`: current runner heartbeat, phase, warmup/cache counts, and last cycle summary.
- `data/paper_portfolio_instance.json.lock`: local process lock for `run` and `reset --yes`.
- `data/market_universe_cache.json`: warm-start cache of public tradable market metadata, kept in priority order.

## Environment Variables

- `COND_ARB_DATA_DIR`
- `COND_ARB_LOG_DIR`
- `COND_ARB_STARTING_CAPITAL_USD`
- `COND_ARB_TRADE_CEILING_USD`
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
- `COND_ARB_MARKET_REFRESH_INTERVAL_SECONDS`
- `COND_ARB_REST_RECONCILE_INTERVAL_SECONDS`
- `COND_ARB_WS_STALE_SECONDS`
- `COND_ARB_UNIVERSE_CACHE_MAX_AGE_SECONDS`
- `POLYMARKET_CLOB_HOST`

`COND_ARB_MARKET_LIMIT=0` means no local validation cap. The legacy `COND_ARB_FAST_START_*` variables are still parsed for compatibility, but they do not enable partial-slice startup trading. The WebSocket path is enabled by default; REST `/books` remains in use for startup seeding, added-token backfill, and periodic reconciliation.
