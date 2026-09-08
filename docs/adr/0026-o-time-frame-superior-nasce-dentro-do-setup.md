# ADR-0026 — O time frame superior nasce dentro do setup

- **Status**: aceito
- **Data**: 2026-09-08
- **Contexto do PR**: PR-205

## Contexto

Ele ditou o primeiro **filtro** das entradas de smart money: *"podemos usar qualquer região do
time frame superior [...] quando o preço atingir uma região no H4 qualquer que ainda não foi
mitigada, a partir deste ponto ele libera de fazer uma entrada no 15 minutos, e é somente uma
entrada por região de H4"*. A entrada continua sendo o CHoCH (ou a continuação) no M15; o que
muda é **se ela pode ser procurada**.

O `sdd.md` e o `loop.py` foram escritos para **um** fluxo de barras: `Context` carrega uma vela,
a que acabou de fechar, e essa é a regra anti-lookahead feita estrutura (§5.1 — *"a estratégia
não é entregue a lista de velas e pedida educadamente para não olhar adiante"*). Um filtro que lê
o H4 precisa de barras de H4 que **nenhuma** parte da engine fornece hoje.

## Decisão

As barras do time frame superior são **montadas dentro do setup**, a partir das barras que ele já
recebe (`BarAggregator`), e os detectores dele — `MarketStructure` e `OrderBlockDetector`, os
mesmos que já são a transcrição do indicador dele — rodam em cima delas dentro de um portão
(`HigherTimeframeGate`). Uma região de H4 existe a partir do **fechamento** da barra de H4 que a
revelou, nunca antes. O loop, o `Context`, o broker e o live não mudam uma linha.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **(a) Segundo fluxo no loop**: `Context` ganha a barra de H4 corrente e o `run()` recebe duas séries | O agregador some; o H4 vem do venue, alinhado ao relógio do servidor | Muda o contrato do loop, do `Context`, do `iter_run`, do splice do live e do aquecimento para **todos** os setups, por causa de um filtro de dois; duas séries que precisam estar alinhadas no tempo são uma nova classe de bug de lookahead (a barra de H4 "corrente" ainda não fechou) |
| **(b) A API pré-calcula as regiões de H4** e entrega como dado ao setup | Engine não sabe o que é H4 | A regra (mitigação, rompimento, 2x) passa a viver **fora** da engine, e no live teria que ser recalculada por outro processo — a invariante §5.3 (estratégia única, backtest = live) quebra silenciosamente |
| **(c) Montar o H4 dentro do setup** a partir do fluxo base | Um fluxo, uma engine, mesmo código em backtest e live; anti-lookahead por construção; os detectores dele reutilizados sem cópia | As barras fecham no relógio **UTC**, não no do servidor do MetaTrader; a estrutura de H4 precisa de aquecimento 16× mais longo em barras de M15 |

## Trade-off aceito

O relógio. Um H4 do MetaTrader fecha em 00h/04h/08h **do servidor da corretora**, que pode estar
deslocado do UTC em horas; o nosso fecha em 00h/04h/08h UTC. A região de H4 que a engine marca
pode diferir da que ele vê no gráfico por esse deslocamento. Fica registrado em `specs/backlog.md`
como pendência conhecida, com o âncora como candidato a parâmetro no dia em que a diferença
aparecer num backtest real. O aquecimento não é problema hoje: o live aquece sobre **todo** o
histórico (39 mil barras na última medição), o que dá mais de 2 mil barras de H4.

## Consequências

- `packages/engine/src/tradeforge_engine/higher_timeframe.py`: `BarAggregator` e
  `HigherTimeframeGate`. O portão guarda o próprio registro de toque na resolução da barra base,
  porque a mitigação do detector de H4 só é carimbada quando a barra de H4 fecha — até 16 barras
  de M15 depois do pavio que tocou.
- `StructureStrategy` ganha `htf` e `timeframe`; o portão é alimentado no topo do `on_bar`, antes
  do ramo de posição aberta, e recusa no `_may_arm`, o ponto único onde toda regra sobre "esta
  região pode ser negociada" já passa. A liberação é **gasta ao armar**, não ao preencher, porque
  a resposta dele foi que ordem cancelada sem preencher gasta a chance.
- `TIMEFRAME_DELTAS` mudou de `strategy.py` para `domain.py`, porque a fábrica de setups precisa
  dela e `strategy` importa a fábrica. `build_setup` recebe o timeframe do documento.
- DSL: `StructureParams.htf: Timeframe | None = None`; a semântica recusa um `htf` que não seja
  mais grosso que o `timeframe` do documento. Primeiro enum anulável da DSL: a web aprendeu a
  desenhar "off" em vez de "choose…", e o formulário grava `htf: null` como já gravava `max_bos`.
- O que fica em aberto (backlog): o âncora do relógio; desenhar as regiões de H4 no gráfico; a
  grade de estudo não sabe variar `htf` incluindo "off".
