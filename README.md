# TradeForge

[![CI](https://github.com/Cascapera/trade-forge/actions/workflows/ci.yml/badge.svg)](https://github.com/Cascapera/trade-forge/actions/workflows/ci.yml)
[![CodeQL](https://github.com/Cascapera/trade-forge/actions/workflows/codeql.yml/badge.svg)](https://github.com/Cascapera/trade-forge/actions/workflows/codeql.yml)
[![codecov](https://codecov.io/gh/Cascapera/trade-forge/branch/main/graph/badge.svg)](https://codecov.io/gh/Cascapera/trade-forge)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/)
[![Ruff](https://img.shields.io/endpoint?url=https://raw.githubusercontent.com/astral-sh/ruff/main/assets/badge/v2.json)](https://github.com/astral-sh/ruff)
[![License](https://img.shields.io/badge/license-Apache%202.0-green.svg)](LICENSE)

**Write a trading strategy once. Backtest it, paper-trade it, and run it live — against the same engine.**

Most backtesting tools let you build a strategy twice: once in a vectorised research
notebook, once again in whatever the live system speaks. The two implementations then
drift, and the gap between the equity curve you backtested and the one your broker
statement shows becomes an unexplained mystery.

TradeForge closes that gap by construction. Strategies are declarative JSON, interpreted
by a single event-driven engine. The engine talks to a `Broker` interface — and whether
that interface is backed by a simulator, a paper account, or a live MetaTrader 5 terminal
is something the strategy never learns.

> ⚠️ Analysis tooling, not investment advice. Trading carries risk of loss.

## Design decisions

The trade-offs that shaped the system, in one line each. The long form lives in
[`sdd.md`](sdd.md) §3.2.

| Decision | Why | Rejected alternative |
|---|---|---|
| **Event-driven engine**, not vectorised | Live trading *is* event-driven; a vectorised core would have to be rewritten for live, reintroducing the divergence this project exists to eliminate | Vectorised backtester (faster to build, forks the strategy in two) |
| **Strategy as declarative JSON**, not Python | The UI composes it, the engine interprets it, an LLM can generate it — and a SaaS can run it without executing user code | Strategies as Python (flexible, but unusable for a no-code UI and unsafe multi-tenant) |
| **`Broker` interface** with swappable implementations | `BacktestBroker`, `PaperBroker`, `MT5Broker` — the core never knows where an order is filled | Coupling the engine directly to MT5 |
| **MT5 isolated at two Windows edges** | The official library is Windows-only; confining it to `apps/collector` and `apps/executor` keeps the other ~90% portable to Linux/Docker | Running the entire stack on Windows |
| **Postgres + Parquet**, not Postgres alone | Years of OHLCV across many symbols is a columnar workload; relational data stays where SQL shines | Everything in Postgres (slow and expensive for candles) |
| **Pluggable `CostModel`** | Forex pays spread and swap, equities pay commission, futures pay both — multi-asset without `if` statements scattered through the engine | Hard-coded transaction costs |

## Invariants

Three properties are enforced by tests on every run, because a backtest that violates any
of them is not merely wrong — it is *confidently* wrong, which is worse:

1. **No lookahead.** A decision taken at the close of candle N is filled at the open of
   candle N+1. Indicators only ever see closed candles.
2. **Determinism.** The same input always produces the same output.
3. **Broker agnosticism.** `MetaTrader5` may only be imported by the collector and the
   executor — asserted in [`tests/test_architecture.py`](tests/test_architecture.py), and
   independently by CI, where no MT5 wheel exists at all.

## Layout

```
apps/api          FastAPI + async workers        (Linux/Docker)
apps/web          React + TypeScript             (Vite)
apps/collector    MT5 → Parquet/Redis            (Windows only)
apps/executor     Orders → MT5, with safeguards  (Windows only)
packages/db       SQLAlchemy models + Alembic migrations
packages/engine   Event-driven engine — the core
packages/schema   Strategy DSL: JSON Schema + generated types
```

`apps/` are deployable; `packages/` are libraries shared between them. Dependencies point
from apps into packages, never the reverse.

## Getting started

Requires [uv](https://docs.astral.sh/uv/), Node 22+ and Docker.

```bash
cp .env.example .env        # then set POSTGRES_PASSWORD — there is no default
uv sync                     # one venv at the root, all six packages editable
npm ci

uv run pre-commit install --install-hooks   # mirrors the CI gates locally

docker compose up -d        # Postgres + Redis, with healthchecks
uv run tradeforge-health    # asks both services whether they are actually up

uv run tradeforge-db upgrade # create the schema (Alembic)
uv run tradeforge-db seed    # example instruments; safe to re-run

uv run pytest               # unit tests + 90% coverage gate (no Docker needed)
uv run pytest -m integration # connects to the real services
npm run test:cov
```

## Quality gates

Every pull request must pass all of them. None are advisory.

| Gate | Tool |
|---|---|
| Lint + format (Python) | `ruff` — including `flake8-bandit` security rules |
| Types (Python) | `mypy --strict` |
| Lint + types (TS) | `eslint` (type-aware) + `tsc --noEmit`, no `any` |
| Tests + coverage | `pytest` and `vitest`, both gated at **90%** |
| Secrets | `gitleaks`, full git history |
| SAST | CodeQL (Python, TS, Actions) |
| Dependencies | `pip-audit` + `npm audit` |
| Commit messages | `commitlint` (Conventional Commits) |

Actions are pinned by commit SHA, not by tag — a tag is mutable, and a supply-chain
attacker only needs it to move. Dependabot keeps the pins fresh.

## Branching

```
feat/pr-XXX-slug  →  develop  →  main
                     (staging)   (production)
```

Work happens on branches and lands through pull requests; `main` and `develop` take
merges, never direct commits (enforced locally by pre-commit, and by branch protection on
GitHub). Automated deploys will hang off these two branches.

## Roadmap

| Phase | Scope | Status |
|---|---|---|
| 0 | Monorepo, CI, strategy DSL schema | in progress |
| 1 | Backtest MVP: engine, collector, API, UI | — |
| 2 | More indicators, grid search, walk-forward | — |
| 3 | Paper → live execution, LLM analyst | — |
| 4 | Multi-user product | — |

## License

[Apache 2.0](LICENSE).
