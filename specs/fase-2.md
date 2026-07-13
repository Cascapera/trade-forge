# Fase 2 — Profundidade (4-6 semanas)

**Objetivo:** de "roda um backtest" para "ferramenta séria de pesquisa": mais blocos, otimização, walk-forward, comparação.
**Referência:** sdd.md §8 (Fase 2).

## PR-201 — Indicadores v2
**Escopo:** RSI, ATR, Bandas de Bollinger, ADX, máximas/mínimas de N períodos — todos incrementais, via registry; novos operadores conforme necessidade (`between`, `rising`, `falling`).
**Aceite:** teste de ouro por indicador contra valores de referência conhecidos.
**Você vai aprender:** indicadores com estado composto (ATR depende de TR; ADX de +DI/-DI), suavizações de Wilder vs EMA.

## PR-202 — Setups nomeados (técnicas do livro)
**Escopo:** mecanismo de "setup composto": um bloco nomeado (ex.: `setup_9_1`) que expande para árvore de condições; primeiros 2-3 setups do livro do Guilherme implementados COM ele (sessão de pair: ele descreve, o agente modela na DSL).
**Aceite:** cada setup com teste de ouro em cenário construído à mão que dispara e cenário que NÃO dispara.
**Você vai aprender:** como formalizar conhecimento tácito de trading em regras executáveis — e onde a formalização vaza (discricionariedade).

## PR-203 — Grid search de parâmetros
**Escopo:** definição de ranges por parâmetro na UI; expansão do grid; execução paralela nos workers; tabela/heatmap de resultados; persistência ligando execuções ao "estudo".
**Aceite:** grid de 100+ combinações executa paralelo e ordena por métrica escolhida.
**Você vai aprender:** paralelismo de backtests (processos vs threads vs fila), e o perigo central: quanto mais você otimiza, mais você superajusta.

## PR-204 — Walk-forward analysis
**Escopo:** janelas deslizantes treino/validação; agregação dos períodos out-of-sample; relatório de robustez (parâmetro estável entre janelas?); UI.
**Aceite:** walk-forward completo sobre um estudo de grid; teste com dataset sintético onde estratégia overfit falha no out-of-sample.
**Você vai aprender:** overfitting em trading, in-sample vs out-of-sample, por que este é O argumento de credibilidade da plataforma (e do post técnico #1).

## PR-205 — Comparador de execuções + multi-símbolo
**Escopo:** comparar N backtests lado a lado (métricas + curvas sobrepostas); rodar a mesma estratégia numa cesta de símbolos com relatório agregado.
**Aceite:** comparação de 3+ execuções na UI; backtest de cesta com agregado correto.
**Você vai aprender:** avaliação de robustez entre mercados (estratégia que só funciona num ativo = red flag).

## PR-206 — Builder visual de blocos
**Escopo:** evolução do form para builder visual (blocos arrastáveis/aninháveis de condição) — continua gerando a mesma DSL.
**Aceite:** estratégia da fase 1 é recriável no builder visual; round-trip JSON → UI → JSON sem perda.
**Você vai aprender:** UI como projeção de uma AST; round-trip como propriedade testável.

## Entregável da fase
Otimização + walk-forward + comparação na UI. **Post técnico #1: "Construindo uma engine de backtest event-driven em Python".**
