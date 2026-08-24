# ADR-0022 — O `CostModel` recebe a barra em que o fill nasce

- **Status**: aceito
- **Data**: 2026-08-24
- **Contexto do PR**: PR-302-A

## Contexto

O `sdd.md §3.3.2` define o `CostModel` como
`entry_cost(order, instrument, price) -> Money`. Essa assinatura foi desenhada quando só existia
backtest, e num backtest o spread é uma **escolha**: a barra histórica carrega o spread do
instante em que foi coletada, não do instante em que negociou, então um ano de dados não tem
resposta honesta única e o número é configurado uma vez (`SpreadCostModel(spread_points=17)`).

O paper trading (fase 3) muda a natureza do número. A barra chega do stream do PR-301 segundos
depois de fechar, e ela **carrega o spread do momento** — `Candle.spread`, em pontos, o mesmo
campo que o `publisher` já serializa. Cobrar um spread fixo num paper seria cobrar um número
inventado tendo o medido em mãos, e a medição do dia 21/08 mostrou o tamanho do erro: o mesmo
backtest com spread 76 (mercado fechado) contra 17 (mercado aberto) foi a diferença entre
-99,4% e um resultado que media estratégia.

O problema é que quem sabe o spread do momento (o broker, dentro do `on_bar`) não tinha como
contar para o modelo de custo. O invariante do `AGENTS.md §5.6` — *nenhuma lógica de custo
hard-coded na engine, sempre via `CostModel`* — proíbe a saída óbvia de ler o spread no ponto do
fill.

## Decisão

`entry_cost` e `exit_cost` passam a receber a **barra** em que o fill está nascendo:
`entry_cost(order, instrument, price, candle) -> Money`.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Barra como parâmetro** (escolhida) | Não existe onde guardar um valor velho, então não existe spread stale; o único lugar onde um `Fill` nasce é `Broker.on_bar`, que já tem a barra na mão | Toca a assinatura das 3 implementações existentes e do `Protocol`; 4 pontos de chamada no `BacktestBroker` |
| **Campo mutável no modelo**, atualizado antes de cada barra | Diff mínimo; nenhuma assinatura muda | Nada no sistema de tipos diz quem atualiza nem quando; um modelo segurando o spread da barra **anterior** cobra um número plausível, em silêncio, para sempre. É a forma exata do erro que o `decidir-vs-traduzir` já custou uma vez |
| **`spread_points` como parâmetro** em vez do `Candle` | Assinatura mais estreita | Coloca "custo significa spread" de volta no seam que existe justamente para não saber isso; um modelo de comissão ignora, e um que um dia cobre por amplitude precisa da barra, não de um número que alguém escolheu por ele |
| **`PaperBroker` como classe nova**, com sua própria lógica de fill | Isola live de backtest; segue o spec ao pé da letra | Duplica 867 linhas de semântica de fill. Concordam hoje e divergem no primeiro conserto aplicado a um só — e a divergência chega como trades plausíveis, não como erro |

## Trade-off aceito

Alargamos um `Protocol` do núcleo — o tipo de mudança que este projeto trata como cara — para
não criar um segundo motor de fill. A conta: uma assinatura com um parâmetro a mais, em 3
implementações e 4 pontos de chamada, contra 867 linhas de aritmética de execução mantidas em
dois lugares. O parâmetro é verificado pelo `mypy --strict`; a duplicação não seria verificada
por nada.

Os dois modelos que ignoram a barra (`CommissionCostModel`, `NoCostModel`) carregam `# noqa:
ARG002` por argumento, o que é ruído honesto: eles **de fato** não leem a barra, e o dia em que
um deles passar a ler, o `noqa` sai junto.

## Um lookahead que aceitamos, de propósito

`MqlRates.spread` reflete o spread **do momento em que a barra se formou** — informação do fim
da barra. O fill de entrada acontece no **open** dela. Então o `BarSpreadCostModel` cobra, num
fill do início da barra, um número que só existiu depois.

Isso é lookahead de **custo**, não de decisão, e a distinção é toda a diferença:

- não pode selecionar trades (a estratégia decidiu na barra anterior e não consultou custo);
- não pode inventar lucro (é sempre uma subtração da conta);
- só torna o custo mais ou menos exato do que seria com o spread do instante do fill.

A alternativa — cobrar o spread da barra **anterior** — trocaria um erro pequeno e sem viés por
um erro sistemático em toda barra de spike, e ainda seria uma escolha, não uma medição.
Aceitamos o de cima e registramos aqui para que ninguém o descubra sozinho e ache que é bug.

⚠️ **Isto não vale para o `require_spread`**: uma barra sem spread não é um custo aproximado,
é a ausência de qualquer afirmação sobre custo, e por isso é recusada em vez de aproximada.

## Consequências

- **`BarSpreadCostModel`** é a implementação nova e é *toda* a diferença entre backtest e paper.
  Loop, broker, ledger e aritmética de P&L são os mesmos objetos (ADR-01).
- **Barra sem spread é recusada, não cobrada como zero.** `Candle.spread` tem default `0`, e um
  zero é o que um candle sintético, um fixture escrito à mão e um campo perdido no fio parecem.
  Cobrar nada por eles seria uma sessão de paper reportando um edge sem custo que a conta real
  não vai ter — a falha que o modelo existe para impedir, chegando pelo próprio modelo. O escape
  (`require_spread=False`) precisa ser pedido pelo nome.
- **`testing.bar()` ganhou `spread`**, com default `0`, para que um cenário que pretende cobrar
  tenha de dizer isso.
- Nada no `sdd.md §3.3.2` fica errado além da assinatura; o documento deve ser atualizado no
  fechamento da fase.
