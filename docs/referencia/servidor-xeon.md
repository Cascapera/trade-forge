# O servidor no Xeon e o i9 só com workers — plano e roteiro da virada

A decisão e o porquê estão no [ADR-0036](../adr/0036-o-servidor-fica-no-xeon-e-o-i9-so-empresta-a-folga.md).
Aqui fica **o que fazer, em que ordem, e como conferir cada passo**. Endereços e senhas desta casa não
entram aqui (o repositório é público): ficam no `.env`, no `worker.env` e nas notas locais.

Nomes usados abaixo:

- **Xeon** = a máquina que vira servidor. `<XEON>` é o endereço dela na VPN (Tailscale), que não muda
  com o DHCP do roteador.
- **i9** = a máquina de trabalho dele, que passa a rodar só workers.

## Fases

| Fase | O quê | Onde | Quem |
|---|---|---|---|
| 0 | ADR-0036 e este plano | repo | Claude |
| 1 | Os cinco PRs (abaixo), mergeados e no ar no i9 como hoje | repo / i9 | Claude |
| 2 | Preparar o Xeon (pré-requisitos), sem ligar nada do servidor | Xeon | ele + Claude no Xeon |
| 3 | Ensaio: subir o servidor no Xeon com uma cópia do banco, sem virar | Xeon | Claude no Xeon |
| 4 | O dia da virada (fim de semana, B3 fechada) | os dois | ele + Claude |
| 5 | i9 só com workers no modo automático; uma semana de observação | i9 | Claude |
| 6 | Apagar os volumes antigos do i9 (com backup), quando ele mandar | i9 | Claude, com o ok dele |

## Fase 1 — os PRs

Cada um sai sozinho, com testes e lição, e já vale no i9 antes da virada (nada deles depende do Xeon).

### PR 1 — `fix(repo)`: memória do Postgres pelo `.env`; workers esperam o lote ao parar

- `shared_buffers` e `effective_cache_size` vêm de `POSTGRES_SHARED_BUFFERS` e
  `POSTGRES_EFFECTIVE_CACHE_SIZE`, com os valores de hoje como padrão (6GB/20GB). O Xeon com 32 GB
  usa algo como 3GB/10GB.
- Workers (nos dois composes) com `stop_grace_period` maior que o lote mais longo, para o Docker não
  matar o processo enquanto o PR 2 o deixa terminar.
- **Aceite:** `docker compose config` mostra os valores do `.env` e os padrões sem ele; nada muda no
  i9 enquanto o `.env` não tiver as variáveis.

### PR 2 — `feat(api)`: um worker parado termina o que está fazendo e sai

- SIGTERM durante um job: o worker para de pegar jobs, deixa o job em andamento terminar e só então
  sai. Hoje o arq cancela o job, e os runs do lote que ainda não tinham sido gravados viram `failed`
  ("the batch was stopped").
- Um segundo SIGTERM, ou o fim do `stop_grace_period`, continua matando na hora; nesse caso o que não
  foi gravado **volta para a fila** em vez de virar `failed`.
- **Aceite:** teste com um lote de verdade: SIGTERM no meio, o lote termina, todos os runs `done`, o
  worker sai com código 0; teste do segundo sinal: os runs não gravados voltam a `queued` e o job é
  devolvido ao arq.

### PR 3 — `feat(repo)`: `tradeforge.ps1 up` sobe o servidor inteiro, e se religa sozinho

- `up` passa a fazer, em ordem: Docker Desktop → `docker compose up -d` → espera Redis e API →
  abre os terminais MT5 das corretoras cadastradas (caminho de cada uma na tabela `brokers`) e espera
  cada um conectar → um agente de coleta por corretora (`agent --broker <slug>`) → notificador e
  supervisor dos sinais (o que o `tradeforge-sinais.ps1` do Desktop faz hoje).
- `down` para tudo isso na ordem inversa; `status` mostra cada peça.
- `autostart` registra uma tarefa agendada no logon do Windows que roda `up` (e `autostart -Remove`
  tira). Funciona com o logon automático do Windows no Xeon (pré-requisito da fase 2).
