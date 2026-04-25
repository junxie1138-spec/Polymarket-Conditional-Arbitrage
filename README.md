# Polymarket Weather Live Bot

Standalone live bot for the fixed_v1_no (Combined) Polymarket weather arbitrage strategy. It is intentionally separated from the backtesting repository and carries only the runtime strategy/model code needed to poll active weather markets, compute fixed_v1_no entries, and place dry-run or live CLOB orders.

The model evaluates YES first using fixed_v1 gates. If YES does not qualify on price, probability, edge, or calibration, it evaluates the NO token with `1 - P(YES)`, mirrored fixed_v1 gates, and a `0.75` maximum NO entry price. It records one side per market.

## Safety Defaults

- `DRY_RUN=true` by default. The bot logs intended orders and skips actual order posting.
- `data/live_positions.json` is ignored by Git and prevents one re-entry per market across restarts.
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

The bot reads environment variables from the current process. PowerShell does
not automatically load `.env`, so either set variables directly in the shell or
load them before running the bot.

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
$env:MAX_POSITION_USD="50"
$env:ENABLE_NO_SIDE="true"
```

Useful validation setting:

```powershell
$env:LIVE_MARKET_LIMIT="10"
```

Use `LIVE_MARKET_LIMIT=10` for bounded validation. Use `0` or leave it unset
for the full live weather market scan.

To load simple `KEY=value` lines from `.env` into the current PowerShell
session:

```powershell
Get-Content .env | Where-Object { $_ -and -not $_.StartsWith("#") } | ForEach-Object {
    $name, $value = $_ -split "=", 2
    Set-Item -Path "Env:$name" -Value $value
}
```

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

## Required Environment For Live Trading

Set these only after dry-run validation passes:

```powershell
$env:DRY_RUN="false"
$env:POLYMARKET_API_KEY="..."
$env:POLYMARKET_API_SECRET="..."
$env:POLYMARKET_API_PASSPHRASE="..."
$env:POLYMARKET_PRIVATE_KEY="..."
```

Optional:

```powershell
$env:POLYMARKET_CLOB_HOST="https://clob.polymarket.com"
$env:POLYMARKET_SIGNATURE_TYPE="..."
$env:POLYMARKET_FUNDER_ADDRESS="..."
$env:POLL_INTERVAL_MINUTES="15"
$env:OFFLINE_RETRY_SECONDS="60"
$env:MAX_POSITION_USD="50"
$env:LIVE_MARKET_LIMIT="0"
$env:ENABLE_NO_SIDE="true"
```

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
- `data/weather_cache.json` caches Open-Meteo forecast responses.

If your internet drops while the bot is running continuously, the bot logs the
failed fetch, leaves existing positions untouched, and retries after
`OFFLINE_RETRY_SECONDS`. Transient forecast failures are not written into
`data/weather_cache.json`, so recovered connectivity can produce fresh signals
on the next cycle.

If the connection drops during a live order submission, the bot fails closed:
it records the market in `data/live_positions.json` with
`order_response.posted="unknown"` and does not re-enter that market
automatically. Check Polymarket manually before clearing that row.

To allow the bot to consider markets again after a dry-run validation, move or
delete `data/live_positions.json`. Do this only when you intentionally want to
clear the one-entry-per-market guard.

## Strategy Artifacts

The repo includes small seed artifacts copied from the backtest project:

- `data/empirical_residuals.json`
- `data/calibration_table.json`
- `data/sigma_cache.json`

Large backtest/raw/runtime files are intentionally ignored.

## Current CLOB SDK

This implementation uses Polymarket CLOB v2 SDK conventions. Polymarket's docs state V2 is testable at `https://clob-v2.polymarket.com` before the April 28, 2026 cutover, and production moves to `https://clob.polymarket.com` after that. The bot chooses the default host dynamically unless `POLYMARKET_CLOB_HOST` is set.
