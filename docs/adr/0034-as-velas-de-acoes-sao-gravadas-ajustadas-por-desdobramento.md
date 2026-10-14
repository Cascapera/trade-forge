# ADR-0034 — As velas de ações são gravadas ajustadas por desdobramento

- **Status**: aceito (14/10, por ele: "vamos ajustar os desdobramentos" / "pode seguir")
- **Data**: 2026-10-14
- **Contexto do PR**: achado no treino do ML das bases novas (14/10) — trades de −500 R e +300 R nas
  ações americanas, todos em datas de desdobramento
- **Relaciona-se com**: ADR-05 (Parquet particionado), ADR-0032 (várias corretoras), ADR-0031 (ML)

## Contexto

A Tradeview entrega o histórico das ações americanas **sem ajuste**. No desdobramento de 20 para 1
da AMZN (06/06/2022) o preço "cai" de 2.446 para 125 entre duas velas. Para todo o sistema isso é
uma queda de 95%:

- o backtest que segurava uma compra levou um stop num gap de −500 R;
- o rótulo do ML (R da entrada) e as variáveis (ATR, médias, retornos) ficam distorcidos por ~200
  barras em volta;
- a varredura e a tela de "melhores" das ações americanas misturam esses trades com os de verdade.

Medido nos candles diários (14/10): **10 desdobramentos em 8 ações** (AAPL 4:1 e TSLA 5:1 em 2020,
NVDA 4:1 em 2021, AMZN 20:1, GOOGL 20:1, SHOP 10:1 e TSLA 3:1 em 2022, NVDA 10:1 em 2024, NFLX 10:1
em 2025, KLAC 10:1 em 2026). As ações brasileiras da XP **já vêm ajustadas**, e ajustadas também por
proventos: a PETR4 de out/2021 está a R$ 8,53 nos nossos dados, quando foi negociada a ~R$ 28.

## Decisão

O coletor grava as velas de ações **já ajustadas** pelos desdobramentos registrados na tabela
`stock_splits`. Cada arquivo Parquet guarda nos seus metadados quais desdobramentos já recebeu, e
um ajuste só aplica os que faltam.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| Ajustar na leitura, manter o bruto no disco | o dado bruto fica intacto; registrar um desdobramento não reescreve nada | pelo menos 4 leitores (motor nos workers, features do ML, aquecimento dos sinais, gráficos da API), alguns em outras máquinas, precisariam conhecer a lista; basta um esquecer para o erro voltar escondido |
| **Ajustar na gravação (escolhida)** | um ponto só; os leitores, o cache dos workers e o sync das máquinas remotas (que seguem tamanho e data do arquivo) não mudam nada | o bruto sai do disco (continua no MT5); exige cuidado para não dividir duas vezes |
| Lista de uma fonte externa (Yahoo etc.) | não depende de achar o salto | dependência de rede nova; a data pode não bater com o CFD da corretora |
| **Detectar o salto nos próprios candles e confirmar à mão (escolhida)** | sem dependência nova; a confirmação humana fica registrada | bonificação pequena (10%) não se distingue de um dia ruim: tem de ser registrada à mão |

## Trade-off aceito

- **O disco deixa de guardar o que a corretora mandou.** O original continua recuperável recoletando
  do MT5, e o ajuste é refeito sozinho porque a coleta aplica os desdobramentos registrados.
- **A idempotência passa a depender de um carimbo no arquivo** (`tradeforge.splits` nos metadados
  do Parquet). Sem carimbo, o arquivo é bruto. Um arquivo copiado à mão de outro lugar carrega o
  carimbo junto, o que é o comportamento certo.
- **O spread arredonda.** Ele é um inteiro em pontos: 3 pontos ÷ 20 = 0,15, que vira 0. O erro é de
  no máximo meio ponto, num preço que ficou 20 vezes menor.
- **A semana do desdobramento é reconstruída a partir dos diários**, e não escalada. O candle
  semanal abre no domingo, antes do desdobramento de segunda, mas só tem preços de depois dele. Um
  desdobramento numa quarta mistura os dois lados. Sem D1 do período, o ajuste recusa.

## Consequências

- `tradeforge-collector splits detect | add | apply`. O `add` **recusa** se o diário não mostrar o
  salto naquela data, que é exatamente o que acontece com dados já ajustados pela corretora (XP).
  Isso protege contra dividir duas vezes.
- A coleta (`backfill`/agente) aplica os desdobramentos registrados a todo dado novo e recusa um
  download que atravesse o desdobramento sem mostrar o salto (a corretora passou a ajustar).
- Runs, varreduras e bases de ML de ações americanas feitos **antes** deste ADR continuam com os
  números antigos: refazer é decisão dele.
- Fora daqui (backlog): ajuste por proventos nas americanas; a série da XP, ajustada por proventos,
  muda a cada provento novo, então recoletar só o ano corrente pode criar um degrau na virada do
  ano; e os saltos de rolagem dos contratos contínuos da B3 (WIN$, WDO$).