- Caminhos relativos ao próprio script (`$PSScriptRoot`): o repositório pode estar em qualquer pasta.
- **Aceite:** no i9, com o PC recém-ligado e nada aberto, `up` deixa tudo no ar e `status` confirma;
  rodar `up` de novo não duplica nada.

### PR 4 — `feat(repo)`: máquinas só de workers e o modo automático do i9

- `tradeforge.ps1 workers <n>` (em máquina com `worker.env`): sobe ou desce para `n` workers do
  `docker-compose.worker.yml`, sem recriar os que estão rodando; descer usa a parada do PR 2.
- `tradeforge.ps1 workers auto`: a cada minuto escolhe um de três níveis, com os números no
  `worker.env`:

  | Nível | Quando | Workers (sugestão para o i9) |
  |---|---|---|
  | ausente | sem teclado e mouse há ≥ 10 min | 16 |
  | folga | em uso, outros programas usando < 35% da CPU nos últimos 3 min e ≥ 12 GB livres | 6 |
  | ocupado | outros programas ≥ 35% da CPU por 2 min, ou < 8 GB livres | 0 |

  Sobe devagar (a condição tem de durar alguns minutos) e desce na hora. `workers <n>` à mão
  desliga o automático até o próximo `workers auto`. "Outros programas" = a CPU do Windows menos a da
  VM do Docker, que no i9 passa a ser só dos workers.
- `workers auto -Install` registra o modo automático como tarefa no logon.
- **Aceite:** com a fila cheia, mexer no PC derruba para o nível certo sem nenhum run `failed`;
  largar o PC sobe de volta.

### PR 5 — `docs`: a principal passa a ser o Xeon

- `worker-remoto.md`: "a principal" é o Xeon; o i9 entra como máquina de workers; some o `robocopy`
  manual (o `candle-sync` já cuida).
- README: como abrir a web de outra máquina da rede.

## Fase 2 — preparar o Xeon (antes da virada, sem desligar nada)

