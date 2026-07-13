# Fase 3 — Live + IA (6-8 semanas)

**Objetivo:** a mesma estratégia do backtest rodando em paper e depois em conta real via MT5, com salvaguardas; duas features de IA de alto valor.
**Referência:** sdd.md §3.3.3, §7, §8 (Fase 3), §11.

⚠️ **Regra da fase:** nenhum código desta fase toca conta real sem: paper trading funcionando, kill switch testado, limites no executor testados. Ordem dos PRs é obrigatória.

## PR-301 — Collector live: candles em tempo real
**Escopo:** collector assina símbolos ativos e publica `CandleClosed` em Redis streams (`candles.{symbol}.{tf}`); detecção de candle fechado vs em formação; reconexão; gap-fill ao reconectar.
**Aceite:** stream contínuo em conta demo MT5; teste de reconexão (matar e religar o MT5).
**Você vai aprender:** streams vs pub/sub no Redis, consumer groups, o problema clássico do "candle ainda não fechou".

## PR-302 — PaperBroker + live_sessions
**Escopo:** `PaperBroker` (mesma interface Broker): consome candles ao vivo, simula fills com spread real do symbol_info; tabela `live_sessions` (mode=paper); engine rodando como processo de sessão; persistência de trades live com `context`.
**Aceite:** estratégia da fase 1 roda em paper com dados ao vivo; trades persistidos idênticos em formato aos de backtest.
**Você vai aprender:** a prova viva do ADR-01 — a MESMA classe de estratégia rodando contra outro broker; gestão de processos de longa duração.

## PR-303 — Execution Service + salvaguardas
**Escopo:** `apps/executor`: consome `orders.outbound`, executa `mt5.order_send` (conta DEMO), publica `fills.inbound`; kill switch em 3 camadas (flag Redis + arquivo local + endpoint); limites locais (perda diária, volume máx., posições máx., janela de horário); `order_audit` append-only; heartbeat com política configurável.
**Aceite:** suíte com MT5 mockado cobrindo: rejeição, fill parcial, desconexão, kill switch em cada camada, limite estourado; execução real em CONTA DEMO.
**Você vai aprender:** design de sistemas que falham com segurança (fail-safe vs fail-open), auditoria append-only, por que os limites vivem no executor e não no core.

## PR-304 — MT5Broker + painel live
**Escopo:** `MT5Broker` (proxy via fila para o executor); UI: painel de sessões live (posições, P&L do dia, estado, log de eventos) com **kill switch em destaque**; promoção paper→real exige N dias de paper registrados (configurável).
**Aceite:** fluxo completo em conta demo: ativar sessão → ordem → fill → painel atualiza via WS → kill switch encerra tudo; trava de paper prévio testada.
**Você vai aprender:** UX de sistemas críticos, idempotência de ordens (retry sem duplicar), reconciliação de estado (posições no MT5 vs no banco).

## PR-305 — IA: analista de backtest
**Escopo:** `POST /ai/analyze/{backtest_id}`: pipeline métricas + trades (incl. `context`) + agregações → prompt estruturado → LLM (Claude API) com tool use (função de consulta agregada segura ao banco) → relatório em linguagem natural com structured output; guardrail: todo número citado é conferido contra o banco antes de exibir; cache por backtest.
**Aceite:** relatório útil e SEM números alucinados (teste automatizado: extrair números do output e validar); custo por análise registrado.
**Você vai aprender:** engenharia de LLM de verdade — structured outputs, tool use, grounding, avaliação de alucinação numérica. Núcleo da vitrine de AI Engineer.

## PR-306 — IA: gerador de estratégia por linguagem natural
**Escopo:** `POST /ai/generate-strategy`: descrição textual → LLM com o JSON Schema como contrato → validação → loop de auto-correção (erro de schema volta ao LLM, máx. 3 tentativas) → abre no builder para revisão humana; suite de avaliação com 15+ descrições e estratégias esperadas.
**Aceite:** ≥80% das descrições da suite geram estratégia válida e semanticamente correta; inválidas falham com mensagem clara, nunca com JSON quebrado.
**Você vai aprender:** LLM com contrato de schema, loops de auto-correção, como construir uma eval suite (a skill de AI Engineering mais pedida em 2026).

## Entregável da fase
Paper → real (demo) funcionando + 2 features de IA. **Posts #2 e #3** (sdd.md §12).
