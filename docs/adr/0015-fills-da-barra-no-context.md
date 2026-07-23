# ADR-0015 — A estratégia enxerga os fills da própria barra

- **Status**: aceito
- **Data**: 2026-07-23
- **Contexto do PR**: PR-204 (maquinaria de entrada compartilhada)

## Contexto

A regra do método (ditada pelo Guilherme em 23/07) é: **uma entrada por região — colocou a
ordem e ativou o trade, a região fica inválida para um próximo trade**. A queima da zona
acontece no **fill**, não na colocação: uma ordem retirada sem nunca preencher (substituída
por uma zona mais nova) devolve a região ao jogo.

Para aplicar essa regra a estratégia precisa *observar* o fill. `Context.position` não
basta: uma ordem limite pode preencher e ser estopada pelo pavio **da mesma barra** — a
posição nasce e morre no passo 1 do loop, antes de a estratégia rodar no passo 2, e
`context.position` chega `None`. Sem outro sinal, a zona pareceria nunca operada e a
maquinaria rearmaria a mesma região depois do stop — o martingale que a revisão do PR-204
(rodada 1) bloqueou.

## Decisão

`Context` ganha o campo aditivo `fills: tuple[Fill, ...] = ()` com os fills nascidos
**dentro da barra corrente** (o que o passo 1 do loop acabou de produzir). A
`StructureStrategy` observa o próprio fill pelo `client_id` (com a posição aberta como
sinal reserva) e só então grava a zona como operada.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|-------------|------|---------|
| `Context.fills` (escolhida) | Aditiva; fiel ao live (a corretora notifica fills na hora); resolve o caso fill+stop na mesma barra; sem inferência | Muda o contrato do `Strategy` (por isso este ADR) |
| Inferir pelo `position` | Nenhuma mudança de contrato | Cega para o trade que nasce e morre na mesma barra → martingale volta |
| "Desqueimar" a zona quando a estratégia retira a ordem | Nenhuma mudança de contrato | Uma retirada espúria de ordem já consumida reabilitaria zona operada — pior que o bug original |

## Trade-off aceito

O contrato do `Strategy` cresce um campo. Aceito porque é aditivo (default `()`), porque
não é lookahead — fills do passo 1 da barra N são eventos da barra N entregues a quem
decide no fechamento de N, exatamente como uma notificação de fill do MT5 — e porque
elimina uma classe de inferência frágil dentro das estratégias.

## Consequências

- `loop.py` repassa os fills da barra ao `Context` (mesma tupla que já ia para o
  `RunResult`).
- `StructureStrategy` mantém `_traded` (zonas que geraram trade) no lugar de `_ordered`
  (zonas que receberam ordem); zona retirada sem fill volta a ser elegível.
- Efeito colateral desejado: ao observar o fill a estratégia esquece `_armed`, o que
  elimina o cancel espúrio apontado na revisão (ATENÇÃO 2 da rodada 2).
- Um futuro broker live deve popular `Context.fills` com as notificações de fill da
  barra; a reconciliação completa estratégia↔broker em live continua no backlog.
