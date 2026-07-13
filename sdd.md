# SDD — Plataforma de Backtest e Execução de Estratégias
**Software Design Document · v1.0 · 13/07/2026**
Autor: Guilherme · Nome de trabalho do projeto: **TradeForge** (sugestão — fácil de trocar)

---

## 1. Visão

Plataforma web para **criar, testar e executar estratégias de trading** de forma visual, sem código, em qualquer ativo disponível no MetaTrader 5 (forex, ações e índices americanos no lançamento; agnóstica a ativos por design).

O diferencial central de arquitetura: **a estratégia é escrita uma única vez** e roda tanto no backtest quanto no mercado ao vivo, contra a mesma engine event-driven. Isso elimina a divergência clássica entre "resultado do backtest" e "resultado real".

### 1.1 Objetivos do projeto

| # | Objetivo | Métrica de sucesso |
|---|----------|--------------------|
| 1 | Uso próprio: validar técnicas de entrada (incl. as do livro do autor) | Backtests reprodutíveis com métricas confiáveis |
| 2 | Vitrine profissional: posicionamento como Senior Dev / AI Engineer | Repo público, demo online, 2-3 posts técnicos |
| 3 | Produto vendável (fase futura) | SaaS multi-usuário com billing |

### 1.2 Não-objetivos (por enquanto)

- HFT / execução em tick-by-tick de baixa latência.
- Previsão de preço por ML (baixa credibilidade; a IA entra em análise e geração de estratégia, não em previsão).
- Mobile app.
- Suporte a corretoras via API própria (fase futura; MT5 é o gateway inicial de execução).

---

## 2. Personas e casos de uso

**P1 — Trader técnico (o próprio autor, inicialmente).** Monta estratégia na UI (indicador + gatilho de entrada + saída + risco), roda backtest em EURUSD H1 de 2020–2025, analisa métricas e trades no gráfico, ajusta parâmetros, salva a versão vencedora, ativa em paper trading e depois em conta real via MT5.

**P2 — Trader iniciante (cliente futuro).** Usa estratégias-modelo prontas, entende os resultados pelo relatório em linguagem natural gerado por IA.

**P3 — Recrutador/tech lead (audiência da vitrine).** Lê o README, navega na demo pública, avalia arquitetura, testes e posts técnicos.

### Casos de uso principais

1. **UC-01 Criar estratégia** — compor blocos (indicadores, condições de entrada/saída, gestão de risco) via UI; salvar como JSON versionado.
2. **UC-02 Rodar backtest** — selecionar estratégia + ativo(s) + timeframe + período + modelo de custos; executar; persistir resultado completo (trade a trade).
3. **UC-03 Analisar resultados** — métricas, curva de capital, trades plotados no gráfico, comparação entre execuções.
4. **UC-04 Otimizar parâmetros** — grid search sobre ranges de parâmetros; walk-forward analysis (fase 2).
5. **UC-05 Paper trading** — estratégia rodando com preços ao vivo e broker simulado.
6. **UC-06 Execução real** — mesma estratégia, ordens enviadas ao MT5; kill switch e limites de risco ativos.
7. **UC-07 Análise por IA** — relatório em linguagem natural sobre um backtest; geração de estratégia a partir de descrição textual (fase 3).

---

## 3. Arquitetura

### 3.1 Visão geral

