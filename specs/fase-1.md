# Fase 1 — MVP Backtest (4-6 semanas)

**Objetivo:** backtest completo via UI: criar estratégia → rodar → ver métricas, curva de capital e trades no gráfico. Publicável como vitrine.
**Referência:** sdd.md §3.3, §4, §5, §6, §8 (Fase 1), §10.

## PR-101 — Modelo de dados + migrations
**Escopo:** SQLAlchemy + Alembic; tabelas `instruments`, `datasets`, `strategies` (versão imutável), `backtests`, `backtest_metrics`, `trades` (sdd.md §5); seeds de instrumentos de exemplo.
**Aceite:** `alembic upgrade head` cria tudo; testes de constraint (versão imutável de estratégia, FKs).
**Você vai aprender:** migrations como código, JSONB vs colunas, imutabilidade por versionamento (por que UPDATE em estratégia é proibido).

## PR-102 — Data Collector: backfill MT5 → Parquet
**Escopo:** `apps/collector` conecta ao MT5, baixa OHLCV por símbolo/timeframe, grava Parquet particionado (`symbol/timeframe/year`), registra em `datasets` e `instruments` (via `symbol_info`); detecção de gaps; CLI (`collector backfill EURUSD H1 2020-01-01 2025-12-31`); modo mock para dev sem MT5 (gera dados sintéticos determinísticos).
**Aceite:** backfill real numa máquina Windows com MT5; testes usam o mock; relatório de gaps por dataset.
**Você vai aprender:** por que Parquet para séries (colunar, compressão, partition pruning), idempotência de backfill, isolamento da dependência Windows.

## PR-103 — Engine: núcleo event-driven
**Escopo:** `packages/engine`: tipos de domínio (Candle, OrderRequest, Fill, Position, AccountState — dataclasses frozen), interfaces Protocol (Broker, Indicator, Condition, CostModel, RiskManager), event loop (sdd.md §3.3.2), Portfolio. SEM indicadores/condições ainda — loop testado com stubs.
**Aceite:** loop processa candles sintéticos com estratégia stub; teste de determinismo (2 execuções ⇒ resultado idêntico byte a byte).
**Você vai aprender:** arquitetura event-driven, Protocol vs herança, por que dataclasses imutáveis no domínio, design para testabilidade.
**engine-guardian obrigatório daqui em diante em todo PR de engine.**

## PR-104 — Indicadores e condições v1
**Escopo:** SMA, EMA (estado incremental O(1) por candle); registry de indicadores/operadores; avaliador da árvore de condições da DSL; refs (`price.*`, `candle[-n].*`, indicadores). Compilador: JSON da DSL → objetos da engine.
**Aceite:** testes de ouro por indicador (valores conferidos contra cálculo manual/planilha); testes da árvore (all/any/not aninhados); property-based: EMA/SMA nunca usa candle não fechado.
**Você vai aprender:** cálculo incremental vs janela completa, padrão registry (novos blocos sem tocar o core — é assim que as técnicas do livro entrarão), interpretação de AST.

## PR-105 — BacktestBroker + CostModel + RiskManager
**Escopo:** `BacktestBroker` com fill no open do candle seguinte (anti-lookahead) e slippage configurável; `SpreadCostModel` (forex) e `CommissionCostModel` (ações); sizing `percent_risk`; stops/alvos (candle_extreme, risk_multiple) avaliados intra-candle com regra explícita e testada para SL+TP no mesmo candle (assumir pior caso: SL primeiro).
**Aceite:** teste de ouro completo — estratégia MA-cross em dataset sintético de ~50 candles com TODOS os trades e P&L calculados à mão em planilha comitada junto; property-based: soma dos trades == variação do equity.
**Você vai aprender:** os vieses clássicos de backtest e como cada linha do broker evita um deles; modelagem de custos por classe de ativo; por que "pior caso" em ambiguidade intra-candle.

## PR-106 — Métricas + persistência do resultado
**Escopo:** cálculo de todas as métricas do sdd.md §5 (`backtest_metrics`) + curva de capital; persistência trade a trade com `context` (snapshot dos indicadores na entrada); `r_multiple`.
**Aceite:** métricas do teste de ouro conferem com a planilha; expectancy e profit factor validados à mão.
**Você vai aprender:** o significado e as armadilhas de cada métrica (por que win rate sozinho engana; drawdown de pico a vale; Sharpe em trades irregulares).

## PR-107 — API + worker assíncrono
**Escopo:** FastAPI: CRUD de estratégias (validando via `packages/schema`), POST /backtests enfileira, worker (arq) executa a engine, GET status/métricas/trades/equity; WebSocket de progresso.
**Aceite:** fluxo completo via curl/httpie: criar estratégia → enfileirar → acompanhar → ler resultados; teste de API com schemathesis.
**Você vai aprender:** por que trabalho pesado nunca roda no processo da API, filas com Redis, contrato REST + WS.

## PR-108 — UI: builder + resultados
**Escopo:** React: form de estratégia dirigido pelo schema (gera o JSON da DSL), tela de lançamento de backtest (ativo/timeframe/período/capital), tela de resultados: cards de métricas, curva de capital, tabela de trades, gráfico de candles com entradas/saídas (lightweight-charts).
**Aceite:** fluxo E2E na UI com dataset local; Playwright cobrindo o caminho feliz.
**Você vai aprender:** UI dirigida por schema, React Query para dados assíncronos com polling/WS, plotagem de trades em gráfico financeiro.

## PR-109 — README vitrine + demo dataset
**Escopo:** README em inglês (GIF, diagrama, Design Decisions com os ADRs, quickstart com dados sintéticos embutidos — sem MT5), LICENSE, CONTRIBUTING básico.
**Aceite:** um dev clona e roda um backtest em <10 min sem MT5.
**Você vai aprender:** o que faz um README de vitrine funcionar (mostrar > contar).

## Entregável da fase
Backtest funcional de ponta a ponta via UI. **Publicar o repo.**
