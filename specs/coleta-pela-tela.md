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

## A janela padrão: orçamento de barras, com piso medido

Decidido em 19/08/2026 a partir de duas medições no broker do Guilherme (`Tradeview-Demo`).

### Medição 1 — barras reais por ano, EURUSD 2024

| tf | barras/ano |
|---|---|
| M1 | 368.083 |
| M5 | 73.705 |
| M15 | 24.582 |
| H1 | 6.150 |
| H4 | 1.541 |
| D1 | 259 |

⚠️ **M1 × 1 ano = 368.083 e M5 × 5 anos = 368.525.** Os dois números que o Guilherme escolheu à
mão são o mesmo número: um **orçamento de barras**, não um limite de calendário. É o critério
certo, porque é barra que vira trade — e generaliza sozinho para timeframes mais lentos, que
precisam de mais tempo de calendário para produzir a mesma quantidade de sinal.

### Medição 2 — o ano em que o spread deixou de ser inventado

Spread que o broker carimbou em cada barra do EURUSD H1:

```
2004: 40  constante o ano inteiro (min == max)   → CARIMBADO
2005: 30  constante                              → CARIMBADO
2006: 20  constante                              → CARIMBADO
2007: 20  constante                              → CARIMBADO
2008: 20  constante                              → CARIMBADO
2009: 20  constante                              → CARIMBADO
2010: 11  varia 8..20                            → medido
2014:  1  varia 0..11                            → medido
```

⚠️ **A fronteira é 2010-01-01, e ela é medida e não escolhida.** Antes disso o broker escreveu um
único número por ano inteiro: não é spread, é preenchimento. Um backtest ali tem custo fictício
mesmo quando o preço não é — e nenhum `CostModel` conserta isso, porque não há o que modelar.

Isso é **independente** do piso de 1999 (nascimento do euro), que separa preço real de série
reconstruída. 2010 separa **custo** real de custo carimbado, e é o mais restritivo dos dois.

### A regra

**Janela padrão = `min(368.500 barras, desde 2010-01-01)`**, por timeframe:

| tf | janela | barras | quem limitou |
|---|---|---|---|
| M1 | 1,0 ano | 368.000 | orçamento |
| M5 | 5,0 anos | 368.500 | orçamento |
| M15 | 15,0 anos | 368.500 | orçamento |
| H1 | 16,6 anos | 102.300 | piso de 2010 |
| H4 | 16,6 anos | 25.600 | piso de 2010 |
| D1 | 16,6 anos | 4.300 | piso de 2010 |

Os dois limites quase se encontram no M15, o que é um bom sinal de coerência entre eles.

⚠️ **Isto é um default que a tela pré-preenche, nunca um limite que a engine impõe.** O operador
sempre pode alargar; o sistema só não escolhe mal por omissão. E a tela mostra a **contagem de
barras** da janela, não só as datas — "5 anos" não diz nada, "7.705 barras" diz.

### O que a regra não conserta

De 2010 a 2026 o spread ainda vai de 11 para 1 ponto. Cobrando um número só na janela inteira, o
erro máximo é ~10 ticks — com a mediana de stop do EURUSD H1 já medida neste projeto (0,00116 =
116 ticks), isso é **~9% de 1R**. Bem melhor que os 40 ticks (~35% de 1R) de antes de 2010, mas
não é zero. O conserto é custo variável no tempo, e está no backlog.

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

  3. **Custo carimbado** — anos em que o `spread` da barra é constante (`min == max`) são anos em
     que o broker inventou o custo. A sonda reporta o primeiro ano medido, que é o piso honesto
     da janela padrão. ⚠️ Sondado **por símbolo**: 2010 é o número do EURUSD deste broker, e uma
     ação americana tem outra história.

**Aceite:** para EURUSD, o M1 aparece marcado como limitado pelo terminal (100.000 barras), o D1
aparece com 1971 como início **bruto** e 1999 como início de preço real, e a janela padrão começa
em 2010 porque antes disso o spread é constante; nenhuma chamada da API espera os 207 s da
sondagem.

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