```
┌────────────────────────────── Windows (VPS ou máquina local) ──────────────────────────────┐
│                                                                                             │
│  ┌─────────────┐     ┌──────────────────────┐        ┌───────────────────────────┐         │
│  │ MetaTrader5 │◄────┤ Data Collector (py)  │        │ Execution Service (py)    │         │
│  │  terminal   │     │ - histórico OHLCV    │        │ - recebe ordens (fila)    │         │
│  │             │◄────┤ - symbol_info specs  │        │ - executa no MT5          │         │
│  └─────────────┘     │ - candles ao vivo    │        │ - reporta fills/posições  │         │
│                      └─────────┬────────────┘        │ - kill switch/risk limits │         │
│                                │                     └──────────▲────────────────┘         │
└────────────────────────────────┼────────────────────────────────┼──────────────────────────┘
                                 │ escreve                        │ consome/publica
                                 ▼                                │
        ┌──────────────────────────────────────┐    ┌─────────────┴─────────────┐
        │ Storage                              │    │ Message Broker (Redis)    │
        │ - PostgreSQL (metadados, trades,     │    │ - orders / fills / candles │
        │   estratégias, resultados)           │    │   (pub-sub + streams)      │
        │ - Parquet (séries OHLCV históricas)  │    └─────────────▲─────────────┘
        └──────────────▲───────────────────────┘                  │
                       │                                          │
        ┌──────────────┴──────────────────────────────────────────┴──────────┐
        │ Core Backend (portável — Linux/Docker)                             │
        │                                                                    │
        │  ┌──────────────────────┐   ┌────────────────────────────────┐     │
        │  │ Strategy Engine      │   │ API (FastAPI)                  │     │
        │  │ (event-driven)       │   │ - REST: estratégias, backtests │     │
        │  │ - BacktestBroker     │   │ - WebSocket: progresso, live   │     │
        │  │ - PaperBroker        │   │ - auth (fase 4)                │     │
        │  │ - MT5Broker (proxy → │   └───────────────▲────────────────┘     │
        │  │   Execution Service) │                   │                      │
        │  │ - AI Service (F3)    │                   │                      │
        │  └──────────────────────┘                   │                      │
        └─────────────────────────────────────────────┼──────────────────────┘
                                                      │ HTTPS/WSS
                                          ┌───────────┴───────────┐
                                          │ Frontend React/TS     │
                                          │ - Strategy Builder    │
                                          │ - Resultados/gráficos │
                                          │ - Painel live         │
                                          └───────────────────────┘
```

### 3.2 Decisões de arquitetura (ADRs resumidos)

| ID | Decisão | Justificativa | Alternativa rejeitada |
|----|---------|---------------|------------------------|
| ADR-01 | Engine **event-driven** desde o início | Estratégia única para backtest e live; live trading é inerentemente event-driven | Engine vetorizada (rápida de construir, mas exigiria reescrever a lógica para o live e criaria risco de divergência) |
| ADR-02 | **Coletor e executor isolados** no Windows; core portável | Lib `MetaTrader5` só roda em Windows com terminal aberto; isolar a dependência mantém 90% do sistema em Docker/Linux e prepara a troca futura por APIs de corretora | Rodar tudo no Windows |
| ADR-03 | Estratégia como **JSON declarativo** interpretado pela engine | UI monta o JSON; novos blocos (técnicas do livro) entram sem tocar no core; serializável, versionável, validável | Estratégias como código Python (flexível, porém inviável para UI no-code e perigoso para SaaS) |
| ADR-04 | Interface **`Broker`** com múltiplas implementações | `BacktestBroker`, `PaperBroker`, `MT5Broker`; futuras: Alpaca, IBKR, etc. Núcleo nunca sabe onde a ordem é executada | Acoplamento direto ao MT5 |
| ADR-05 | **Postgres para metadados/trades + Parquet para OHLCV** | Séries históricas são colunar-friendly (leitura rápida com pandas/polars); dados relacionais ficam onde consultas SQL brilham | Tudo no Postgres (OHLCV de anos × vários ativos fica lento e caro) |
| ADR-06 | **Redis** como broker de mensagens (streams/pub-sub) | Simples, suficiente para candles/ordens/fills; upgrade natural p/ RabbitMQ ou NATS se precisar | Kafka (overkill nesta escala) |
| ADR-07 | Custos de transação como **componente plugável** (`CostModel`) | Forex = spread + swap; ações = comissão; índices/futuros = ambos. Multi-ativo sem `if` espalhado | Custos hard-coded |
| ADR-08 | IA via **LLM (API)** para análise e geração de estratégia | Alto valor, resultado confiável, ótima vitrine de AI Engineering (tool use, structured output, validação) | ML preditivo de preço |

