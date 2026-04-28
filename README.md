# Polymarket Weather Live Bot

Standalone live bot for the fixed_v1_no (Combined) Polymarket weather arbitrage strategy. It is intentionally separated from the backtesting repository and carries only the runtime strategy/model code needed to poll active weather markets, compute fixed_v1_no entries, and place dry-run or live CLOB orders.

The model evaluates YES first using fixed_v1 gates. If YES does not qualify on price, probability, edge, calibration, or because the YES token does not have a two-sided live book, it evaluates the NO token with `1 - P(YES)`, mirrored fixed_v1 gates, and a `0.75` maximum NO entry price. It records one side per market.

## Safety Defaults

- `DRY_RUN=true` by default. The bot logs intended orders and skips actual order posting.
- `data/live_positions.json` is ignored by Git and prevents one re-entry per market across restarts.
- Live mode blocks trading until startup reconciliation checks open orders and positions.
- Live mode refreshes CLOB collateral balance/allowance before each order and fails closed if either is below the order size.
- `logs/` is ignored by Git.
- Live trading requires Polymarket CLOB credentials in environment variables.

## Setup And Runbook

Open PowerShell in the repo root:

```powershell
cd "C:\Users\aiden\Documents\New project 3"
```

This repo uses `uv` because the old backtest venv is not portable. Install the
dependencies:

```powershell
uv sync --extra dev
```

If `uv` complains about its user cache on this machine, keep the cache inside
the project and rerun the sync:

```powershell
$env:UV_CACHE_DIR="$PWD\.uv-cache"
$env:UV_PYTHON_INSTALL_DIR="$PWD\.uv-python"
uv sync --extra dev
```

Run the test suite after setup:

```powershell
uv run pytest -p no:cacheprovider
```

## Environment Variables

The bot and dashboard automatically load simple `KEY=value` settings from the
repo-root `.env` file. Variables already set in the shell take precedence over
`.env`, which is useful for one-off validation overrides.

Start by creating a local env file from the example:

```powershell
Copy-Item .env.example .env
notepad .env
```

Minimum safe dry-run settings:

```powershell
$env:DRY_RUN="true"
$env:POLL_INTERVAL_MINUTES="15"
$env:OFFLINE_RETRY_SECONDS="60"
$env:RECONCILE_ON_STARTUP="true"
$env:MAX_POSITION_USD="2.50"
$env:ENABLE_NO_SIDE="true"
```

Useful validation setting:

```powershell
$env:LIVE_MARKET_LIMIT="10"
```

Use `LIVE_MARKET_LIMIT=10` for bounded validation. Use `0` or leave it unset
for the full live weather market scan.

Market discovery, weather forecasts, startup reconciliation, periodic safety
checks, and reconnect recovery stay on REST. The public market WebSocket is a
low-latency best bid/ask cache for candidate YES/NO token IDs discovered by
Gamma; stale or missing quotes fall back to the existing REST CLOB book fetch.
The authenticated user WebSocket is enabled only in live mode with API
credentials present and logs order/trade events for candidate condition IDs.

Default streaming and safety settings:

```powershell
$env:POLYMARKET_MARKET_WS_ENABLED="true"
$env:POLYMARKET_USER_WS_ENABLED="true"
$env:POLYMARKET_WS_MARKET_STALE_SECONDS="20"
$env:POLYMARKET_WS_MARKET_MAX_TOKENS="200"
$env:POLYMARKET_WS_MARKET_WARMUP_SECONDS="1.5"
$env:SAFETY_RECONCILE_INTERVAL_MINUTES="60"
$env:SAFETY_RECONCILE_MIN_INTERVAL_SECONDS="300"
$env:EVENT_SNAPSHOT_INTERVAL_MINUTES="5"
```

`POLYMARKET_WS_MARKET_MAX_TOKENS` caps the active token subscription set to
stay bounded during broad scans. When the scan has more tokens than the cap,
the bot prioritizes existing positions and markets that pass static trade
filters before applying the cap, then falls back to higher-volume markets. If
the WebSocket disconnects and reconnects in live mode, the next bot cycle
schedules REST reconciliation before trading; rapid reconnect loops are throttled by
`SAFETY_RECONCILE_MIN_INTERVAL_SECONDS`.

The effective cap is logged at startup as `max_position_usd=...` and shown in
the dashboard runtime panel. Existing rows in `data/live_positions.json` keep
their recorded ledger size, but the dashboard reports old dry-run rows using
the current capped size so validation exposure matches `MAX_POSITION_USD`.
Live rows continue to show actual recorded exchange exposure.

## Dry-Run Commands

Run one bounded dry-run poll cycle:

```powershell
$env:DRY_RUN="true"
$env:LIVE_MARKET_LIMIT="10"
uv run python -m weather_arb_live.live_bot --once
```

Run continuously in dry-run mode:

