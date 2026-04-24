# Polymarket Weather Live Bot

Standalone live bot for the fixed_v1 Polymarket weather arbitrage strategy. It is intentionally separated from the backtesting repository and carries only the runtime strategy/model code needed to poll active weather markets, compute fixed_v1 entries, and place dry-run or live CLOB orders.

## Safety Defaults

- `DRY_RUN=true` by default. The bot logs intended orders and skips actual order posting.
- `data/live_positions.json` is ignored by Git and prevents one re-entry per market across restarts.
- `logs/` is ignored by Git.
- Live trading requires Polymarket CLOB credentials in environment variables.

## Setup

This repo uses `uv` because the old backtest venv is not portable.

```powershell
uv sync --extra dev
```

Run one dry-run poll cycle:

```powershell
$env:DRY_RUN="true"
uv run python -m weather_arb_live.live_bot --once
```

Run continuously:

```powershell
$env:DRY_RUN="true"
uv run python -m weather_arb_live.live_bot
```

## Required Environment For Live Trading

Set these only after dry-run validation passes:

- `POLYMARKET_API_KEY`
- `POLYMARKET_API_SECRET`
- `POLYMARKET_API_PASSPHRASE`
- `POLYMARKET_PRIVATE_KEY`

Optional:

- `POLYMARKET_CLOB_HOST`
- `POLYMARKET_SIGNATURE_TYPE`
- `POLYMARKET_FUNDER_ADDRESS`
- `POLL_INTERVAL_MINUTES`
- `MAX_POSITION_USD`
- `LIVE_MARKET_LIMIT` for bounded validation runs; leave unset or `0` for production.

## Strategy Artifacts

The repo includes small seed artifacts copied from the backtest project:

- `data/empirical_residuals.json`
- `data/calibration_table.json`
- `data/sigma_cache.json`

Large backtest/raw/runtime files are intentionally ignored.

## Current CLOB SDK

This implementation uses Polymarket CLOB v2 SDK conventions. Polymarket's docs state V2 is testable at `https://clob-v2.polymarket.com` before the April 28, 2026 cutover, and production moves to `https://clob.polymarket.com` after that. The bot chooses the default host dynamically unless `POLYMARKET_CLOB_HOST` is set.