### 3.3 Componentes

#### 3.3.1 Data Collector (Windows, Python)
- Conecta ao terminal MT5 (`MetaTrader5` lib oficial).
- **Backfill**: baixa histórico OHLCV por símbolo/timeframe, grava Parquet particionado (`symbol/timeframe/year`) e registra catálogo no Postgres.
- **Live**: assina candles ao vivo dos símbolos com estratégia ativa e publica em `candles.{symbol}.{tf}` no Redis.
- Captura `symbol_info` → tabela `instruments` (tick size, tick value, contract size, moeda, digits, spread típico).
- Resiliência: reconexão automática ao MT5, detecção de gaps e re-backfill.

#### 3.3.2 Strategy Engine (core, Python)
Loop event-driven:

```
para cada evento CandleClosed:
    atualizar indicadores (estado incremental)
    avaliar condições de saída das posições abertas → OrderRequest
    avaliar condições de entrada → OrderRequest
    aplicar RiskManager (position sizing, limites)
    broker.submit(OrderRequest) → Fill events → Portfolio atualizado
```

Interfaces principais:

```python
class Broker(Protocol):
    def submit(self, order: OrderRequest) -> OrderResult: ...
    def positions(self) -> list[Position]: ...
    def account(self) -> AccountState: ...

class Indicator(Protocol):          # estado incremental, O(1) por candle
    def update(self, candle: Candle) -> None: ...
    def value(self) -> float | None: ...

class Condition(Protocol):          # árvore de expressão avaliada por candle
    def evaluate(self, ctx: EvalContext) -> bool: ...

class CostModel(Protocol):
    def entry_cost(self, order, instrument) -> Money: ...
    def exit_cost(self, order, instrument) -> Money: ...

class RiskManager(Protocol):
    def size(self, signal, account, instrument) -> Volume: ...
    def allow(self, order, account) -> bool: ...     # kill switch, max daily loss
```

Implementações de `Broker`:
- **BacktestBroker** — fill simulado no próximo open (padrão anti-lookahead), slippage configurável, aplica `CostModel`.
- **PaperBroker** — preços ao vivo do Redis, execução simulada. Obrigatório antes do real.
- **MT5Broker** — publica `OrderRequest` na fila; o Execution Service executa e devolve o `Fill`.

Regras anti-vieses (inegociáveis, cobertas por testes):
- **Anti-lookahead**: decisão no fechamento do candle N executa no open do candle N+1.
- **Determinismo**: mesmo input ⇒ mesmo output, sempre (seed fixa onde houver aleatoriedade).
- Indicadores só enxergam candles já fechados.

#### 3.3.3 Execution Service (Windows, Python)
- Consome `orders.outbound` (Redis stream), executa via `mt5.order_send`, publica `fills.inbound`.
- **Salvaguardas locais** (funcionam mesmo se o core cair):
  - Kill switch (flag no Redis + arquivo local + endpoint de emergência).
  - Limite de perda diária e de posições simultâneas.
  - Rejeição de ordens fora do horário permitido ou com volume acima do teto.
- Log de auditoria imutável de toda ordem (solicitada, enviada, executada, rejeitada) com timestamps.
- Heartbeat: se o core não responde por N segundos, opção configurável de zerar posições.

#### 3.3.4 API (FastAPI)
- REST: CRUD de estratégias, disparo/consulta de backtests, catálogo de dados/instrumentos, controle de sessões live.
- WebSocket: progresso de backtest, stream de eventos live (fills, P&L, estado).
- Backtests rodam em **worker assíncrono** (fila via Redis + processo worker; Celery/RQ/arq — decidir na implementação), nunca no processo da API.

