# ADR-0029 — O contexto do mercado é lido uma vez por lote de runs

- **Status**: aceito
- **Data**: 2026-09-27
- **Contexto do PR**: PR-333 (esta ADR); implementação em PR-334 a PR-337
- **Relaciona-se com**: ADR-0026 (o time frame superior nasce dentro do setup), AGENTS.md §5.2 e §5.3

## Contexto

Uma varredura roda o mesmo gráfico dezenas de milhares de vezes, e quase tudo o que cada run
calcula é igual ao run do lado. A varredura AUDUSD de 26–27/09 (entrada `structure_continuation`,
janela 2020–2025) mostra o tamanho disso no M5:

- **65.664 runs** = 144 contextos (htf × lado × max_bos × secundária) × 19 gatilhos × 24 saídas.
- Cada run percorre **372 mil barras** e leva ~11–13 s no i9 (depois do PR-332), para fazer
  **~14 trades**. Só **7–15% das barras** têm algo que dependa do ponto: zona armada, ordem,
  posição, quebra de estrutura com a conta vazia.
- **~58% do tempo de cada run** vai para a leitura do mercado, que não depende do ponto: a
  estrutura e as regiões do gráfico (`MarketStructure`, `OrderBlockDetector`) e a parte do gate
  do time frame superior que acompanha regiões e toques (`HigherTimeframeGate`). Essa leitura é
  **a mesma** para os 10.944 runs de cada htf.
- M5, M15 e M30 são mais de 90% do tempo de uma varredura. H1 para cima já é barato.

Ele propôs (27/09) um **funil**: montar o contexto uma vez e, a partir de onde ele libera uma
entrada, rodar as variações de entrada e de saída. A condição dele: **sem perder qualidade no
resultado**.

A investigação de 27/09 mapeou o estado da estratégia barra a barra
(`scratchpad/funil`, fora do repo) e encontrou uma fronteira limpa:

- **Pura** (função só dos candles, do htf e do offset): `MarketStructure`, `OrderBlockDetector`
  (inclusive `TrackedZone.mitigated`, que só o detector escreve), e no gate o agregador de barras,
  a estrutura e as regiões do time frame superior, `_touched`, `_untouched` e as regiões
  alcançadas em cada barra.
- **Dependente do caminho**: `_releases` do gate (mexido por `spend` ao armar e por `restore` ao
  recusar), o qualificador inteiro (escada de BOS, `_since_choch`, só consultado com a conta sem
  posição), `_armed`, `_traded`, `_filled`, o trailing, o estado das ativações, o broker
  (ordens, proteção, saldo, patrimônio) e as recusas guardadas pelo loop.
- A camada dependente **só lê** a pura. Nenhum campo puro lê estado de trade.

## Decisão

**Dentro de uma varredura, os runs que leem o mesmo mercado (mesmo símbolo, gráfico, janela
exata, htf e offset) rodam juntos num lote: um líder avança a camada pura uma vez por barra, cada
run avança só a sua camada dependente — no mesmo motor, com o mesmo `on_bar` — e cada run pula
as barras em que, pela regra do próprio setup, nada do seu estado pode mudar.** O resultado de
cada run é idêntico, trade a trade, ao de rodá-lo sozinho. O live e o backtest avulso não mudam.

Duas peças, entregues nesta ordem:

**A — leitura do mercado compartilhada (lockstep).**
- O `HigherTimeframeGate` é separado em duas classes: o **rastreador de regiões** (puro:
  agregador, estrutura e regiões do time frame superior, toques) e as **liberações**
  (`_releases`, `spend`, `restore`, fim da busca). Hoje as duas vivem na mesma classe.
- O `StructureStrategy` passa a receber a **leitura do mercado** (estrutura, regiões do gráfico
  e rastreador do time frame superior) por injeção. Sem injeção, ele monta a sua — que é o que
  o live, o paper e o backtest avulso continuam fazendo.
- A leitura injetada é **somente-leitura** para os runs.

**B — pular barras quietas.**
- Cada setup declara, **ao lado do seu `on_bar`**, quando uma barra é quieta para ele. Para o
  `structure_continuation`: sem posição, sem ordem pendente, em repouso ou proteção, nada armado,
  nada acabou de encher, nenhuma recusa a entregar, nenhuma quebra de estrutura nesta barra, e
  (escada vazia ou nenhuma liberação aberta e nenhum toque no time frame superior nesta barra).
- Numa barra quieta o run só avança as liberações do gate e registra o ponto de patrimônio
  (igual ao saldo, porque não há posição). Para qualquer outra estratégia a resposta padrão é
  "nunca quieta", ou seja, o comportamento de hoje.

## Por que o resultado é idêntico

1. A camada pura não lê nada da dependente (mapa acima), então calculá-la uma vez ou N vezes dá
   a mesma sequência — e o motor é determinístico (§5.2).
