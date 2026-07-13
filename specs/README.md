# Specs — como executar este projeto

Cada fase tem um spec com a lista de PRs em ordem. O agente (Claude Code/Cursor) deve:

1. Ler `AGENTS.md` (regras, modo professor, aprovações) e o spec da fase atual.
2. Anunciar o próximo PR: escopo + **o que o Guilherme vai aprender nele**.
3. Explicar o plano (decisão → alternativas → trade-off → escolha) ANTES de codar.
4. Implementar com testes. PRs que tocam `packages/engine` passam pelo subagent `engine-guardian`.
5. Gerar a lição em `docs/aulas/PR-XXX-<slug>.md` (subagent `professor`).
6. Commit (conventional) — pedir aprovação, pois altera estado.

Ordem: `fase-0.md` → `fase-1.md` → `fase-2.md` → `fase-3.md` → `fase-4.md`.

## Fluxo de branches (git-flow)

```
feat/pr-XXX-slug  →  PR  →  develop  →  PR  →  main
                             (staging)         (produção)
```

`main` e `develop` recebem **merge, nunca commit direto** — o hook `no-commit-to-branch` bloqueia
localmente e a branch protection do GitHub bloqueia no servidor. Os deploys automáticos (futuros)
vão pendurar nessas duas branches.

Regras de escopo:
- Um PR = um escopo. Ideias fora do escopo → `backlog.md`.
- Critério de aceite não cumprido = PR não terminou.
- Divergência do `sdd.md` = registrar ADR em `docs/adr/` e explicar o trade-off.

Status: manter a tabela abaixo atualizada.

| Fase | Status | Início | Fim |
|------|--------|--------|-----|
| 0 — Fundação | em andamento (PR-001) | 13/07/2026 | |
| 1 — MVP Backtest | não iniciada | | |
| 2 — Profundidade | não iniciada | | |
| 3 — Live + IA | não iniciada | | |
| 4 — Produto | não iniciada | | |