#### 3.3.5 Frontend (React + TypeScript)
- **Strategy Builder**: formulário composicional (fase 1) evoluindo para builder visual de blocos (fase 2). Sempre gera/edita o JSON da estratégia com validação por JSON Schema compartilhado com o backend.
- **Resultados**: cards de métricas, curva de capital, drawdown, distribuição de trades, tabela de trades, gráfico de candles com entradas/saídas plotadas (**lightweight-charts** da TradingView).
- **Comparador**: N execuções lado a lado.
- **Painel Live** (fase 2/3): posições abertas, P&L do dia, botão de kill switch em destaque.
- Stack: Vite, React Query, Zustand (estado leve), Tailwind, lightweight-charts.

#### 3.3.6 AI Service (fase 3)
Ver §7.

---

## 4. DSL de estratégia (JSON)

Contrato central do sistema. Versionado (`schema_version`), validado por JSON Schema em backend e frontend.

```json
{
  "schema_version": "1.0",
  "name": "MA Cross + Breakout da máxima anterior",
  "description": "Compra quando preço rompe a máxima do candle anterior com tendência de alta pela MM",
  "timeframe": "H1",
  "indicators": [
    { "id": "sma_fast", "type": "SMA", "params": { "period": 9,  "source": "close" } },
    { "id": "sma_slow", "type": "SMA", "params": { "period": 21, "source": "close" } }
  ],
  "entry": {
    "long": {
      "all": [
        { "op": "gt", "left": { "ref": "sma_fast" }, "right": { "ref": "sma_slow" } },
        { "op": "breaks_above", "left": { "ref": "price.high" }, "right": { "ref": "candle[-1].high" } }
      ]
    },
    "short": null
  },
  "exit": {
    "stop_loss":   { "type": "candle_extreme", "params": { "lookback": 1, "side": "low" } },
    "take_profit": { "type": "risk_multiple",  "params": { "rr": 2.0 } },
    "conditions":  [ { "op": "crosses_below", "left": { "ref": "sma_fast" }, "right": { "ref": "sma_slow" } } ]
  },
  "risk": {
    "sizing": { "type": "percent_risk", "params": { "percent": 1.0 } },
    "max_open_positions": 1,
    "max_daily_loss_percent": 3.0
  }
}
```

Princípios:
- **Condições como árvore de expressão** (`all`/`any`/`not` + operadores `gt, lt, crosses_above, crosses_below, breaks_above, breaks_below, ...`). Novos operadores = novas classes registradas num registry; zero mudança no core.
- **Referências** resolvem indicadores (`sma_fast`), preço (`price.close`), candles passados (`candle[-1].high`) e, futuramente, contexto de posição (`position.entry_price`).
- As **técnicas do livro do autor** entram como novos tipos de indicador/condição/setup registrados no mesmo registry — inclusive setups compostos nomeados (ex.: `"type": "setup_9_1"`), que internamente expandem para árvores de condição.

---

## 5. Modelo de dados (PostgreSQL)

```
instruments
  id PK, symbol, name, asset_class (forex|stock|index|future|crypto),
  exchange, currency_base, currency_quote, tick_size, tick_value,
  contract_size, digits, updated_at

datasets  (catálogo do que existe em Parquet)
  id PK, instrument_id FK, timeframe, date_from, date_to,
  candle_count, parquet_path, collected_at

strategies
  id PK, name, description, definition JSONB, schema_version,
  version INT, parent_version_id FK (histórico), created_at, updated_at
  -- versão nova a cada edição; backtests apontam para versão exata

backtests
  id PK, strategy_id FK (versão exata), instrument_id FK, timeframe,
  date_from, date_to, initial_capital, cost_model JSONB,
  status (queued|running|done|failed), error TEXT,
  engine_version, created_at, started_at, finished_at

backtest_metrics
  backtest_id PK/FK, net_profit, gross_profit, gross_loss, total_trades,
  win_rate, payoff, profit_factor, expectancy, max_drawdown_abs,
  max_drawdown_pct, max_dd_duration_days, sharpe, sortino, cagr,
  avg_trade_duration, long_trades, short_trades, equity_curve JSONB

trades  (trade a trade — alimenta estatística e IA futuras)
  id PK, backtest_id FK NULL, live_session_id FK NULL,
  instrument_id FK, direction (long|short),
  entry_time, entry_price, exit_time, exit_price, volume,
  stop_loss, take_profit, exit_reason (sl|tp|condition|kill|manual),
  gross_pnl, costs, net_pnl, r_multiple,
  context JSONB  -- valores dos indicadores no momento da entrada (ouro p/ IA)

live_sessions
  id PK, strategy_id FK, instrument_id FK, mode (paper|real),
  status (active|paused|stopped|killed), started_at, stopped_at,
  config JSONB

order_audit  (append-only)
  id PK, live_session_id FK, order_request JSONB, mt5_response JSONB,
  status (requested|sent|filled|partial|rejected|error),
  requested_at, executed_at

users, api_keys, subscriptions  → fase 4
```

