# TradeForge — Claude Code

@AGENTS.md

## Notas específicas do Claude Code

- Subagents disponíveis em `.claude/agents/`: use `engine-guardian` antes de finalizar qualquer PR que toque `packages/engine`; use `professor` para gerar a lição do PR em `docs/aulas/`.
- O `engine-guardian` tem **dois modos, e o prompt precisa dizer qual**: `MODO: FULL` na primeira rodada (revisa o diff inteiro, teto de 14 mutantes) e `MODO: DELTA` quando ele já reprovou e você consertou (verifica só os bloqueantes dele + o delta desde a última rodada, teto de 6). Rodada DELTA pode ir de `model: "sonnet"` — o trabalho é confirmatório e o escopo já está fechado. Nunca peça para ele re-revisar o que já aprovou: foi isso que fez uma rodada de duas correções de uma linha levar mais de uma hora.
- Permissões pré-aprovadas para leitura/verificação estão em `.claude/settings.json` — não peça confirmação para o que já está na allowlist.
- Ao iniciar uma sessão de trabalho, leia o spec da fase atual em `specs/` e diga ao Guilherme qual é o próximo PR e o que ele vai aprender nele.
