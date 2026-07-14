# ADR-0009 — Camada de persistência em `packages/db`, compartilhada

- **Status**: aceito
- **Data**: 2026-07-14
- **Contexto do PR**: PR-101

## Contexto

O `sdd.md §7` desenha o monorepo com `apps/api`, `apps/web`, `apps/collector`, `apps/executor`, `packages/engine` e `packages/schema`. Não previu onde ficam os modelos SQLAlchemy e as migrations — e mais de um serviço precisa deles:

- `apps/api` — CRUD de estratégias, backtests, métricas, trades.
- `apps/collector` — registra `datasets` e `instruments` a partir do `symbol_info` do MT5 (PR-102).
- `apps/executor` — `live_sessions` e `order_audit` (Fase 2).

E um serviço precisa explicitamente **não** ter banco: `packages/engine`. A engine é pura — candles entram, ordens saem. Um core capaz de ler o banco é um core cujo resultado depende do que está lá dentro, e isso mata o determinismo (invariante 2 do `AGENTS.md §5`).

## Decisão

Criar `packages/db` (`tradeforge-db`): modelos SQLAlchemy, migrations Alembic, fábrica de sessão e a config do Postgres. É importado pela API, pelo collector e pelo executor. **Nunca** pela engine.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **`packages/db` compartilhado** (escolhida) | Nenhum app importa outro app; o collector não arrasta FastAPI e Redis para gravar uma linha em `datasets`; uma única definição de schema | Um membro a mais no workspace; estende o `sdd.md §7` (daí este ADR) |
| Modelos dentro de `apps/api` | Zero pacote novo | O collector teria que importar `apps/api` (app dependendo de app, com FastAPI junto) ou duplicar os modelos. Duplicar contrato é exatamente o que o PR-004 eliminou |
| Collector sem banco: só a API escreve, via HTTP | Fronteira limpa, um único escritor | Contraria o escopo do PR-102 no spec; exige a API no ar para rodar um backfill; adiciona autenticação entre serviços por nada |

## Trade-off aceito

Um pacote a mais para manter, e a estrutura do `sdd.md §7` deixa de estar completa sem ler este ADR. Em troca, a dependência entre serviços fica acíclica e o schema tem uma definição só.

## Consequências

- `apps/api` passa a depender de `tradeforge-db`. `Settings` da API agora **herda** de `PostgresSettings`, em vez de repetir os campos do Postgres — a montagem da DSN existe num lugar só.
- `packages/db` depende de `packages/schema`: a lista de timeframes do `CHECK` no banco é derivada da DSL (`TIMEFRAMES`), não é uma segunda cópia.
- A engine continua sem nenhuma dependência de banco. Isso é uma regra, não um acidente — se um dia `packages/engine` listar `tradeforge-db` como dependência, é bug.
