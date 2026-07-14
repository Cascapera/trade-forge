# ADR-0012 — O núcleo é dono do vocabulário; os adaptadores se conformam

- **Status**: aceito
- **Data**: 2026-07-14
- **Contexto do PR**: PR-103

## Contexto

Quando o PR-101 e o PR-102 foram escritos, a engine ainda não existia. Então os tipos de domínio nasceram onde deu:

- `Candle` foi definido em `apps/collector` (era quem precisava dele primeiro).
- `InstrumentSpec` e `AssetClass` foram definidos em `packages/db`.
- `Direction` (long/short) foi definido em `packages/db`, como um enum de coluna.

Isso está invertido. Significa que a engine — o núcleo, aquele que o `sdd.md §1` chama de diferencial da arquitetura — teria que se conformar ao vocabulário da **persistência**. Um `Candle` moldado pelo que era conveniente gravar em Parquet, um `Direction` moldado por um `CHECK` do Postgres.

E há uma consequência dura: o `AGENTS.md §5` e o ADR-0009 proíbem `packages/engine` de importar `tradeforge_db`. Se os tipos ficassem no banco, a engine teria que **duplicá-los** — e um sistema com duas definições de "candle" é um sistema onde uma delas está errada e ninguém sabe qual.

## Decisão

Os tipos de domínio moram em `packages/engine`. `db` e `collector` importam de lá.

```
packages/engine   Candle, Side, AssetClass, InstrumentSpec, OrderRequest, Fill,
                  Position, AccountState, Signal, Context  (zero dependências)
        ▲
        ├── packages/db      (mapeia domínio → tabelas)
        └── apps/collector   (preenche o domínio a partir do MT5)
```

A regra, em uma frase: **o núcleo define a linguagem; os adaptadores a falam.** A dependência nunca aponta para o outro lado.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Tipos no núcleo** (escolhida) | Uma definição de cada conceito; a engine mantém zero dependências; o banco vira um detalhe de gravação | Um refactor agora, tocando `db` e `collector` |
| Engine define os seus, adaptadores mapeiam nas fronteiras | Nenhum refactor imediato | Três dataclasses duplicadas (`Candle`, `InstrumentSpec`, `Side`/`Direction`) e uma camada de tradução para manter. Duplicação de contrato é exatamente o que o PR-004 eliminou |
| Um `packages/domain` separado, importado por todos | Fronteira explícita | Um pacote a mais para separar duas coisas que nunca vão divergir: o domínio *é* a engine. Ceremônia sem benefício |

## Trade-off aceito

Um refactor no meio da Fase 1, e `packages/db` passa a depender de `packages/engine`. Em troca: a engine continua com **zero dependências** — não é ascetismo, é o mecanismo do determinismo. Uma função dos seus inputs não tem por onde produzir outra resposta amanhã.

## Consequências

- `packages/db` depende de `tradeforge-engine`. O grafo continua acíclico: engine → nada.
- `tradeforge_db.models.Direction` deixou de existir; use `tradeforge_engine.domain.Side`. Os valores no banco (`'long'`, `'short'`) não mudaram — **nenhuma migration foi necessária**.
- `tradeforge_db.instruments.InstrumentSpec` e `tradeforge_db.models.AssetClass` deixaram de existir; vêm de `tradeforge_engine.domain`.
- `tests/test_architecture.py` continua garantindo o inverso: a engine **nunca** importa `tradeforge_db`.
- Os dublês de teste (`tradeforge_engine.testing`) são publicados junto com o pacote. Quem implementar um `Broker` contra esses `Protocol`s ganha uma implementação de referência para testar — que é para isso que serve uma interface.
