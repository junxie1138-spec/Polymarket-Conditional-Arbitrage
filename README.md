# Polymarket Conditional Arbitrage Scanner

Paper-only scanner for Polymarket same-condition arbitrage. It discovers all active Gamma events, seeds public CLOB order books over REST, listens to public CLOB market WebSocket updates for live cache refreshes, evaluates standard binary YES+NO complete-set opportunities, and optionally solves neg-risk event groups with a linear program.

The scanner does not place orders, derive API credentials, read private keys, reconcile wallet state, or provide a fail-open live mode.

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

Run one scan and print JSON:

```powershell
uv run poly-cond-arb-scan --once --json
```

Run a bounded validation scan:

```powershell
uv run poly-cond-arb-scan --once --limit 25 --max-capital-usd 20
```

Run continuously:

```powershell
uv run poly-cond-arb-scan
```

Run continuously with the REST-only polling path:

```powershell
uv run poly-cond-arb-scan --no-market-ws --poll-interval-seconds 60
```

Disable neg-risk event-group solving:

```powershell
uv run poly-cond-arb-scan --once --no-neg-risk
```

## Outputs

- `logs/conditional_arb_scan.log`: human-readable scanner log.
- `data/conditional_arb_events.jsonl`: append-only cycle and opportunity events.
- `data/conditional_arb_opportunities.json`: latest scan snapshot.
- `data/paper_conditional_arb_ledger.json`: paper alert ledger keyed by opportunity id.

## Environment Variables

- `COND_ARB_DATA_DIR`
- `COND_ARB_LOG_DIR`
- `COND_ARB_MARKET_LIMIT`
- `COND_ARB_POLL_INTERVAL_SECONDS`
- `COND_ARB_MARKET_WS_ENABLED`
- `COND_ARB_MARKET_WS_ENDPOINT`
- `COND_ARB_MARKET_WS_HEARTBEAT_SECONDS`
- `COND_ARB_MARKET_WS_MAX_ASSETS_PER_CONNECTION`
- `COND_ARB_MARKET_REFRESH_INTERVAL_SECONDS`
- `COND_ARB_REST_RECONCILE_INTERVAL_SECONDS`
- `COND_ARB_WS_STALE_SECONDS`
- `COND_ARB_MIN_NET_PROFIT_USD`
- `COND_ARB_MIN_NET_RETURN_BPS`
- `COND_ARB_MAX_CAPITAL_USD`
- `COND_ARB_SLIPPAGE_BUFFER_BPS`
- `COND_ARB_GAS_COST_USD`
- `COND_ARB_TAKER_FEE_BPS`
- `COND_ARB_MAX_BOOK_AGE_SECONDS`
- `COND_ARB_INCLUDE_NEG_RISK`
- `POLYMARKET_CLOB_HOST`

`COND_ARB_MARKET_LIMIT=0` means no local validation cap. Gamma discovery is always requested with `closed=false` and without weather or tag filtering. The WebSocket path is enabled by default; REST `/books` remains in use for startup seeding and periodic reconciliation.
