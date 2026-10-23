# ADR-0030 — Um run aquece antes da janela, e um trade só conta na janela em que entrou e saiu

- **Status**: aceito
- **Data**: 2026-09-28
- **Contexto do PR**: PR-350 (esta ADR), PRs seguintes (motor, API)

## Contexto

Um backtest de `[date_from, date_to]` lê **só** as barras dessa janela (`runner._candles_to_run`).
Duas coisas decorrem disso, e nenhuma é sobre o mercado:

1. **O começo depende da data escolhida.** Os indicadores leem `None` até terem visto barras
   suficientes e a estrutura começa sem viés — o mesmo problema que o ADR-0023 resolveu para uma
   sessão ao vivo, sem solução no backtest. Um setup de média não opera enquanto a média esquenta;
   um de estrutura opera desde a primeira CHoCH, sobre uma estrutura sem passado.
2. **O fim também.** Uma posição aberta quando as barras acabam **não vira trade** (o motor não
   fecha nada à força; ela só aparece na curva, marcada a mercado). E o R por ano (`yearly_r`) é
   contado pelo **ano de entrada**. Um run mais longo conta, no último ano do curto, o trade que o
   curto deixou de fora.

Medido em 28/09 (motor 0.2.0), comparando o R de cada ano entre runs que começam em 2020 e em 2021,
ponto a ponto, nos mesmos documentos:

| Setup | Gráfico | Média longa | 1º ano igual | Ano do meio | Último ano |
|---|---|---|---|---|---|
| CONTINUATION (estrutura), AUDUSD M5, 4 320 pontos | M5 | — | 100 % | 100 % | 100 % |
| MM9 BASE COMPLETO, AUDUSD, 3 240 pontos por gráfico | D1 | off / 200 | 93 % / **39 %** | 100 % / 83 % | 90 % / 84 % |
| | H4 | off / 200 | 91 % / **33 %** | 100 % / 100 % | 89 % / 100 % |
| | H1 | off / 200 | 83 % / **44 %** | 94 % / 97 % | 88 % / 95 % |

O CONTINUATION deu 100 % porque opera 5–8 vezes por ano e não tem média. No de média, quanto mais
longa a média, mais o primeiro ano diverge — no D1 com 100 ou 200, até o segundo. E ~10 % do último
ano diverge **qualquer que seja a média**: é o trade aberto no corte.

A mesma divergência impede o reaproveitamento por recorte (PR-345): um run de 2020–25 não responde
a 2022–24, porque um run novo de 2022–24 daria outro resultado. Mas o defeito é do backtest, não do
reaproveitamento: **o mesmo trade de 2021 sai diferente conforme a data de início escolhida.**

## Decisão

**Regra 1 — aquecimento.** Um run lê `W` barras antes de `date_from` e roda a estratégia sobre elas
**normalmente, operando** — como o aquecimento ao vivo (ADR-0023), que mediu "ordens fantasmas" em
4 de 5 pontos quando proibia ordens. O que entra antes de `date_from` é **sombra**: o broker o
simula até fechar (ele ocupa a vaga, como num run mais longo), mas não mexe no saldo, não entra na
lista de trades nem na curva. A conta abre em `date_from` com o capital inicial exato; o
dimensionamento continua pelo saldo real.

- `W` = **4 × o maior período** que o documento lê (média longa de 200 → 800 barras): uma média
  exponencial precisa de ~4 períodos para esquecer o valor inicial. Setup sem período (estrutura):
  **1 ano** de calendário.
- Sem histórico bastante antes da janela, o run aquece com o que houver e **grava quantas barras
  aqueceu** — nunca em silêncio.

**Regra 2 — o trade pertence à janela em que entrou e saiu.** Continua fora o que não fecha dentro
da janela (como hoje). O R por ano passa a ser guardado também por **ano de entrada × ano de saída**:
recortar `[a, b]` de um run mais longo soma as células com entrada ≥ `a` e saída ≤ `b` — o que um
run novo de `[a, b]` daria.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| Aquecer sem operar (vetar ordens) | Nada a descartar | "Ordens fantasmas" medidas no ADR-0023; o setup acredita numa ordem que não existe |
| Descartar o aquecimento e recalcular o dinheiro por replay (como o cluster) | Motor quase intocado | O replay redimensiona e arredonda lotes de outro jeito: mudaria o dinheiro até de runs sem aquecimento |
| `W` fixo (1 ano) para tudo | Simples | Média de 200 no D1 precisa de mais; M5 de estrutura desperdiçaria |
| Manter o saldo que veio do aquecimento | Nenhuma regra de sombra | O dinheiro da janela dependeria de trades que não contam |
| Não mudar nada e só recortar setups medidos 100 % | Sem mexer no motor | O defeito do começo e do fim continua em todo backtest |

## Trade-off aceito

- Cada run lê mais barras: 4 × período antes da janela (no D1 com média de 200, ~3 anos). Tempo a
  mais estimado em 10–30 % no M5, mais nos gráficos longos com média longa.
- Resultados mudam: o motor vai a **0.3.0** e nenhum run anterior é original para reaproveitamento.
- Uma janela que começa no início dos dados (2009) aquece menos que `W` — registrado no run.

## Consequências

- Motor: o broker de backtest ganha "sombra" antes de `book_from`; o laço não registra ponto de
  curva antes dele; o lote (ADR-0029) recebe o mesmo `book_from`.
- API: o runner lê as `W` barras antes (a série inteira já está em memória no worker); o run grava
  as barras de aquecimento; o R por entrada × saída vai para `backtest_metrics`.
- Reaproveitamento (PR-345): a chave passa a incluir as barras de aquecimento lidas; o recorte por
  ano fica possível, primeiro em R.
- Golden tests do começo e do fim da janela mudam; o engine-guardian revisa cada PR do motor.
