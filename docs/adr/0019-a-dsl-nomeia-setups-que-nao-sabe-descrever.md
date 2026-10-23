# ADR-0019 — A DSL **nomeia** setups que não sabe descrever

- **Status**: aceito
- **Data**: 2026-07-30
- **Contexto do PR**: PR-214

## Contexto

O `specs/fase-2.md` planejou os setups do livro do Guilherme como açúcar sintático sobre a DSL existente:

> **PR-202 — Setups nomeados (técnicas do livro).** Escopo: mecanismo de "setup composto": um bloco nomeado (ex.: `setup_9_1`) que **expande para árvore de condições**.

Esse plano não sobreviveu ao contato com o método. Do PR-202 ao PR-213 foram construídos seis setups, e nenhum deles é uma árvore de condições. São **máquinas de estado**:

- `Mme9BreakoutStrategy` lembra qual ordem está pendurada, em qual barra de referência, e se a virada atual já deu o seu trade (`_spent`);
- `StructureStrategy` mantém uma escada de zonas, o conjunto de regiões já negociadas, e quantos rompimentos a posição aberta já viu;
- `PontoContinuoStrategy` conta correções consecutivas, trava uma qualificação, e a queima num fechamento abaixo da média;
- os três **conduzem** o stop, o que exige lembrar o instante da entrada e o nível vigente.

A diferença não é de poder expressivo, é de **memória**. Uma condição enxerga um candle fechado e responde sim ou não; `all`/`any`/`not` compõem respostas, não estado. Nenhum aninhamento de condições expressa "a ordem que eu pendurei duas barras atrás ainda está lá, e esta barra a substitui". Expandir um setup para condições exigiria a DSL ganhar variáveis, atribuição e ciclo de vida de ordem — ou seja, virar uma linguagem de programação, que é exatamente o que o `sdd.md §3` recusa ao escolher uma DSL declarativa versionada.

Consequência prática, medida: `apps/api/.../runner.py` só chamava `compile_strategy(definition)`, que interpreta indicador + comparação. **Nenhum dos seis setups era lançável pela API ou pela UI.** Tudo que foi construído desde o PR-202 era invisível para o sistema que sobe no `docker compose up`.

## Decisão

A DSL ganha um bloco `setup` opcional: uma união discriminada por `type` que **nomeia** uma estratégia da engine e lhe passa parâmetros, em vez de descrevê-la. Um documento é *ou* condições (`indicators` + `entry`) *ou* um setup nomeado — nunca os dois, nunca nenhum — e `compile_strategy` passa a devolver o protocolo `Strategy`, construindo a classe quando o bloco está presente.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **A — bloco `setup` opcional no documento atual** (escolhida) | Aditiva: documentos salvos continuam válidos e `schema_version` fica em `"1.0"` (ADR-0013). Um só cabeçalho (nome, timeframe, risco) para as duas formas. Um só ponto de entrada (`compile_strategy`), então nenhum chamador ganha `if`. | O documento passa a ter duas formas válidas e o JSON Schema sozinho não expressa o "ou exclusivo" — precisa do `semantic.py`. |
| B — união discriminada no topo (`kind: "conditions" \| "setup"`) | Cada forma exige exatamente o que precisa; o XOR fica no schema, sem camada semântica. | Obriga todo documento salvo a ganhar um campo `kind`, ou a conviver com um default que reintroduz a ambiguidade. Duplica o cabeçalho comum em dois modelos. |
| C — registro de setups fora da DSL (endpoint próprio) | Não mexe na DSL. | Joga fora a imutabilidade por versão do ADR-0013: um setup lançado precisa ser um documento salvo e congelado, senão um backtest de três meses atrás não é reproduzível. E dá à UI duas maneiras diferentes de lançar a mesma coisa. |
| D — estender a DSL com estado (variáveis, ciclo de vida de ordem) | Setups voltariam a ser expressáveis; o plano original do spec sobreviveria. | Transforma a DSL numa linguagem de programação, contra o `sdd.md §3`. E o LLM da Fase 3 passaria a gerar programas em vez de documentos, com uma superfície de erro incomparavelmente maior. |

## Trade-off aceito

**Sacrificamos a completude do JSON Schema.** Ele deixa de ser suficiente para dizer se um documento é bem formado: aceita um que traga `setup` *e* `entry`, e é o `semantic.py` que recusa. Isso não é um furo novo — é a mesma fronteira fixada no PR-004 e afirmada pelas fixtures em `invalid-semantic/`, que **provam que o ajv as aceita**: o schema valida *forma*, a camada Python valida *significado*, e nunca se deve assumir que um documento aprovado no browser é executável. O bloco `setup` mora do lado certo dessa linha porque a regra é sobre *quais campos podem coexistir*, não sobre a forma de campo nenhum.

O segundo sacrifício é uma **duplicação deliberada de defaults**: o número (período 20, breakeven 2x1) vive na classe da engine e é repetido no modelo Pydantic. Não dá para ter um só — um default que existe só em Python é invisível para o builder, para os tipos TypeScript gerados e para o LLM da Fase 3, e uma tela que não sabe mostrar "20" obriga o usuário a adivinhar. A mitigação é que a fábrica **não** guarda uma terceira cópia (parâmetro ausente simplesmente não é passado, então o default da classe vale) e um teste em `tests/test_setup_defaults.py` compara as duas fontes e falha nomeando as duas.

## Consequências

- **Schema.** `Strategy.setup: Setup | None = None`; `entry` e `exit` ganham default para que um documento de setup possa omiti-los. `schema_version` permanece `"1.0"` (ADR-0013: mudança aditiva não bumpa). `strategy.schema.json` e os tipos TS são regenerados.
- **Semântica.** Documento com `setup` recusa `indicators`, `entry`, `exit.stop_loss` e `exit.conditions` — recusa, não ignora, porque cada um seria uma segunda opinião sem árbitro. E a regra "alvo em múltiplo de risco exige `stop_loss`" **não se aplica** a ele: o setup põe o próprio stop a partir da barra de referência, então o risco existe sem o campo. Aplicá-la rejeitaria justamente os documentos que interessam.
- **Engine.** `compile_strategy` devolve o protocolo `Strategy`, não `CompiledStrategy`. Quem precisar do tipo concreto estreita explicitamente. O `setup_factory.py` é o único lugar que traduz nome em classe, e falha alto num nome que não conhece — mesma doutrina do `build_indicator` e do construtor de `CostModel`.
- **Runner.** Nenhuma mudança: já passava o resultado de `compile_strategy` como `strategy=`, e o parâmetro sempre foi tipado pelo protocolo.
- **UI.** Fora do escopo deste PR por decisão explícita. O builder visual continua produzindo só documentos de condição, e o tipo `ConditionStrategy` em `apps/web/src/strategy/builder.ts` registra isso — é onde a mudança começa quando a tela aprender a montar setups.
- **O que este ADR não resolve.** "Várias estratégias no mesmo ativo de uma vez" continua não existindo: o runner compila **uma** estratégia por execução. É outro problema de arquitetura (risco por estratégia ou de portfólio? duas posições simultâneas no mesmo ativo? quem ganha em sinais opostos?) e fica para um PR próprio.
- **O spec.** `specs/fase-2.md` PR-202 descreve um mecanismo que não foi construído. Fica registrado aqui em vez de reescrito lá: o spec é o plano de execução como foi feito, e este ADR é onde a realidade discordou dele.
