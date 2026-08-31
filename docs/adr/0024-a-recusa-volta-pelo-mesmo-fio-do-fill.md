# ADR-0024 — A recusa volta pelo mesmo fio do fill

- **Status**: aceito
- **Data**: 2026-08-28
- **Contexto do PR**: PR-304-B-D-2a

## Contexto

`Broker.submit` responde `accepted` — e está certo desde a fase 1: *"queue an order; returns
whether it was **accepted**, not whether it **executed**"*. Aceito é um fato local, conhecível no
instante em que a ordem entra no stream.

Mas o executor pode recusar **depois**, três processos adiante e de forma assíncrona. Medido em
produção em 28/08/2026: `demand-20260730T1500-360` respondeu `accepted` ao `MT5Broker` e foi
`refused` pelo executor 8 ms depois. A estratégia atravessou acreditando que armou uma limite que
o venue nunca viu — o fantasma que o ADR-0023 mediu em 4 de 5 pontos de hand-over, chegando pelo
lado que ninguém olhava.

O PR-304-B-D-1 abriu o canal **dentro** da engine (`Context.refusals`, `Refusal`,
`StructurePhase._observe_refusal`), e fechou as quatro portas síncronas. O que sobrou é o
transporte: uma recusa que nasce noutro processo não tem como chegar nesse canal.

Duas restrições que o código já tinha decidido, e que este ADR respeita em vez de reabrir:

1. **`_publish_fill` recusa carregar recusa como fill.** *"a 'fill' of zero would be a session
   waiting for a position it will never get told about"*. Correto.
2. **`orders.outbound` é um stream com três formas** (`WireOrder | WireCancel | WireModifyStop`)
   e um `kind` que *"is read first and never guessed"* — porque adivinhar transforma uma entrada
   ilegível em **ordem enviada**.

## Decisão

**A recusa volta pelo mesmo stream do fill, como uma forma irmã discriminada por `kind`** — e o
stream é renomeado de `fills.inbound` para `venue.outcomes`, porque passa a carregar desfechos e
não só preenchimentos.

```
Outcome = WireFill | WireRefusal        (um stream, uma ordem de chegada, kind estrito)
```

O `Broker` ganha um método para entregar o que chegou fora de banda; o loop o consulta uma vez
por barra e funde o resultado no `Context.refusals` da barra seguinte, pelo mesmo caminho que as
recusas síncronas já usam.

E `RefusedBy` ganha os dois membros que o PR-304-B-D-1 deliberadamente não inventou por não terem
produtor: `EXECUTOR` (salvaguarda nossa) e `VENUE` (o terminal disse não).

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| **Stream próprio para recusas** (`orders.refused`) | Nenhuma mudança de formato; o fill continua puro | **Duas ordens de chegada para eventos do mesmo pedido.** É exatamente o que a ida documenta ter evitado: *"One stream is one order of arrival. That is the whole argument"*. Um fill e uma recusa da mesma ordem poderiam ser lidos fora de ordem |
| **Recusa como `WireFill` de volume zero** | Zero mudança de formato | Já vetado pelo `_publish_fill`, com razão: `Fill` recusa preço zero, e a sessão ficaria esperando uma posição que nunca chega |
| **Manter `fills.inbound` e só somar `kind`** | Sem rename, 6 arquivos a menos | O nome passa a **mentir** — recusa não é fill. E os 184 registros legados sem `kind` viram 184 logs de "unreadable fill" por sessão nova, porque o grupo nasce em `id="0"` |
| **A sessão consulta o `order_audit` no banco** | Sem mudança de fio nenhuma | Acopla a engine ao esquema do banco, e o `order_audit` é trilha de auditoria, não canal. Uma consulta por barra a uma tabela que só cresce |
| **O `submit` passa a ser síncrono e espera o veredito** | A estratégia saberia na hora | Quebra o protocolo da fase 1 e bloqueia o loop numa ida e volta entre processos. Um venue lento vira uma sessão travada |

## Trade-off aceito

**Sacrificamos a pureza do nome `Fill` no stream e pagamos um rename de 39 ocorrências em 6
arquivos**, em troca de uma única ordem de chegada para tudo que acontece com um pedido.

O rename só é barato por um fato medido: **zero entradas reais** em `fills.inbound` — os 184
registros são todos de teste (`test-…`, `somebody-else-…`). Nenhum fill de verdade jamais cruzou.
Se houvesse dado real, a escolha honesta seria manter o nome e aceitar o caminho de compatibilidade.

⚠️ **E sacrificamos a simetria do `Broker`:** ele passa a ter um método que só o broker de venue
tem o que dizer. O `BacktestBroker` o responde vazio, sempre — e isso é verdade, não um stub: num
backtest não existe recusa fora de banda, porque não existe fora de banda.

## Consequências

**Três estados, não dois.** A distinção passa a ser explícita no `_publish_fill`:

```
enviada e preencheu       → WireFill      (como hoje)
enviada e está em repouso → nada          (como hoje) ← a limite viva, nem fill nem recusa
NÃO foi enviada           → WireRefusal   (novo)
```

Publicar algo pela ordem em repouso seria afirmar que algo aconteceu quando o que houve foi uma
ordem esperando.

**O que muda:**

- `wire.py`: `VENUE_OUTCOMES` substitui `FILLS_STREAM`; `WireRefusal`, `outcome_fields`,
  `outcome_from_fields` com `kind` estrito. O stream antigo fica órfão e é descartável.
- `service.py`: `_publish_fill` vira `_publish_outcome` e passa a publicar os dois desfechos.
- `broker.py`: o `MT5Broker` traduz `WireRefusal` para a `Refusal` da engine e a guarda até o
  próximo `on_bar`.
- `protocols.py` + `loop.py`: o `Broker` ganha o método, e o loop funde o que ele devolve nas
  recusas pendentes da barra seguinte.
- `domain.py`: `RefusedBy.EXECUTOR` e `RefusedBy.VENUE`.

**O que isto NÃO resolve, e fica declarado:**

- **Uma limite que preenche minutos depois do ack não tem quem avise** — nada neste ADR olha para
  ordens em repouso. É mecanismo diferente (alguém perguntando ao venue), não formato de mensagem.
- **Um `cancel` recusado** continua sem voltar. Uma ordem que a estratégia acha que retirou e
  segue no livro é o fantasma pelo terceiro lado, e o `Refusal` da engine hoje se correlaciona com
  entradas armadas, não com cancelamentos.
- **A retentativa sob recusa permanente** — medido, 48 re-armagens por zona — só fica alcançável
  *depois* deste ADR, porque é ele que traz a recusa permanente (AutoTrading desligado) de volta.
  O teto é decisão do PR-304-B-D-2c, e é para isso que `EXECUTOR` e `VENUE` são membros separados.
