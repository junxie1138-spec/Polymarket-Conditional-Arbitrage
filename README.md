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

Print current portfolio performance:

```powershell
uv run poly-cond-arb status
```

Reset the local simulated portfolio:

```powershell
uv run poly-cond-arb reset --yes
```

## Outputs

- `logs/conditional_arb_scan.log`: human-readable portfolio runner log.
- `data/paper_portfolio_instance.json`: current cash, equity, realized PnL, costs, executions, inventory, book fingerprints, and metadata.
- `data/paper_portfolio_events.jsonl`: append-only portfolio lifecycle, cycle, and execution events.

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
- `POLYMARKET_CLOB_HOST`

`COND_ARB_MARKET_LIMIT=0` means no local validation cap. The WebSocket path is enabled by default; REST `/books` remains in use for startup seeding and periodic reconciliation.