2. Cada run continua chamando o mesmo `on_bar`, com o mesmo contexto numérico (`ENGINE_CONTEXT`).
3. Numa barra quieta, o `on_bar` não emite nada e `_may_arm` não pode passar; o broker sem posição
   não faz nada; o patrimônio é o saldo. As remoções atrasadas da escada não mudam resultado,
   porque mitigação e poda de região são permanentes.

## Evidência (protótipo, 27/09, sobre develop 849b51c)

- **57 de 57 runs reais idênticos** ao motor atual: trades (`repr`), curva de patrimônio e
  métricas. Os de M5 também batem com o que o banco gravou.
- Ganho medido (lotes de 10–12 runs):

| Gráfico | htf | Motor atual | A + B | Ganho |
|---|---|---|---|---|
| M5 | H4 | 11,30 s/run | 2,42 s/run | 4,7× |
| M5 | M15 | 13,21 s/run | 2,84 s/run | 4,7× |
| M15 | H1 | 4,16 s/run | 0,89 s/run | 4,7× |
| M30 | H4 | 1,91 s/run | 0,39 s/run | 4,9× |

- Só A (sem pular): ~2×. Uma barra quieta ainda custa ~4 µs; saltar direto para a próxima barra
  com evento levaria o teto para ~8–10×, **não medido** — fica para depois de A+B em produção.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| Deixar como está | Nenhum risco novo | M5 inteiro leva horas; 500–800 ativos seriam meses |
| **Peneira rápida + confirmação dos melhores no motor oficial** | Ganho maior | **Não é exata**: um ponto bom descartado nunca aparece; viés de seleção no banco para ML; é uma segunda cópia da lógica da estratégia (§5.3) |
| Cache da camada pura em disco, por worker | Serve entre varreduras | Tem que reproduzir no tempo o estado mutável das regiões (mitigação, poda); mais código e mais memória que o lockstep, mesmo ganho |
| Vetorizar / numba / PyPy no motor | Ganho em tudo | Reescrita do motor inteiro, `Decimal` em todo lugar, risco alto para o determinismo |
| Mais hardware | Nenhuma mudança de código | Linear e caro (energia); ele decidiu trabalhar com o que tem |
| **A + B (esta decisão)** | Exato por construção, mesmo motor, ~4,7× medido nos gráficos pequenos | Mexe no coração do motor; o lote vira a unidade de trabalho do worker |

## Trade-off aceito

- **O lote é a unidade de falha.** Um processo que cai (a CPU instável deste PC, por exemplo)
  perde o lote inteiro, não um run. Mitigação: lotes pequenos (dezenas de runs, ~1 min de M5),
  e o lote refeito pula os runs que já terminaram (`status = done`).
- **Os resultados chegam em rajadas**: os runs de um lote terminam juntos no fim da janela, e a
  tela vê a varredura andar de lote em lote, não de run em run.
- **Mais memória por worker**: N estados de estratégia e broker ao mesmo tempo (pequenos perto
  dos candles, que já são compartilhados).
- **A regra de "barra quieta" é código novo por setup** e precisa ser provada a cada setup que a
  declarar. Setups que não declaram ganham só a parte A.

## Consequências

- **Engine** (`packages/engine`): o gate é separado em rastreador e liberações; o
  `StructureStrategy` aceita a leitura do mercado injetada; os setups podem declarar barra quieta;
  o loop ganha um modo de lote. Todo PR passa pelo engine-guardian. `ENGINE_VERSION` não muda,
  porque o resultado é idêntico.
- **Worker** (`apps/api`): um job novo roda um lote de runs; o lançamento da varredura enfileira
  lotes agrupados pela chave (símbolo, gráfico, janela, htf, offset) em vez de um job por run.
  Um run avulso, o estudo, a cesta e o walk-forward continuam um job por run.
- **Prova de equivalência, obrigatória antes de ligar em produção**:
  1. Teste diferencial motor-atual × lote em ~200 runs reais sorteados por gráfico, com todos os
     tipos de entrada e htfs, comparando trades, curva de patrimônio, métricas e recusas.
  2. Hypothesis com candles sintéticos e parâmetros aleatórios ("lote = runs sozinhos"),
     forçando os caminhos raros (recusa de ordem → `restore`).
  3. Modo sombra numa fração dos runs: rodar o `on_bar` também nas barras quietas e conferir que
     nada sai e o estado não muda.
  4. Teste de que ninguém fora do detector escreve numa região compartilhada.
- **Plano de PRs**:
  - **PR-334** — separar o gate em rastreador de regiões e liberações. Nenhuma mudança de
    resultado; teste diferencial gate antigo × novo.
  - **PR-335** — a leitura do mercado injetável no `StructureStrategy` (N=1 continua montando a
    sua). Nenhuma mudança de resultado.
  - **PR-336** — o lote: loop em lockstep, job de lote no worker, lançamento agrupado, retomada
    que pula runs terminados. Teste diferencial em runs reais.
  - **PR-337** — barra quieta no `structure_continuation` (e nos setups que compartilham a mesma
    condução), com modo sombra.
