# Fase 0 — Fundação (~1 semana)

**Objetivo:** esqueleto do monorepo rodando com CI verde e o contrato central (JSON Schema da DSL) definido.
**Referência:** sdd.md §3.1, §4, §8 (Fase 0), §9.

## PR-001 — Monorepo, tooling e quality gates
> **Nota de escopo:** este PR absorveu o antigo PR-003 (CI). Motivo: se a CI só chega no
> terceiro PR, o histórico do git mostra dois PRs que entraram sem gate nenhum — inaceitável
> num repo que é vitrine. Gates existem desde o commit #1.

**Escopo:** estrutura `apps/{api,web,collector,executor}` + `packages/{engine,schema}`; uv workspace (venv único, lock único) com Python 3.12 pinado; frontend Vite+React+TS; ruff + mypy strict + ESLint type-aware; pre-commit; CI no GitHub Actions (lint, tipos, testes, cobertura ≥90%, gitleaks no histórico completo, CodeQL, pip-audit/npm audit, commitlint); Dependabot; Apache 2.0; `.env.example`; `.gitignore` (inclui `docs/aulas/`, que não vai para o repo público).

**Aceite:** `uv run pytest` (cobertura ≥90%) e `npm run lint` passam localmente; CI verde no PR; teste de arquitetura falha se algo fora de `collector`/`executor` importar `MetaTrader5`.

**Você vai aprender:** layout de monorepo Python+TS, por que separar `packages/` (compartilhado, testável isolado) de `apps/` (deployáveis), uv workspace, marcadores de plataforma como fronteira arquitetural executável, e por que se pina GitHub Action por SHA e não por tag.

## PR-002 — Docker Compose (Postgres + Redis)
**Escopo:** `docker-compose.yml` com Postgres 16 e Redis 7, volumes nomeados, healthchecks; script de conexão de teste; convenções de config por env vars (12-factor).
**Aceite:** `docker compose up` sobe os dois serviços saudáveis; app de teste conecta em ambos.
**Você vai aprender:** healthchecks e depends_on, volumes vs bind mounts, 12-factor config.

## ~~PR-003 — CI (GitHub Actions)~~ → absorvido pelo PR-001
CI, cobertura, gitleaks, CodeQL, auditoria de dependências e commitlint entraram no PR-001.
Motivo em `specs/backlog.md` e na nota de escopo acima.

## PR-004 — JSON Schema v1 da DSL de estratégia
**Escopo:** `packages/schema` com o JSON Schema da DSL (sdd.md §4): indicadores (SMA/EMA), árvore de condições (`all/any/not` + `gt/lt/crosses_above/crosses_below/breaks_above/breaks_below`), refs (`price.*`, `candle[-n].*`, ids de indicador), exit (stop candle_extreme, tp risk_multiple, condições), risk (percent_risk, max_open_positions, max_daily_loss_percent), `schema_version`. Validador Python (jsonschema/pydantic) + geração de tipos TS (json-schema-to-typescript). Fixtures: 3 estratégias válidas + 5 inválidas com erros esperados.
**Aceite:** validação passa/falha corretamente nas fixtures nos DOIS lados (py e ts); tipos TS gerados no build.
**Você vai aprender:** design de DSL declarativa, contrato compartilhado entre back e front, árvores de expressão, versionamento de schema.
**Nota:** este é o PR mais importante da fase — o schema é o contrato central do sistema. Discuta o design com o Guilherme antes de fechar.

## Entregável da fase
Repo com CI verde, compose funcional, DSL v1 validável nos dois lados. Nenhuma lógica de negócio ainda.
