# ADR-0036 — O servidor fica no Xeon e o i9 só empresta a folga

- **Status**: proposto
- **Data**: 2026-10-16
- **Contexto do PR**: pedido dele de 16/10, depois do segundo reinício de madrugada por Windows Update
  em três dias ("o xeon é melhor para ficar ligado direto e este eu preciso muitas vezes fazer
  tarefas pesadas"); implementação em cinco PRs depois do aceite, mais o dia da virada
- **Relaciona-se com**: ADR-0021 (agente de coleta no host), ADR-0029 (lotes), ADR-0032 (várias
  corretoras), ADR-0033 (sinais por mercado), `docs/referencia/worker-remoto.md`,
  `docs/referencia/servidor-xeon.md` (o plano e o roteiro da virada)

## Contexto

Hoje **tudo o que precisa ficar ligado roda no PC de trabalho dele** (i9-14900K, 64 GB):

| No i9 | Por quê está lá |
|---|---|
| Postgres (81 GB em 16/10), Redis (fila das varreduras, streams e contadores dos sinais) | Foi onde o projeto nasceu |
| API e web | Idem |
| 3 a 16 workers | A máquina mais rápida |
| Terminais MT5 (XP, ActivTrades, Tradeview), um agente de coleta por corretora | A biblioteca `MetaTrader5` só roda no Windows (ADR-0021) |
| Supervisor dos sinais e notificador (Discord/Telegram) | Precisam dos terminais e do Redis |

O Xeon (E5-2697A v4, 16 núcleos/32 threads, 32 GB ECC, mais 32 GB planejados) só roda 10 workers
remotos (`docker-compose.worker.yml`).

Isso deu errado de três jeitos nas últimas semanas:

1. **O i9 é a máquina que ele usa para trabalhar e para as tarefas pesadas.** Em 12/10 o WSL foi
   baixado para 24 GB para o replay de ML rodar no Windows; em 15/10, com uma varredura de M5 longo,
   a VM travou: Postgres e Redis pararam junto, e os sinais ficaram 14 minutos sem vela.
2. **Qualquer reinício do i9 derruba o sistema inteiro**: a fila, a API, os sinais e todos os workers
   remotos, que ficam sem banco. Em 14/10 e 16/10 o Windows Update reiniciou o i9 de madrugada; nas
   duas vezes os sinais só voltaram quando alguém abriu os terminais e rodou o script à mão. Os
   agentes de coleta também não voltam sozinhos.
3. **O Xeon fica ligado o tempo todo e ele mexe nele uma ou duas vezes por semana**, mas é justamente
   nele que só roda o que pode parar.

## Decisão

**O Xeon passa a ser o servidor: Postgres, Redis, API, web, os terminais MT5, os agentes de coleta,
o supervisor dos sinais e o notificador rodam nele, junto com alguns workers. O i9 vira uma máquina
só de workers (`docker-compose.worker.yml`), que sobe muitos workers quando ele está ausente, poucos
quando há folga e nenhum quando ele está usando a máquina para algo pesado. A web é aberta do i9 (ou
de qualquer máquina da rede dele) pelo endereço do Xeon.**

O código do app não muda de forma: ele já separa "a máquina principal" das "máquinas de workers"
(`docker-compose.yml` e `docker-compose.worker.yml`, `candle_sync` desde 09/10). O que muda é **qual
máquina é a principal**, e o que falta para essa troca ser segura:

1. **Um worker pode ser parado sem perder runs.** Hoje um worker que recebe SIGTERM no meio de um
   lote marca os runs que faltavam como `failed` ("the batch was stopped"), e eles não voltam
   sozinhos. Isso era raro: um worker só parava num rebuild. No i9 os workers vão subir e descer
   várias vezes por dia, então **parar passa a ser: terminar o trabalho em andamento, não pegar o
   próximo e sair**. O Docker espera esse tempo antes de matar o processo (`stop_grace_period`).
2. **O servidor se religa sozinho.** Depois de um reinício do Xeon, um script só (`tradeforge.ps1
   up`, chamado no logon) sobe o Docker, os terminais MT5, um agente por corretora, o supervisor dos
   sinais e o notificador. Isso substitui o `tradeforge-sinais.ps1` do Desktop e o agente iniciado à
   mão.
3. **A memória do Postgres segue a máquina.** Os 6 GB de `shared_buffers` foram medidos para os 44 GB
   da VM do i9; no Xeon, com 32 GB para tudo, eles viram variável do `.env`, e o padrão continua o de
   hoje.
4. **O i9 decide sozinho quantos workers sobe**, olhando o tempo sem teclado e mouse, a CPU usada por
   outros programas e a RAM livre, com limites que ele escolhe.

### O que fica onde depois da virada

| Xeon (servidor, sempre ligado) | i9 (trabalho dele) | Outras máquinas (notebook) |
|---|---|---|
| Postgres, Redis, API, web | Workers: 0 a 16, conforme o uso | Workers, quando ele liberar |
| Terminais MT5 e um agente de coleta por corretora | Cópia dos candles (`candle-sync`, como os outros workers) | Cópia dos candles |
| Supervisor dos sinais e notificador | Tarefas pesadas e ML, lendo o banco do Xeon pela rede | |
| Poucos workers (4 com 32 GB; mais com 64 GB) | O navegador com a web do Xeon | |
| Os candles "oficiais" (`data/ohlcv`) e a base de ML | | |

## Alternativas consideradas

| Alternativa | Prós | Contras |
|---|---|---|
| **Deixar tudo no i9 e só religar sozinho depois do reinício** | Nenhuma migração | Não resolve a disputa com as tarefas pesadas nem o reinício no meio do dia; o servidor continua na máquina que mais muda |
| **Banco no Xeon, sinais e MT5 no i9** | Migração menor (os terminais ficam onde estão) | Os sinais, que são o que tem que ficar no ar, continuam presos à máquina que reinicia |
| **Servidor numa VPS ou na nuvem** | Fora de casa: sem queda de luz nem de internet de casa | Custo mensal; 81 GB de banco crescendo; MT5 exige Windows (VPS mais cara); os workers de casa passariam a escrever pela internet; a experiência com a Contabo (quedas de madrugada) |
| **Xeon com Linux nativo (sem WSL)** | Sem teto de memória da VM; Docker mais leve | O MT5 é só Windows: coleta e sinais precisariam de Wine ou de uma VM Windows; reinstalar a máquina. Pode voltar a ser discutido depois |
| **Xeon como servidor, i9 só workers (escolhida)** | A máquina que fica ligada passa a ser a que serve; o i9 fica livre para ele; as peças já existem (`worker.yml`, `candle_sync`) | O Xeon tem 32 GB para tudo até a RAM nova chegar; virada de algumas horas com o sistema parado; os workers do i9 dependem da rede |

## Trade-off aceito

- **Menos workers no Xeon** enquanto ele tiver 32 GB: o servidor (Postgres, API, Redis, web, cerca
  de 9 GB na VM), os terminais e os sinais no Windows (cerca de 4 GB) vêm antes. A conta é a mesma
  de sempre: workers × 3 GB + o resto < teto do WSL (`.wslconfig`). Os workers que saem de lá voltam
  em dobro no i9 quando ele não está usando a máquina.
- **O Xeon passa a ser o ponto único de falha.** Isso já era verdade com o i9, mas agora o servidor
  fica na máquina que ele não usa no dia a dia, com reinício automático controlado (as mesmas
  políticas do Windows Update do i9) e religamento sozinho depois de qualquer reinício.
- **Uma janela de algumas horas com tudo parado** no dia da virada (cópia de 81 GB de banco). Ela vai
  para um fim de semana, com a B3 fechada, e a fila das varreduras atravessa intacta: a cópia do
  Redis leva os jobs, e os workers devolvem os lotes ao parar.
- **Os núcleos do Xeon são mais lentos** (um lote leva cerca de 3× o tempo do i9, `WorkerSettings`).
  Para o servidor isso não pesa: API, banco e sinais passam a maior parte do tempo esperando.

## Consequências

**Código (cinco PRs depois do aceite):**

1. `fix(repo)`: a memória do Postgres (`shared_buffers`, `effective_cache_size`) vem do `.env`, com o
   padrão de hoje; os workers ganham um `stop_grace_period` do tamanho de um lote.
2. `feat(api)`: o worker que recebe SIGTERM termina o job em andamento, não pega outro e sai. Nenhum
   run fica `failed` por causa de uma parada; o que não terminou volta para a fila.
3. `feat(repo)`: `tradeforge.ps1 up` sobe o servidor inteiro (Docker, terminais MT5, um agente por
   corretora, sinais e notificador) e `tradeforge.ps1 autostart` registra isso no logon do Windows.
4. `feat(repo)`: `tradeforge.ps1 workers <n>|auto` para máquinas só de workers, com o modo automático
   do i9 (ausente, folga, ocupado) e os limites no `worker.env`.
5. `docs`: `worker-remoto.md` e README passam a chamar o Xeon de principal; o roteiro da virada fica
   em `docs/referencia/servidor-xeon.md`.

**Operação:**

- O `.env` do i9 aponta para o Xeon (`POSTGRES_HOST`, `REDIS_HOST`) para o que ainda roda direto no
  Windows (replay e treino de ML, scripts).
- Depois de uma coleta, ninguém mais precisa copiar candles: a coleta acontece no Xeon, e todo worker
  (inclusive o i9) recebe pelo `candle-sync`.
- Os volumes antigos do i9 ficam guardados, sem uso, até ele mandar apagar: são o caminho de volta se
  a virada der errado.
- Antes de qualquer conta real, as mesmas regras de hoje valem no Xeon (AGENTS.md §5.7): o terminal
  da ActivTrades é uma conta real sem saldo e nunca recebe ordem.
