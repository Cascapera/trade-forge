# Fase 4 — Produto (contínuo)

**Objetivo:** transformar a ferramenta em SaaS vendável.
**Referência:** sdd.md §8 (Fase 4), §11, §12.

Esta fase é menos prescritiva — detalhar cada PR quando a fase 3 terminar e houver aprendizado real de uso. Blocos previstos:

## Bloco A — Multi-usuário
- Auth (JWT + OAuth social), tabela `users`, ownership em estratégias/backtests/sessões, isolamento por usuário em todas as queries (testes de autorização são obrigatórios).

## Bloco B — Monetização
- Stripe (planos + webhooks), quotas por plano (backtests/mês, símbolos, sessões live), feature flags.

## Bloco C — Deploy gerenciado
- Core em cloud (containers), VPS Windows para collector/executor MT5, observabilidade (logs estruturados, Sentry, métricas), backups do Postgres, staging.

## Bloco D — Produto
- Onboarding guiado, biblioteca de estratégias-modelo, demo pública com rate limit e cache de análises IA, disclaimer legal (ferramenta de análise, não recomendação de investimento) e termos de uso.

## Bloco E — Corretoras via API
- `AlpacaBroker` como segunda implementação real de `Broker` (paper nativo da Alpaca primeiro) — valida que a abstração aguenta uma corretora não-MT5; depois avaliar IBKR/outras conforme demanda.

**Antes de iniciar qualquer bloco:** revisar prioridades com o Guilherme e detalhar os PRs no padrão das fases 1-3.
