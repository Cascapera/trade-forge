# ADR-0010 — `strategies` é append-only, e o banco é quem garante

- **Status**: aceito
- **Data**: 2026-07-14
- **Contexto do PR**: PR-101

## Contexto

O `sdd.md §5` diz que estratégias são "imutáveis por versão" — e, na mesma linha, lista uma coluna `updated_at` na tabela. As duas coisas não podem ser verdade ao mesmo tempo: se nada nunca muda, não há o que datar.

O motivo da imutabilidade é reprodutibilidade. Um backtest é uma afirmação sobre uma estratégia específica. Se a definição puder ser editada no lugar, todo resultado já produzido vira inexplicável: a linha para a qual o backtest aponta não descreve mais a corrida que gerou aqueles números.

A `Strategy` da DSL (PR-004) já carrega `name`, `description`, `schema_version` e `timeframe` dentro do próprio documento JSON. Ou seja: o "rótulo" também faz parte do documento versionado.

## Decisão

A tabela `strategies` é **append-only**. Editar uma estratégia é inserir a próxima versão, apontando para a anterior via `parent_version_id`. Um **trigger** `BEFORE UPDATE` rejeita qualquer UPDATE.

`name`, `description` e `schema_version` são **colunas geradas** (`GENERATED ALWAYS AS (definition ->> '...') STORED`) — projeções indexáveis do JSONB, que por construção não podem divergir dele. Não existe `updated_at`.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Trigger + colunas geradas** (escolhida) | A regra sobrevive a script de manutenção, ORM com bug e psql às 2h da manhã; rótulo e documento não podem divergir | Trigger é lógica no banco — um lugar a mais para olhar quando algo dá errado |
| Imutabilidade só na aplicação | Nada de PL/pgSQL; tudo em Python | Uma regra que só existe na aplicação é uma regra que o próximo script quebra. E o critério de aceite do PR-101 pede *teste de constraint* |
| `definition` congelada, mas `name`/`description` editáveis | Renomear sem criar versão | O `name` está *dentro* do documento da DSL. Ou o banco passa a contradizer o JSON, ou o JSON vira mutável — e aí o hash da estratégia não significa mais nada |
| `REVOKE UPDATE` no papel da aplicação | Sem trigger | Protege contra a aplicação, não contra o dono do banco; e some numa restauração de dump |

## Trade-off aceito

Renomear uma estratégia cria uma versão nova. É mais cerimônia do que renomear um arquivo — e é o preço de poder abrir um backtest de seis meses atrás e saber exatamente o que rodou.

Contrariamos o `sdd.md §5` em um ponto concreto: **não existe `strategies.updated_at`**.

## Consequências

- A API (PR-107) nunca emite UPDATE em estratégia. O endpoint de edição é um POST que insere a versão seguinte.
- `backtests.strategy_id` aponta para a **versão exata**, com `ON DELETE RESTRICT`: um backtest não pode ficar órfão.
- `UNIQUE (name, version)` e `CHECK ((version = 1) = (parent_version_id IS NULL))` — uma v1 não tem pai, e nenhuma versão posterior deixa de ter.
- Existe um teste de integração que tenta o UPDATE e exige que o banco recuse (`test_a_strategy_cannot_be_updated`).