Destaques:
- `trades.context` guarda o snapshot dos indicadores na entrada — é o que permitirá, depois, análises do tipo "essa estratégia só funciona quando o ADX > 25" e alimentará a IA.
- Estratégias são **imutáveis por versão**: reprodutibilidade total de qualquer backtest antigo (junto com `engine_version`).

---

## 6. API (contratos principais)

| Método | Rota | Descrição |
|--------|------|-----------|
| GET    | /instruments | Lista ativos disponíveis e cobertura de dados |
| POST   | /strategies | Cria estratégia (valida JSON Schema) |
| GET/PUT| /strategies/{id} | Lê/atualiza (PUT gera nova versão) |
| POST   | /backtests | Enfileira backtest `{strategy_id, instrument, timeframe, period, capital, cost_model}` |
| GET    | /backtests/{id} | Status + métricas |
| GET    | /backtests/{id}/trades | Trades paginados |
| GET    | /backtests/{id}/equity | Curva de capital |
| WS     | /ws/backtests/{id} | Progresso em tempo real |
| POST   | /live-sessions | Inicia paper/real |
| POST   | /live-sessions/{id}/kill | **Kill switch** |
| WS     | /ws/live/{id} | Eventos live (fills, P&L) |
| POST   | /ai/analyze/{backtest_id} | Relatório IA (fase 3) |
| POST   | /ai/generate-strategy | Texto → JSON de estratégia (fase 3) |

---

## 7. Camada de IA (fase 3) — a vitrine de AI Engineer

Duas features de alto valor e alta credibilidade (nada de previsão de preço):

### 7.1 Analista de backtest (LLM)
Pipeline: métricas + amostra estruturada de trades (incl. `context`) + estatísticas agregadas → prompt estruturado → LLM → relatório em linguagem natural com achados acionáveis.
- Exemplos de output: "73% das perdas ocorrem quando a distância entre as médias é < 0,2× ATR — a estratégia sofre em lateralização"; "o RR 2:1 está deixando dinheiro na mesa: 60% dos TPs continuariam a favor".
- Técnicas exibidas: structured output (JSON), tool use (o LLM pode pedir consultas agregadas ao banco — *function calling* sobre um mini-DSL de queries seguras), citação de trades específicos como evidência, guardrails contra alucinação numérica (todo número citado é verificado contra o banco antes de exibir).

### 7.2 Gerador de estratégia por linguagem natural
"Compre quando o preço romper a máxima do dia anterior com a média de 20 apontando pra cima, stop na mínima, alvo 2:1" → LLM com o JSON Schema da DSL como contrato → estratégia validada → usuário revisa no builder → backtest em 1 clique.
- Loop de auto-correção: se o JSON falha na validação, o erro volta ao LLM (máx. N tentativas).
- É o "wow" da demo pública e o tema do post técnico mais forte.

