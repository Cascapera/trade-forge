# Coleta pela tela — PR-232 a PR-234

**Objetivo:** testar **qualquer** ativo que o MT5 entregue, sem sair da tela. Buscar o símbolo
digitando, saber quanto histórico dá para pegar (e o que está limitando), e disparar a coleta.

**Referência:** `docs/adr/0021-agente-de-coleta-no-host.md`, backlog (origem PR-223, pedido de
06/08/2026), `sdd.md §9` (topologia).

⚠️ **Inserido entre o PR-301 e o PR-302.** A ordem obrigatória da fase 3 vale para o caminho *live*
(paper → kill switch → conta real, `specs/fase-3.md`), e nada aqui toca esse caminho.

---

## A restrição que decide tudo

A API roda em **container Linux**; o `MetaTrader5` é **wheel de Windows** e só é importado por
`apps/collector` e `apps/executor` (ADR-02). A API não fala com o MT5 — hoje nem nunca. Por isso
existe o **agente no host** (ADR-0021), e por isso toda pergunta ao terminal é assíncrona.

## O que foi medido antes de escrever o spec (19/08/2026)

| | |
|---|---|
| `symbols_get(group="A*")` | **0,1 ms**, casa por prefixo, enxerga símbolos ocultos |
| busca binária por posição, H1 | 0,6 ms |
| busca binária por posição, **H4** | **207 s** (o terminal baixa histórico durante a pergunta) |
| `copy_rates_from(1970, …)` | **derruba o processo** (`OSError: Errno 22`) |
| `terminal_info().maxbars` | **100000** — capa M1, M5, M15 e H1 na conta dele |
| EURUSD D1 mais antigo | 1971-01-04, com `o==h==l==c` e `tick_volume=1` |

As duas últimas linhas são o motivo de a sonda de histórico não poder devolver só uma data.

---

## PR-232 — Agente no host + busca de símbolo

**Escopo:**
- Agente no host: `arq tradeforge_collector.agent.WorkerSettings`, fila `collect`, com o
  `MT5Source` já existente por trás. Mesma forma como o worker da API sobe no compose
  (`arq tradeforge_api.worker.WorkerSettings`) — um subcomando próprio seria superfície nova
  para nenhuma capacidade nova.
- Job `sync_symbols`: `mt5.symbols_get()` → tabela nova `broker_symbols` (snapshot do que a
  corretora oferece **hoje**), separada de `instruments`.
- API: `GET /symbols/search?q=<prefixo>&limit=` servindo do snapshot; `POST /symbols/sync`
  enfileirando o job e devolvendo `202`.
- Web: o `<select>` de símbolo do `BacktestSettings` vira **combobox** que consulta ao digitar
  (a partir de 1 letra), com ação "não achei / atualizar do MT5".

**Aceite:** digitar `eur` lista os 18 pares EUR do broker, incluindo os que **não** estão no
Market Watch; trocar de conta no MT5 e sincronizar troca a lista; a busca continua respondendo com
o terminal fechado.

**Você vai aprender:** por que uma fronteira de plataforma vira topologia (e não um `if`), filas
segregadas por *quem pode executar* em vez de por prioridade, e a diferença entre "o que o sistema
conhece" (`instruments`) e "o que a corretora oferece" (`broker_symbols`).

---

## PR-233 — Sonda de histórico

**Escopo:**
- Job `probe_history(symbol, timeframe)`: busca binária **por posição** (nunca `copy_rates_from`
  com data antiga — ver medição), gravando barra mais antiga, contagem, `maxbars` do terminal e o
  instante da sondagem.
- Detecção das duas mentiras:
  1. **Teto do terminal** — `contagem == maxbars` significa que o limite é a configuração dele, não
     o broker. A tela diz isso e diz onde mudar.
  2. **Série reconstruída** — barras com `high == low` e `tick_volume <= 1` não são mercado. A sonda
     reporta a partir de onde a série vira negociação de verdade.
- API: `GET /symbols/{symbol}/history?timeframe=` (o que já foi sondado) e `POST .../probe` (202).
- Web: ao escolher o símbolo, mostrar o span **utilizável** e o que o está limitando.

**Aceite:** para EURUSD, o M1 aparece marcado como limitado pelo terminal (100.000 barras) e o D1
aparece com 1971 como início **bruto** e 1999 como início utilizável; nenhuma chamada da API espera
os 207 s da sondagem.

**Você vai aprender:** que "mais histórico" e "mais validação" não são a mesma coisa — 28 anos de
barras sem range fazem o backtest parecer melhor e ser menos validado; e como reportar uma medição
junto com o que a limitou.

---

## PR-234 — Coletar pela tela

**Escopo:**
- Job `collect_range(symbol, timeframe, start, end)`: reusa o `backfill()` que já existe, escreve
  Parquet no host e cataloga `instruments` + `datasets`.
- API: `POST /collections` (202) + `GET /collections/{id}` para acompanhar; progresso pelo mesmo
  caminho de pub/sub dos backtests.
- Web: escolher símbolo/timeframe/período (pré-preenchido pelo que a sonda do PR-233 disse ser
  utilizável) e acompanhar.

**Aceite:** escolher um símbolo que **nunca** foi coletado, coletar pela tela e rodar um backtest
nele sem tocar na CLI.

**Você vai aprender:** idempotência de coleta (recoletar um range já coletado atualiza a linha em
vez de duplicá-la), e por que o caminho de escrita do Parquet mora no host.

---

## Fora de escopo, anotado

- **Dois terminais instalados**: `mt5_source.py` chama `mt5.initialize()` sem argumento, então não
  há como escolher qual terminal. Precisa de `--terminal-path` antes de "trocar de corretora" virar
  operação de tela. (Backlog, origem PR-223.)
- **Supervisão do agente**: ele morre com o terminal que o iniciou, exatamente como o `live`
  (backlog, origem PR-301-A). O mesmo lugar que resolver um resolve o outro.