```powershell
$env:DRY_RUN="true"
$env:LIVE_MARKET_LIMIT="0"
uv run python -m weather_arb_live.live_bot
```

Dry-run orders are logged but not posted. They are also recorded in
`data/live_positions.json` so the bot does not repeatedly enter the same market
during validation.

## Local Dashboard

Run the dashboard in a second PowerShell window while the bot is running:

```powershell
uv run python -m weather_arb_live.dashboard --host 127.0.0.1 --port 8765
```

Then open `http://127.0.0.1:8765`. The dashboard reads local runtime state from
`data/live_positions.json`, `logs/live_bot.log`, environment/config settings,
and the seeded model artifacts. It reports whether live credentials are present
without exposing credential values.

The bot saves `data/live_positions.json` after every dry-run or live entry, so
the dashboard should show test entries during a long scan instead of waiting
for `cycle_end`. If you run the bot and dashboard in separate shells, keep
`WEATHER_ARB_DATA_DIR` and `WEATHER_ARB_LOG_DIR` identical in both shells.
The dashboard also attempts to mark each position from the live CLOB book for
per-position PnL. If a token has no two-sided book or the machine is offline,
the PnL cell is left blank while the rest of the dashboard continues to load.
When live credentials are set, the dashboard also shows the CLOB collateral
balance and allowance. That account lookup is read-only and timeout-bounded;
if credentials are missing or connectivity drops, the balance card reports the
error while positions, logs, and PnL continue to render. For proxy/funder
wallets, the dashboard also reads the configured funder's Polygon collateral
balance, preferring pUSD and falling back to USDC.e/native USDC when present,
and uses that as the top-line account balance when the CLOB balance cache
reports zero. The account details panel still shows the separate CLOB
balance/allowance values so mismatches are visible.

## Required Environment For Live Trading

Set these only after dry-run validation passes:

```powershell
$env:DRY_RUN="false"
$env:POLYMARKET_API_KEY="..."
$env:POLYMARKET_API_SECRET="..."
$env:POLYMARKET_API_PASSPHRASE="..."
$env:POLYMARKET_PRIVATE_KEY="..."
```

On live startup, the bot validates the CLOB API key triplet with an
authenticated read. If Polymarket returns `401 Unauthorized/Invalid api key`,
the bot derives fresh CLOB API credentials from `POLYMARKET_PRIVATE_KEY`, keeps
them in memory, and updates the existing repo-root `.env` file by default. Set
`POLYMARKET_AUTH_WRITE_DOTENV=false` to keep the repaired credentials only in
the current process.

If you use a proxy/funder wallet, set the reconciliation address to the wallet
that Polymarket's Data API shows as holding positions:

```powershell
$env:POLYMARKET_RECONCILE_USER_ADDRESS="0x..."
```

Optional:

```powershell
$env:POLYMARKET_CLOB_HOST="https://clob.polymarket.com"
$env:POLYMARKET_SIGNATURE_TYPE="2" # 0=EOA, 1=POLY_PROXY/Magic, 2=GNOSIS_SAFE
$env:POLYMARKET_FUNDER_ADDRESS="..."
$env:POLY_BUILDER_CODE="0x..." # optional V2 builder attribution bytes32
$env:POLYMARKET_WALLET_BALANCE_TTL_SECONDS="60"
$env:POLYMARKET_WALLET_BALANCE_PREFLIGHT_FALLBACK="true"
$env:POLYGON_RPC_URL="https://..."
$env:POLL_INTERVAL_MINUTES="15"
$env:OFFLINE_RETRY_SECONDS="60"
$env:RECONCILE_ON_STARTUP="true"
$env:MAX_POSITION_USD="2.50"
$env:LIVE_MARKET_LIMIT="0"
$env:ENABLE_NO_SIDE="true"
$env:POLYMARKET_MARKET_WS_ENABLED="true"
$env:POLYMARKET_USER_WS_ENABLED="true"
$env:SAFETY_RECONCILE_INTERVAL_MINUTES="60"
```

For proxy wallets, the private key must be the signer for the configured
`POLYMARKET_FUNDER_ADDRESS`. On startup and before live orders, the bot checks
Polymarket Proxy wallet clones by deriving the expected proxy from the signer;
if the signer derives a different proxy, it stops before submitting an order
with a `POLYMARKET_PRIVATE_KEY does not control ...` error. Use signature type
`1` for Polymarket Proxy/Magic wallets and `2` for Gnosis Safe/browser wallets.
If you participate in the Builder Program, set `POLY_BUILDER_CODE` to the
bytes32 code from your Builder Profile. V2 attaches that code directly to the
signed order; the old builder HMAC order-signing headers are not used.

Run one live cycle first:

```powershell
uv run python -m weather_arb_live.live_bot --once
```

Then run continuously only after confirming the log output and posted order
behavior:

```powershell
uv run python -m weather_arb_live.live_bot
```

## Runtime Files

