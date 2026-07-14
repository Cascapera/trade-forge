# ADR-0011 — Preços no Parquet em `decimal128`, não `float64`

- **Status**: aceito
- **Data**: 2026-07-14
- **Contexto do PR**: PR-102

## Contexto

O `sdd.md §12` diz "Parquet (pyarrow)" e nada sobre o tipo dos preços. Praticamente todo dataset OHLCV do mercado usa `float64` — é o default de fato.

Mas o PR-101 fixou uma regra no banco: dinheiro é `NUMERIC`, nunca float, porque erro de ponto flutuante binário acumula ao somar milhares de fills até a curva de capital divergir do extrato. Se o Parquet guardar `float64` e o banco guardar `NUMERIC`, a conversão acontece **na fronteira da engine** — e o preço já chega lá com o ruído binário embutido. `Decimal(1.10525)` não é `Decimal("1.10525")`: é `1.10524999999999997...`, e nenhum arredondamento posterior desfaz o que foi perdido na origem.

## Decisão

As colunas `open`, `high`, `low`, `close` no Parquet são `decimal128(20, 10)` — a mesma precisão e escala do tipo `PRICE` do `packages/db`. Uma definição de "preço" no arquivo, no banco e na engine.

A regra que isso estabelece, e que vale para a Fase 1 inteira:

> **Aproxime onde você está estimando; seja exato onde você está contabilizando.**

Uma SMA é uma estimativa — a engine pode calculá-la em `float`, e vai. Um preço de execução é um fato contábil: é multiplicado por um volume e somado a uma curva de capital milhares de vezes. Guardar a fonte exata significa que **a engine escolhe quando aproximar**, em vez de herdar uma aproximação que ela não pediu e não pode desfazer.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| **`decimal128(20,10)`** (escolhida) | Round-trip exato; um único tipo de preço no sistema; a engine decide onde aproximar | Arquivo um pouco maior; algumas ferramentas do ecossistema lidam pior com decimal que com float |
| `float64` | Padrão da indústria; mais rápido para matemática vetorizada | O ruído binário entra na origem e nunca mais sai. E contradiz a regra que o PR-101 acabou de fixar no banco |
| `int64` escalado pelo tick (preço em ticks) | Exato e compacto; é o que exchanges fazem internamente | Ilegível fora do sistema; todo consumidor precisa do `tick_size` para entender o número |

## Trade-off aceito

Arquivos maiores e um caminho numérico um pouco mais lento na leitura, em troca de nunca ter que investigar por que o P&L do backtest difere do extrato na quarta casa decimal.

## Consequências

- `OHLCV_SCHEMA` (`apps/collector/storage.py`) é a definição canônica do arquivo. `Candle.open/high/low/close` são `Decimal`.
- A conversão float → Decimal acontece **uma vez**, na fronteira do MT5 (`normalise()`), quantizando ao `digits` do instrumento — que é o que transforma o float de volta no número que o mercado realmente imprimiu.
- Quando a engine (PR-103+) quiser velocidade para indicadores, ela converte para float **explicitamente**, num ponto visível do código. O que não pode acontecer é a aproximação entrar sem ninguém decidir.
