# ADR-0025 — A ordem que o mercado encerrou

- **Status**: aceito
- **Data**: 2026-09-06
- **Contexto do PR**: PR-194

## Contexto

`BacktestBroker._fill_resting` tem dois caminhos que **removem** uma ordem do book sem preencher
e sem que ninguém a tenha cancelado:

- `_survives_the_gap` — o preço abriu através do nível *e* através do stop medido a partir dele,
  então o fill abriria uma posição já do lado errado da própria saída;
- `_survives_the_slip` (ADR-0016) — o stop dispararia tão longe do gatilho que a posição
  carregaria risco que o gestor nunca dimensionou.

As duas remoções estão certas e justificadas nos próprios docstrings. **O que não existia era o
registro.** O único vestígio era um `logger.debug`, e a estratégia não lê log.

Medido em 06/09/2026, corrida solta dos cinco `entry_point` sobre M15 real de 2025 (AAPL e
EURUSD, 20 cenários, 725 trades): **18 descartes por gap e 1 por slip**, e `RunResult.refusals`
devolveu `0` nos vinte. O sintoma que denunciou foi aritmético — `zonas − fills − cancelamentos
que acertaram` não fechava em três casos, e `cancel` errava exatamente uma vez para cada ordem
que o broker tinha descartado: a estratégia seguia acreditando na ordem e mandava cancelar um
nome que o book não tinha mais.

Isso contradiz a promessa escrita no docstring de `RunResult.refusals` — *"o registro de toda
ordem que foi pretendida e não aconteceu"*, e *"a pergunta que ele responde é a que um backtest
não podia: por que isto não tomou trade nenhum?"*. E do lado da estratégia é o fantasma do
ADR-0023 chegando por uma porta nova: a fase acredita que uma ordem descansa onde não descansa, e
deixa de oferecer a zona.

**Um segundo buraco, achado ao consertar o primeiro.** `iter_run` drenava `broker.refusals()`
depois do `yield`, direto para o `Context` da barra seguinte, **sem nunca tocar num
`BarOutcome`** — e `run()` é um acumulador de `BarOutcome`. Ou seja: o que o broker reportava
chegava à estratégia e **nunca** ao registro da corrida. Ficou invisível enquanto o único broker
de backtest devolvia a tupla vazia; no dia em que um deles passou a responder, as 19 recusas
foram para o lugar nenhum.

## Decisão

Uma ordem em repouso que o mercado encerrou é reportada como `Refusal` com
**`RefusedBy.MARKET`**, um membro novo; e a caixa de correio do broker — drenada uma vez por
barra, no mesmo ponto de sempre — passa a chegar ao `BarOutcome` da barra seguinte, porque
`_step` devolve os portões separados do registro (trade-off 3).

`RefusedBy.MARKET` **não conta contra o `MAX_ARMING_ATTEMPTS`**, e quebra a sequência em vez de
incrementá-la.

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| **(A)** Reusar `RefusedBy.BROKER` | zero contrato novo; o encanamento já entrega | Mente duas vezes: o docstring de `Refusal` diz *"nunca chegou ao book"*, e esta chegou; e `_observe_refusal` contaria contra o cap — o próprio docstring de lá diz que gastar tentativa assim *"aposentaria zonas que estão funcionando exatamente como pretendido"*. ⚠️ Pelo loop de hoje isso é **latente**: a `MARKET` chega em N+2 e o ramo `not gated` já zerou a sequência em N+1, então a contagem nunca passa de 1 (medido pelo guardian). Vira aposentadoria real no dia em que a entrega for na própria barra (backlog). A objeção a (A) é o contrato errado, não um trade que estivesse sendo perdido hoje |
| **(B)** Membro novo em `RefusedBy`, alargando o docstring da classe | aditivo de verdade — **até este PR nada no código ramificava em `refused_by`**: produzido em seis lugares no `develop` (os quatro portões de `loop.py`, o hand-over em `warmup.py`, o `live/broker.py` da API) e lido em um só, um `logger.debug` em `loop.py`. O primeiro `if` sobre ele é o deste PR, em `_observe_refusal`. E não existe enum disso no banco: sem migration, sem consumidor a atualizar; a taxonomia por causa é exatamente o que `RefusedBy` documenta como sua função | Um membro que não é um *portão*, num enum cujo nome diz "recusado por" |
| **(C)** Tipo de desfecho novo (`Withdrawal`), com campo próprio | o mais honesto na taxonomia: nem fill nem recusa | Contrato novo atravessando `BarOutcome`, `Context`, o fio do ADR-0024, o executor e todo consumidor — custo desproporcional a uma diferença que nenhum consumidor lê |

**Escolhida: (B).**

O argumento que decidiu: o que os membros de `RefusedBy` têm em comum **não** é *"nunca chegou ao
book"* — isso era a descrição do único jeito de acontecer até agora, não a invariante. O que
todos compartilham é a **instrução**: *pare de acreditar que essa ordem existe*. `MARKET` cumpre
essa invariante inteira. O docstring foi corrigido para dizer isso.

## Trade-off aceito

**1. Um membro que não é um portão.** `RefusedBy.MARKET` fica num enum cujo nome sugere um
agente que recusou, e aqui não houve nenhum. Aceito porque o eixo que o enum realmente serve é
*"o que a estratégia deve fazer a respeito"*, e nisso `MARKET` é irmão legítimo dos outros seis
— com uma diferença, o cap, que fica presa em teste.

