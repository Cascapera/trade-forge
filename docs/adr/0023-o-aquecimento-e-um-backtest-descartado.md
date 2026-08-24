# ADR-0023 — O aquecimento de uma sessão é um backtest cujo ledger é descartado

- **Status**: aceito
- **Data**: 2026-08-24
- **Contexto do PR**: PR-302-C-A
- **Substitui**: a decisão de aquecimento registrada em `specs/fase-3.md` em 24/08/2026

## Contexto

Uma sessão de paper começa fria: os indicadores leem `None` até terem visto barras suficientes, e
a estrutura não tem viés até o mercado dar um. Uma sessão que abre no próximo tick deixa de pegar
trades por um tempo e **parece uma sessão que não achou setup** — dois estados com remédios
opostos e nada na tela que os separe.

A decisão registrada no `specs/fase-3.md` foi: *"lê N barras históricas do Parquet, alimenta a
estratégia com elas **sem permitir ordem**, e só então liga o stream"*. Este ADR existe porque a
segunda metade dessa frase está errada, e só foi possível saber medindo.

## O que a medição mostrou

**Não existe `N`.** O aquecimento tem duas naturezas, e só uma é calculável:

| indicador | barras até o 1º valor |
|---|---|
| SMA · EMA · ATR · Bollinger | `período` |
| RSI · Highest · Lowest | `período + 1` |
| **ADX** | **`2 × período`** — 28 com período 14 |

⚠️ `max(período)` subestima o ADX pela metade. Mas o termo dominante é a estrutura, e ela **não
tem fórmula** — primeira CHoCH, medida nos dados reais do projeto:

```
EURUSD H1  62      BTCUSD H1  730      EURGBP M15 151
AUDCAD H1  38      XAUUSD H1   40      AUDCAD M5  310      XAUUSD H4 190
```

De 38 a 730 barras. Qualquer número fixo é fome para um ativo ou desperdício para outro, e quando
é fome o sintoma é exatamente o que o aquecimento existe para eliminar.

**E "sem permitir ordem" quebra a estratégia.** Um setup marca sua ordem armada como colocada no
instante em que **emite** o sinal (`setups.py`), e o laço nunca devolve `OrderResult`. Um veto no
`RiskManager` mata a ordem depois disso — então a estratégia atravessa a virada acreditando que há
uma limite repousando num venue que nunca ouviu falar dela, e aquela região **nunca é operada**.

Sondado com o setup `structure_choch` real sobre EURUSD H1 real, em cinco pontos de virada:

```
virada    100   200   300   400   500
fantasma  SIM   SIM   nao   SIM   SIM      -> 4 em 5
```

Sem o veto, nas mesmas cinco barras, a estratégia e o broker concordam sempre.

Frequência do que atravessa, medida no mesmo dado (EURUSD H1, após 200 barras):

| na virada | frequência |
|---|---|
| com limite em repouso | **35% a 73%** |
| em posição aberta | **0,4% a 3,0%** |

## Decisão

**O aquecimento roda como um backtest de verdade** — pelo laço real, contra um broker real, com
ordens preenchendo e zonas queimando — e a sessão abre com um **broker novo**. O dinheiro é
intocado *por construção*. As ordens ainda em repouso são re-submetidas, **re-dimensionadas**
contra a conta da sessão.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **Backtest com ledger descartado** (escolhida) | A estratégia atravessa coerente com o broker; o dinheiro é intocado por construção, sem `reset_ledger` cujos campos alguém esqueça | A sessão não pode abrir em posição aberta; a escrituração da estratégia refere-se a trades cujo ledger não existe mais |
| **Vetar tudo decidido no histórico** (a do spec) | Conta limpa; usa o seam de veto que já existe | Produz o fantasma em 4 de 5 viradas medidas — reproduz a doença que cura |
| **Vetar e recolocar na virada** | Conta limpa e sem fantasma | Cria a pergunta "esta ordem ainda vale?", que ninguém responde sem reimplementar o julgamento do setup |
| **Vetar só ordem a mercado** | Menor mudança | Uma limite armada no histórico ainda pode preencher **dentro** do histórico, inventando o trade do mesmo jeito |

## Trade-off aceito

**Uma sessão não pode abrir no meio de um trade.** Herdar a posição faria o broker novo marcar a
mercado uma entrada cujo preço ele nunca pagou, e a identidade `sum(net_pnl) == equity final −
capital inicial` deixaria de valer para a sessão. Isso não é raro nem comum: é **impossível de
conciliar**. Medido em 0,4-3% das barras — mas o argumento não é a raridade, e é importante que
não seja: estatística convida à pergunta "e se eu tratar o caso raro?", identidade fecha a porta.

Aceitamos também um resíduo de auditoria: a estratégia atravessa com `_traded`/`_spent` de zonas
cujos trades só existem no ledger descartado, então *"por que ela pulou esta região?"* tem resposta
fora da sessão. `HandOver.warm_trades` registra quantos foram — reportar, não carregar.

## Consequências

- **Todo campo de uma ordem carregada é fato de mercado, menos um.** `volume` foi calculado da
  equity do ledger descartado, então é **re-dimensionado** contra a conta da sessão. Medido: um
  aquecimento que termina em 9 901 deixa a ordem com 1,08 lote onde a conta de 10 000 pede 1,09;
  um que corre a 13 000 deixa 1,42 contra 1,09 — a primeira operação da sessão arriscando 1,3%
  onde a estratégia pediu 1%.
- `client_id`, `decided_at` e `snapshot` **não** mudam. O primeiro é o que faz o `_observe_fill`
  reconhecer o preenchimento; o segundo mantém o `loop._reject_lookahead` armado; o terceiro é o
  único que aquela ordem terá, porque o broker novo não tem janela para reconstruir um.
- `hand_over` consulta **`allow` além de `size`**. O `protocols.py` mantém o veto fora do sizing
  para que um bug de sizing não vire um bug de segurança; perguntar só `size` faz a mesma inversão
  ao contrário, e o dia em que o kill switch entrar (AGENTS.md §5.7) quem o escrever vai procurar
  as chamadas de `allow`.
- **`specs/fase-3.md` fica corrigido** no mesmo PR: a frase "sem permitir ordem" era o desenho que
  esta medição derrubou.
- O `N` continua sem existir. O que a sessão registra é quantas barras **usou**, não quantas
  precisaria — porque a segunda não é conhecível.
