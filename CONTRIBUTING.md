# Contributing to TradeForge

Thanks for taking a look. This document is the short version of how the project is built and
what a change has to clear to land. The long-form design rationale is in [`sdd.md`](sdd.md).

## Getting set up

Follow the [Quickstart](README.md#quickstart) — it installs the workspace, brings up Postgres
and Redis, and gets a backtest running. Then wire the local gates to match CI:

```bash
uv run pre-commit install --install-hooks
```

## The workflow

Work lands through pull requests. `main` and `develop` never take direct commits.

```
feat/pr-XXX-slug  →  develop  →  main
```

1. **Branch** off `develop`: `feat/…`, `fix/…`, `chore/…`, `docs/…`.
2. **One PR, one scope.** Out-of-scope work you notice goes in [`specs/backlog.md`](specs/backlog.md),
   not into the diff.
3. **Tests come with the change** — a bug fix carries the test that would have caught it; a
   feature carries the tests that pin its behaviour.
4. **Conventional Commits.** `feat:`, `fix:`, `test:`, `docs:`, `chore:`, `refactor:`, with a
   scope from the enum in `commitlint.config.js` (`engine`, `db`, `api`, `web`, `schema`, …).
   `commitlint` enforces it.
5. Open the PR against **`develop`** and make sure every gate below is green.

## Quality gates

Every one of these runs in CI and blocks the merge. They are not advisory — the table lives in
the [README](README.md#quality-gates). Run them locally before pushing:

```bash
uv run pytest                       # Python tests + 90% coverage
uv run pytest -m integration        # against real Postgres/Redis (needs `docker compose up`)
npm run lint --workspaces           # eslint (type-aware) + tsc, no `any`
npm run test:cov --workspaces       # TS tests + 90% coverage
uv run pre-commit run --all-files   # ruff, mypy --strict, gitleaks, commitlint
```

## Conventions that matter

**Python (3.12+).** Full type hints; `mypy --strict` on the engine core. `ruff` for lint and
format. Pydantic v2 for API models; `Protocol` for engine interfaces. The engine has *golden*
tests (results worked by hand) plus property-based tests (`hypothesis`).

**TypeScript / React.** Strict mode, no `any`. Server state is React Query; UI state is Zustand;
styling is Tailwind. **DSL types are generated from the shared JSON Schema in `packages/schema`,
never hand-written** — a copy would drift from the contract.

**General.** Code and comments in English. Secrets only through environment variables; never
commit `.env`.

## Architecture invariants

A change that breaks one of these is a critical bug, even with green tests. They are why the
project exists (see [`sdd.md`](sdd.md) §5 and the README's *Invariants*):

- **No lookahead** — a decision at the close of candle N fills at the open of N+1; indicators
  only ever see closed candles.
- **Determinism** — the same input always produces the same output.
- **Broker agnosticism** — only `apps/collector` and `apps/executor` may import `MetaTrader5`;
  a test asserts it, and CI has no MT5 wheel to fall back on.
- **Costs are pluggable** — no spread/commission/swap logic hard-coded in the engine; it always
  goes through a `CostModel`.

## New architecture decisions

Anything that contradicts or extends `sdd.md` (a new dependency, a changed contract, a new
service) gets an ADR under [`docs/adr/`](docs/adr/) using the template there, explaining the
trade-off.