| Item | Por quê | Como conferir |
|---|---|---|
| **RAM**: 64 GB, ou 32 GB com 4 workers | Servidor + terminais + sinais vêm antes dos workers | Gerenciador de Tarefas |
| **Disco**: SSD com ≥ 300 GB livres onde fica a VM do Docker | Banco de 81 GB crescendo (era 11 GB em 04/10), mais o dump na virada | `Get-PSDrive` |
| `.wslconfig` | 32 GB: `memory=22GB`; 64 GB: `memory=48GB` | `wsl -d docker-desktop free -g` |
| **Logon automático** do Windows | Docker Desktop e MT5 precisam de uma sessão aberta | Reiniciar e ver o desktop aparecer sozinho |
| **Energia**: nunca suspender, nunca desligar o disco | Servidor | `powercfg /query` |
| **Windows Update**: as políticas do i9 (16/10): sem drivers pelo WU, prazo com "não reiniciar automaticamente", AUOptions=3 | Os dois reinícios de madrugada do i9 | ETL do USO depois da próxima busca |
| **Reserva de DHCP** no roteador (ou só usar o endereço da VPN) | A web e os workers acham o servidor sempre no mesmo lugar | `ipconfig` depois de religar |
| **Clone novo** fora do Desktop/OneDrive (ex.: `C:\dev\trade-forge`), no commit do i9 | O OneDrive não pode sincronizar `data\` | `git log -1` igual ao do i9 |
| `uv sync` (o `.venv` do Windows) | Agentes de coleta e sinais rodam fora do Docker | `.venv\Scripts\python -c "import MetaTrader5"` |
| **Os três terminais MT5** instalados, logados, "salvar senha" marcado | Coleta e sinais | O check do `tradeforge.ps1 status` |
| `.env` copiado à mão do i9 (nunca pelo git) | Senha do banco, tokens do Telegram/Discord | `docker compose config` sem erro |
| **Firewall**, só perfil Privado e a interface da VPN: 5433, 6379, 8000, 5173 | Workers, `candle-sync` e a web de outras máquinas | `Test-NetConnection <XEON> -Port 5173` do i9 |
| `docker compose build` (sem `up`) | A virada não espera build | `docker images` |

## Fase 3 — ensaio

Subir no Xeon um banco restaurado do dump de um dia antes, com outro nome de projeto do compose
(`-p tradeforge-ensaio`), portas diferentes e sem workers, para medir quanto tempo leva o `pg_restore`
e conferir que API, web e sinais (num terminal demo) sobem. Apagar o ensaio depois. É ele que diz
quanto tempo a virada de verdade vai levar.

## Fase 4 — o dia da virada

Num fim de semana. A fila das varreduras atravessa intacta: os workers devolvem os lotes ao parar
(PR 2), e a cópia do Redis leva os jobs. Os sinais armados são cancelados no canal quando o supervisor
para (como em qualquer reinício), então melhor com os mercados fechados.

1. **No i9:** parar sinais e notificador (`tradeforge.ps1 down` só das peças do host).
2. **Parar todos os workers** de todas as máquinas, esperando cada um terminar o lote (PR 2):
   Xeon (`docker compose -f docker-compose.worker.yml --env-file worker.env down`), i9, notebook.
   Conferir no banco: 0 runs `running`.
3. **No i9:** parar API e web (`docker compose stop api web worker`), deixando Postgres e Redis no ar.
4. **Redis:** `docker exec tradeforge-redis redis-cli SAVE` e copiar o `dump.rdb` e a pasta
   `appendonlydir` do volume.
5. **Postgres:** `pg_dump -Fd -j 8` (formato de pasta, em paralelo) para um disco com espaço; anotar
   a contagem de `backtests`, `signals`, `strategies` e `sweeps` para conferir do outro lado.
6. **Parar Postgres e Redis no i9** (`docker compose stop`). **Não apagar os volumes**: são a volta.
7. **Copiar** o dump, os arquivos do Redis, `data\ml` e o que faltar de `data\ohlcv` para o Xeon
   (compartilhamento só leitura + `robocopy`, como os candles).
8. **No Xeon:** `docker compose up -d postgres redis` → restaurar o Redis (parar o redis, copiar os
   arquivos para o volume, subir) → `pg_restore -j 8` → `docker compose up -d` (o `migrate` não tem
   nada a fazer) → `tradeforge.ps1 up` (terminais, agentes, sinais).
9. **Conferir:** as contagens do passo 5; a web aberta do i9 em `http://<XEON>:5173`; a fila com o
   mesmo número de jobs; uma coleta pequena; os mercados dos sinais aquecendo.
10. **No i9:** `worker.env` com `TRADEFORGE_HOST=<XEON>` e `TRADEFORGE_CANDLES` apontando para o
    `data\ohlcv` que já existe aqui; `.env` com `POSTGRES_HOST` e `REDIS_HOST` = `<XEON>`;
    `tradeforge.ps1 workers auto -Install`.
11. **Outras máquinas de workers:** `worker.env` com o endereço do Xeon.

**Voltar atrás** (até o passo 10): parar o que subiu no Xeon e `tradeforge.ps1 up` no i9. Os volumes
do i9 estão como foram deixados no passo 6; o que o Xeon gravou depois disso se perde.

## Riscos e o que observar

- **RAM do Xeon com 32 GB.** O limite de 5 GB por worker (#467) mata um worker por vez em vez de
  travar a VM, mas o Windows também precisa de folga para os terminais e os sinais (~4 GB). Começar
  com 4 workers e subir olhando a memória.
- **O banco cresce rápido** (11 GB em 04/10, 81 GB em 16/10). A regra de exportar para a base de ML e
  limpar perto de 200 GB continua; o disco do Xeon decide quando.
- **Um reinício do Xeon** passa a derrubar o servidor: por isso logon automático, `autostart` e as
  políticas do Windows Update entram antes da virada, não depois.
- **O i9 lendo o banco pela rede** (replay de ML, scripts): uma rede de 1 Gbit é suficiente;
  consultas que trazem milhões de linhas ficam mais lentas que no localhost.
- **Um worker com código diferente do servidor** continua sendo o perigo de sempre (ADR-0029): toda
  atualização do servidor atualiza também o i9 e o notebook.
