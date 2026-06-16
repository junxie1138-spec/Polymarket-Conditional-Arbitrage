# Repository Guidelines

## Project Scope

This repo is a local paper-only Polymarket conditional-arbitrage portfolio runner. Keep changes inside the paper trading boundary unless the user explicitly asks otherwise: do not add live order placement, wallet/key handling, contract calls, credential derivation, or live-trading modes.

The current product target is a single local paper portfolio instance with `$1,000` starting capital, default `$100` per-trade ceiling, paired YES/NO tranche execution, and MTM plus realized accounting.

## Commands

Use these commands from the repository root:

```powershell
uv sync --extra dev
uv run pytest -p no:cacheprovider
uv run poly-cond-arb
uv run poly-cond-arb status
uv run poly-cond-arb status --once
uv run poly-cond-arb reset --yes
```

Prefer focused tests while iterating, then run the full pytest command before handing off material behavior changes.

## Runtime And Data

Generated runtime artifacts live under `data/` and `logs/`. Treat them as local operational state unless the user asks to inspect or preserve a specific artifact.

Important runtime files:

- `data/paper_portfolio_instance.json`: source of truth for the local paper portfolio.
- `data/paper_portfolio_events.jsonl`: append-only paper event log.
- `data/paper_portfolio_runtime.json`: runner heartbeat and current phase for `status`.
- `data/paper_portfolio_instance.json.lock`: run/reset process lock.
- `data/market_universe_cache.json`: generated market-universe cache.
- `data/polymarket_latency_report.json`: optional saved public latency probe report.

Do not revert or delete user-generated data casually. If a test or smoke run creates unwanted artifacts, explain what changed before cleaning them up.

## Implementation Rules

- Keep `status` read-only and lock-free.
- `run` and `reset --yes` are the paths that acquire the portfolio lock.
- Preserve stale-lock semantics based on host/PID metadata.
- Paper executions should be persisted atomically and should not be replayed after partial post-execution failures.
- Retry/recovery changes should stay scoped to paper runtime behavior, startup/bootstrap, REST polling, WebSocket seeding, and reconciliation unless explicitly widened.
- Duplicate execution prevention should continue to key off executed tranche/book state and any source revision/fingerprint fields already used by the code.

## Style

Follow the existing Python style: dataclasses and small helper functions, explicit runtime state, and standard-library types where practical. Keep comments sparse and use them only to clarify non-obvious state transitions.

Do not introduce broad abstractions or new services for narrow changes. Prefer extending the existing modules in `polymarket_conditional_arb/` and focused tests in `tests/`.

## Verification

For CLI/state work, cover the relevant behavior with regression tests where practical. Useful areas include startup retry, REST cycle retry, WebSocket seed retry, duplicate prevention, `.tmp` ignore behavior, second-run lock failure, `status` no-lock/no-mutation, and `reset --yes` lock acquisition.

The default full verification command is:

```powershell
uv run pytest -p no:cacheprovider
```