**2. A entrega continua uma barra depois.** A retirada nasce no passo 1 de `_step` — dentro de
`broker.on_bar`, na mesma chamada que produz os fills —, **antes** de o `Context` da mesma barra
ser montado. Os fills daquela chamada são mostrados na própria barra; a retirada não. Aceito
porque `Context.refusals` significa "a da barra anterior" para todos os outros membros, e uma
regra de entrega que a estratégia consegue enunciar vale mais do que uma barra de frescor. O
custo é medido e limitado — a fase segura um nome morto por exatamente uma barra. Dos 19
descartes de 2025, o conserto eliminou **4** cancelamentos fantasma; os demais aconteciam dentro
dessa uma barra.

⚠️ Fechar isso é **uma** drenagem no topo de `_step` roteada para o `Context` da mesma barra —
e ela cobriria uma janela **maior** que a de hoje, porque o `for` de `iter_run` bloqueia
esperando a vela seguinte *depois* da drenagem atual, e o topo da barra vê também o que chegou
nessa espera. O que impede não é custo de leitura: é que `Context.refusals` deixaria de
significar "a da barra anterior", o que reprende `carrying == [1]` em `test_loop` e mexe no que
`_observe_refusal` afirma sobre quando uma recusa pode chegar. Está anotado no backlog com esse
preço, e com o prêmio.

**3. O registro fica uma barra depois do fato; a entrega, não.** ⚠️ **A primeira versão deste
ADR decidiu o contrário, sobre uma premissa falsa, e o `engine-guardian` reprovou.** Eu tinha
movido a drenagem de `broker.refusals()` para o topo da barra, afirmando que a entrega à
estratégia ficava idêntica e a janela do live "estritamente maior". A janela até era maior; a
entrega não era idêntica, e o erro foi de **roteamento**, não de posição: o que a drenagem
antiga pega na **retomada do gerador** — depois do `yield` e do corpo do consumidor, o que inclui
o veredito do executor que chega 8 ms depois do `submit` (ADR-0024), no passo 3 da própria barra
N — vai direto para `pending` e para o `Context` de N+1. A primeira versão drenava no topo de
`_step` e punha o resultado no `BarOutcome` daquela barra, de onde `pending` o levava para o
`Context` da barra **seguinte à seguinte**. Medido nos dois lados, mesmo fake, único delta a
drenagem e seu destino:

| drenagem | ordem submetida na barra 2, recusada pelo venue dentro do `submit` | `RunResult.refusals` |
|---|---|---|
| depois do `yield` (`develop`) | estratégia soube na barra **3** | `[]` ← o bug |
| topo da barra (1ª versão deste PR) | estratégia soube na barra **4** | `['venue']` |
| depois do `yield` + `_step` devolve os portões (final) | estratégia soube na barra **3** | `['venue']` |

Numa sessão live M15 a versão do meio custava **quinze minutos** acreditando numa ordem que o
venue nunca aceitou, e uma chance a menos de rearmar a zona antes de a região ser mitigada. Nada
ficava vermelho, porque o único teste da caixa de correio enche a caixa no `__init__` — a única
janela de chegada que as duas drenagens tratam igual. Foi acrescentado
`test_a_verdict_that_lands_during_submit_still_reaches_the_next_bar` para prender a janela real.

A saída foi desacoplar registro de entrega em vez de trocar um pelo outro: `_step` — que é
privado, então dizer isso no tipo de retorno não custa contrato — devolve `(BarOutcome, portões)`.
O `BarOutcome` carrega o que a caixa entregou **mais** os portões desta barra, e é o registro;
`pending` é montado só com os portões mais a drenagem nova, e é a entrega. Assim cada recusa
aparece **exatamente uma vez** de cada lado. O preço: uma recusa drenada depois da barra N é
registrada no `BarOutcome` de N+1. `RunResult.refusals` é uma lista plana e não se importa;
`StructurePhase` se importa muito com em que barra fica sabendo.

E continua sendo **uma** leitura por barra, que `test_loop` prende. ⚠️ Não pelo motivo do
`account()`, que a primeira versão deste texto invocou: duas leituras de saldo podem discordar,
mas uma drenagem não discorda — a segunda devolve o que chegou depois da primeira. O que uma
segunda drenagem custaria é uma ida e volta a mais por barra contra o terminal e a mudança de
significado de `Context.refusals` do trade-off 2; o que ela compraria é a janela maior do mesmo
trade-off. Nenhum dos dois é assunto deste PR.

## Consequências

- `RunResult.refusals` e `BarOutcome.refusals` passam a conter o que o broker reporta fora de
  banda. Em live isso inclui `EXECUTOR` e `VENUE`, que também nunca chegavam ao registro.
- `StructurePhase` para de acreditar numa ordem que o mercado encerrou, e a zona **não** é gasta
  (só o FILL gasta região — a regra dele, inalterada).
- **Comportamento de trade inalterado, e provado:** nos mesmos 20 cenários de 2025, `equity`,
  `trades` e `fills` são idênticos em 20 de 20 antes e depois. O que muda é o registro, a crença
  da estratégia, e 4 cancelamentos fantasma a menos.
- **Doze** mutantes escritos à mão contra o diff final, doze mortos com nome de teste — incluindo o que separa *"ler a caixa
  de correio"* de *"registrar o que veio nela"*, o que separa `MARKET` de `EXECUTOR` no cap
  usando **o mesmo cenário com um único campo diferente**, e o que apaga o `pop` do ramo
  `MARKET` (este sobreviveu à primeira versão do teste, que usava três barras onde `pop` e
  "só não incrementar" produzem o mesmo número; separam-se com quatro).
- Fica em aberto no backlog: entregar a retirada na própria barra, e a regra dele de recolher a
  ordem antes do fechamento da sessão (que precisa de calendário e **não** pode ser deduzida da
  série, sob pena de lookahead).