### 7.3 Futuras (fase 3.5+)
- Agente de otimização: LLM propõe hipóteses de melhoria, dispara backtests, compara e itera (com orçamento de execuções).
- Análise de regime de mercado como filtro sugerido.

---

## 8. Roadmap

### Fase 0 — Fundação (1 semana)
- Monorepo (`apps/api`, `apps/web`, `apps/collector`, `apps/executor`, `packages/engine`, `packages/schema`), Docker Compose (Postgres, Redis), CI (lint, testes), pre-commit.
- **Entregável:** esqueleto rodando, CI verde.

### Fase 1 — MVP Backtest (4-6 semanas)
- Collector: backfill OHLCV + instruments (forex majors + ~20 ações/índices US).
- Engine event-driven: SMA/EMA, condições `gt/lt/crosses/breaks`, stop/alvo (extremo do candle, múltiplo de risco), sizing por % de risco, `BacktestBroker` com custos (spread p/ forex, comissão p/ ações).
- Métricas completas (§5) + persistência trade a trade.
- API + worker assíncrono + UI: builder por formulário, resultados, gráfico com trades.
- **Testes de ouro:** estratégias com resultado calculado à mão validando a engine; testes anti-lookahead.
- **Entregável:** backtest completo via UI, repo público com README forte. *Publicar aqui.*

### Fase 2 — Profundidade (4-6 semanas)
- Mais indicadores (RSI, ATR, Bandas de Bollinger, ADX...) e operadores; primeiros setups do livro como blocos nomeados.
- Grid search de parâmetros (paralelizado nos workers) + **walk-forward analysis** + comparador de execuções.
- Multi-símbolo por backtest. Builder visual de blocos.
- **Entregável:** otimização + walk-forward na UI. *Post técnico #1: "Construindo uma engine de backtest event-driven em Python".*

### Fase 3 — Live + IA (6-8 semanas)
- `PaperBroker` (candles ao vivo) → Execution Service com salvaguardas → `MT5Broker` real.
- Painel live com kill switch. Auditoria completa de ordens.
- IA: analista de backtest (7.1) + gerador de estratégia (7.2).
- **Entregável:** paper→real funcionando + 2 features de IA. *Posts #2 ("LLM que gera estratégias validadas por schema") e #3 ("Do backtest ao live sem reescrever a estratégia").*

### Fase 4 — Produto (contínuo)
- Auth (JWT/OAuth), multi-usuário, planos + billing (Stripe), quotas de backtest, deploy gerenciado (core em cloud + VPS Windows para MT5), onboarding, biblioteca de estratégias-modelo.
- Conectores de corretora via API (Alpaca primeiro — API limpa e paper trading nativo) como novas implementações de `Broker`.

---

## 9. Stack consolidada

| Camada | Tecnologia |
|--------|-----------|
| Linguagens | Python 3.12+ (backend/engine), TypeScript (frontend) |
| API | FastAPI + Pydantic v2, uvicorn |
| Engine | Python puro + numpy; polars/pandas para I/O Parquet |
| Workers | arq ou Celery sobre Redis |
| Dados | PostgreSQL 16, Parquet (pyarrow), Redis 7 (streams/pub-sub) |
| MT5 | lib oficial `MetaTrader5` (collector e executor, Windows) |
| Frontend | React 18, Vite, TypeScript, Tailwind, React Query, Zustand, lightweight-charts |
| IA | API de LLM (Claude) com structured outputs + tool use |
| Infra | Docker Compose (dev), GitHub Actions (CI), VPS Windows p/ MT5 |
| Qualidade | pytest (+hypothesis p/ property-based na engine), ruff, mypy, ESLint, Playwright (e2e básico) |

---

## 10. Estratégia de testes

