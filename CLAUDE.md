# TradeForge — Claude Code

@AGENTS.md

## Notas específicas do Claude Code

- Subagents disponíveis em `.claude/agents/`: use `engine-guardian` antes de finalizar qualquer PR que toque `packages/engine`; use `professor` para gerar a lição do PR em `docs/aulas/`.
- Permissões pré-aprovadas para leitura/verificação estão em `.claude/settings.json` — não peça confirmação para o que já está na allowlist.
- Ao iniciar uma sessão de trabalho, leia o spec da fase atual em `specs/` e diga ao Guilherme qual é o próximo PR e o que ele vai aprender nele.