- `logs/live_bot.log` contains startup, skip, enter, and order logs.
- `data/live_positions.json` tracks entered markets across restarts.
- `data/live_events.jsonl` is an append-only structured event log for bot
  actions such as `signal_generated`, `order_submitted`,
  `order_acknowledged`, `order_partially_filled`, `order_filled`,
  `order_cancelled`, `position_closed`, and `market_resolved`.
- `data/market_snapshots.jsonl` and `data/forecast_snapshots.jsonl` append
  periodic snapshots for only markets the bot has touched through a signal,
  order, position, or reconciliation guard. The default cadence is controlled
  by `EVENT_SNAPSHOT_INTERVAL_MINUTES=5`.
- `data/weather_cache.json` caches Open-Meteo forecast responses.

The JSONL event rows include UTC timestamps and the PnL-explanatory fields
needed for post-live analysis: market/city/date/bracket, side, model
probability, intended edge, bid/ask/mid, submitted limit, fill price and size,
fees, queue/cancel timing when available, realized or mark-to-market PnL, and
final payout when a market resolution message is observed. Unknown values are
kept as `null` so later report code can distinguish missing telemetry from
zero-valued outcomes.

Create a daily Excel report from those append-only files with:

```powershell
uv run python -m weather_arb_live.daily_report --date 2026-04-26
```

If `--date` is omitted, the command uses today's UTC date. Reports are written
to `reports/daily_report_YYYY-MM-DD.xlsx` and can be generated while the bot is
running. The reader skips any incomplete JSONL line it sees during a concurrent
append, so the bot does not need to stop for daily review. The workbook includes
summary metrics, event counts, market-quality rows, signal-review flags, latest
market and forecast snapshots, and raw events.

When `DRY_RUN=false` and `RECONCILE_ON_STARTUP=true`, the bot queries CLOB
open orders and Polymarket Data API positions before trading. Any live
exchange exposure in an active weather market gets a local guard row in
`data/live_positions.json`, so a cloud restart or lost local file does not
blindly re-enter that market. If reconciliation cannot complete, continuous
mode keeps retrying after `OFFLINE_RETRY_SECONDS` and does not trade.
After startup, live mode repeats that REST reconciliation on
`SAFETY_RECONCILE_INTERVAL_MINUTES`, reusing the current Gamma market list
when available. Reconciliation remains the source of truth after WebSocket
reconnects; streamed user order/trade messages are treated as live telemetry,
not as authoritative position state.

Before each live order, the bot refreshes CLOB collateral balance/allowance and
blocks the order locally if either value is below `MAX_POSITION_USD` for that
entry. If the CLOB balance or allowance cache reports exactly zero for a
proxy/funder wallet, the bot can fall back to read-only on-chain collateral
balance and allowance checks for the configured funder. Set
`POLYMARKET_WALLET_BALANCE_PREFLIGHT_FALLBACK=false` to disable that fallback.
A balance preflight failure does not create an `unknown` ledger row, because no
order has been submitted yet. Live submissions use FOK orders, so an unfilled
signal cancels immediately instead of resting on the book for a later stale
fill.

If your internet drops while the bot is running continuously, the bot logs the
failed fetch, leaves existing positions untouched, and retries after
`OFFLINE_RETRY_SECONDS`. Transient forecast failures are not written into
`data/weather_cache.json`, so recovered connectivity can produce fresh signals
on the next cycle.

If the connection drops during a live order submission, the bot fails closed:
it records the market in `data/live_positions.json` with
`order_response.posted="unknown"` and does not re-enter that market
automatically. Check Polymarket manually before clearing that row.

If startup reconciliation cannot find a matching exchange order or position for
an existing live local row, the bot keeps the row and marks it with
`reconciliation.requires_manual_review=true`. Review Polymarket before deleting
or editing that row.

To allow the bot to consider markets again after a dry-run validation, move or
delete `data/live_positions.json`. Do this only when you intentionally want to
clear the one-entry-per-market guard.

## Cloud Hosting Notes

For a VPS, EC2, or Droplet, run the bot under `systemd`, Docker restart policy,
or another process manager. Keep `.env` secrets on the server only, and keep
`data/` on persistent disk so ledgers, caches, and reconciliation guards survive
restarts. Start cloud deployment with `DRY_RUN=true`, then switch to
`DRY_RUN=false` only after logs and `data/live_positions.json` look correct.

## Strategy Artifacts

The repo includes small seed artifacts copied from the backtest project:

- `data/empirical_residuals.json`
- `data/calibration_table.json`
- `data/sigma_cache.json`

Large backtest/raw/runtime files are intentionally ignored.

## Current CLOB SDK

This implementation uses Polymarket CLOB v2 SDK conventions. The live bot defaults to `https://clob.polymarket.com` because Gamma active markets are production markets and the v2 test host can have sparse or empty books. Set `POLYMARKET_CLOB_HOST=https://clob-v2.polymarket.com` only when you intentionally want to validate against the test host.