1. **Engine = cobertura máxima.** Testes de ouro com resultados calculados à mão; property-based (hypothesis): P&L da soma dos trades == variação do equity, nenhum trade usa dado futuro, determinismo (rodar 2× ⇒ resultado idêntico).
2. **Anti-lookahead como teste permanente**: dataset sintético onde qualquer vazamento de futuro gera lucro impossível → teste falha se lucro > limiar.
3. **Contratos**: JSON Schema da DSL testado nos dois lados; testes de API com schemathesis.
4. **Execution Service**: MT5 mockado; cenários de rejeição, fill parcial, desconexão, kill switch.
5. **E2E leve**: fluxo criar estratégia → backtest → ver resultado (Playwright, dataset pequeno).

---

## 11. Segurança e salvaguardas de execução real

- **Paper antes de real, sempre**: sessão real só pode ser criada para estratégia com N dias de paper trading registrados (configurável).
- Kill switch em 3 camadas: UI → API → flag local no Execution Service (funciona mesmo com o core fora do ar).
- Limites no executor (independentes do core): perda diária máx., volume máx. por ordem, posições simultâneas máx., janela de horário.
- Auditoria append-only de toda ordem.
- Segredos (login MT5, chaves) via variáveis de ambiente/secret manager; nunca no repo.
- Disclaimer legal claro no produto: ferramenta de análise, não recomendação de investimento (relevante para venda futura).

---

## 12. Vitrine e posicionamento

**Repo público (GitHub):**
- README em inglês: GIF da demo, diagrama de arquitetura, seção "Design Decisions" (os ADRs do §3.2 resumidos), badges de CI/cobertura, roadmap aberto.
- Código exemplar: type hints completos, docstrings, commits limpos (conventional commits), PRs consigo mesmo com descrição (mostra processo).

**Demo online:** versão hospedada com dataset histórico embutido (não depende do MT5) — recrutador testa em 30 segundos. Backtest limitado a datasets de exemplo.

**Conteúdo (LinkedIn/dev.to, 1 por fase):**
1. "Construindo uma engine de backtest event-driven em Python — e por que não vetorizei" (fase 2)
2. "Usando LLMs para gerar estratégias de trading validadas por JSON Schema" (fase 3)
3. "Do backtest à execução real sem reescrever uma linha da estratégia" (fase 3)

**Narrativa de entrevista** (o projeto responde às perguntas clássicas de system design): trade-offs documentados (ADRs), sistema distribuído real (4 serviços + fila), problema de domínio não-trivial (vieses de backtest), IA aplicada com guardrails — não "chamei uma API", e sim structured output, tool use, validação e auto-correção.

---

## 13. Riscos e mitigações

| Risco | Impacto | Mitigação |
|-------|---------|-----------|
| Dependência do MT5/Windows | Fragilidade operacional | Isolamento em 2 serviços pequenos; abstração `Broker`; Alpaca na fase 4 |
| Engine com viés (lookahead, survivorship) | Resultados enganosos → decisões ruins com dinheiro real | Testes de ouro + property-based + anti-lookahead permanente; fills no próximo open |
| Escopo crescer antes do MVP | Nunca publicar | Fases fechadas com entregáveis; publicar ao fim da fase 1 |
| Perda financeira no live | Real | Paper obrigatório, kill switch em camadas, limites no executor |
| Custo de LLM na demo pública | Financeiro | Rate limit por IP, cache de análises, modelo econômico p/ demo |
| Qualidade dos dados MT5 (gaps, splits em ações) | Métricas distorcidas | Validação de integridade no collector; relatório de gaps por dataset |

---

## 14. Glossário

- **Walk-forward**: otimizar em janela A, validar na janela B seguinte, deslizar — evita overfitting.
- **Expectancy**: (win_rate × ganho médio) − (loss_rate × perda média); valor esperado por trade.
- **R-multiple**: resultado do trade medido em múltiplos do risco inicial.
- **Lookahead bias**: usar informação do futuro na decisão do presente; o pecado capital do backtest.
- **Slippage**: diferença entre preço esperado e preço executado.

---

*Próximo passo sugerido: Fase 0 — criar o monorepo, docker-compose e o JSON Schema v1 da DSL de estratégia.*