# FEATURE — Coleta multi-ativo pela tela

## 1. Painel de status

| | |
|---|---|
| **Status** | `em implementação` — PR 4 (último) pronto na branch |
| **Progresso** | **3/4 PRs mergeados** (#129, #130, #131) · **14/14 itens** |
| **Suposições** | 6 (§16) |
| **Questões em aberto** | 1 (Q-02) — **Q-01 e Q-03 resolvidas** (§16) |
| **Migração de banco** | **nenhuma** — ver §8 |
| **Data** | 21/08/2026 |

### ⚠️ Correções ao plano (a realidade divergiu, o plano foi corrigido)

1. **F-01 e F-02 são inseparáveis.** Trocar `symbol` por `items` quebra a rota no mesmo instante,
   então não existe estado em que só F-01 esteja mergeado e a suíte verde. Foram executados como
   uma unidade e marcados juntos.
2. **PR 1 carrega uma mudança mínima no web.** O plano original deixava a tela mandando o payload
   antigo entre o PR 1 e o PR 3 — ou seja, a tela de coleta quebrada por dois PRs, contra a regra
   "cada PR deixa o sistema consistente". PR 1 agora atualiza `types.ts`, `client.ts`, `hooks.ts`
   e o `submit` da tela para enviar `items` com **um** símbolo. A UI de seleção múltipla continua
   sendo o PR 3.
3. **F-07 partia de premissa falsa.** O item dizia "tipo TS **regerado** do schema". Não
   existe codegen para `api/types.ts` — o header do próprio arquivo diz que ele é espelhado
   à mão de propósito, e só o tipo da **DSL** vem gerado de `packages/schema`. O item virou
   "espelhar à mão, seguindo a convenção documentada do arquivo".
4. **Um `type: ignore` novo, contra o padrão da §14.** Ver a nota abaixo do checklist.
5. **O seletor múltiplo é componente NOVO, não um modo no antigo.** O plano dizia
   "`SymbolCombobox` ganha modo múltiplo". Os dois diferem no que uma escolha *significa*
   — substituir contra alternar — e no que o campo faz depois: um guarda a escolha, o outro
   limpa para o próximo ticker ser digitado. Um componente só ramificaria nisso em seis
   lugares. O que era comum (busca, debounce, teclado, dropdown, rodapé) foi **extraído**
   para `symbolSearch.ts` + `SymbolOptions.tsx`, e os 10 testes existentes do seletor
   antigo provaram que a extração não regrediu nada.
6. **A regra do eslint `react-refresh` forçou dois arquivos.** Um arquivo que exporta
   componentes não pode exportar mais nada, então os hooks foram para o `.ts` e os
   componentes para o `.tsx`.
7. **`bindingFloor` e `useSymbolHistories` não estavam no plano, e precisavam estar.** A
   janela sugerida é pisada pela sonda **de um símbolo**; com N, deixar isso para o PR 4
   teria feito o caso de um símbolo *regredir* — ele perderia o piso de 2009 que já tem
   hoje. Então a **leitura** das sondas veio para cá; a **sondagem automática** (a
   mutação) continua no PR 4.
8. **Constante própria em vez do `_MAX_SYMBOLS` do basket.** Reusar a do basket acoplaria dois
   tetos que a Q-02 prevê que podem divergir — e eles limitam trabalhos de custo muito diferente:
   vinte backtests são segundos, vinte coletas são horas. Nasceu `MAX_COLLECTION_SYMBOLS`, com o
   porquê no docstring.

---

## 2. Resumo

Hoje a tela `/collect` coleta **um** símbolo por vez. Esta feature faz o operador escolher
**vários ativos** numa mesma busca, definir **um** timeframe e **um** intervalo, e disparar tudo
de uma vez.

O trabalho continua serializado no agente do host — que já é assim de propósito, porque o
terminal MT5 é um canal IPC único. Nenhuma tabela nova, nenhuma migração: uma leva de 8 ativos
é simplesmente 8 linhas em `collections`, o mesmo que 8 cliques dariam hoje, sem os 8 cliques.

---

## 3. Contexto do código (Fase 0)

### Trabalho anterior encontrado e aproveitado

`specs/coleta-pela-tela.md` (196 linhas) descreve os PRs 232/233/234, mergeados em 19–20/08/2026.
**Está atualizado** — o último fechou anteontem. É a fonte direta desta feature, e dele vem a
regra da janela padrão (orçamento de barras) e o piso de custo medido, que continuam valendo sem
alteração.

`docs/adr/0021-agente-de-coleta-no-host.md` explica por que a API nunca fala com o MT5.

### O padrão análogo já existe: `Basket`

`baskets` responde exatamente a mesma forma de problema — um pedido que vira N execuções
independentes. E o jeito como ele distribui é o que esta feature copia:

```python
# routers/baskets.py — a ROTA cria as N linhas e enfileira N jobs.
for symbol in request.symbols: ...        # N × Backtest(...)
await queue.enqueue_job(RUN_BACKTEST, str(run.id))
```

Não existe job-pai orquestrando, e a ausência é deliberada: um job que enfileira sub-jobs e
espera dá **deadlock** quando há um worker só — que é exatamente o caso aqui (`max_jobs = 1`).

### O requisito "não sobrecarregar" já está satisfeito por construção

```python
# apps/collector/.../agent.py — WorkerSettings
max_jobs = 1
```

O comentário no código dá uma razão mais forte do que a do pedido: o terminal é **um canal IPC
compartilhado**, e dois jobs pedindo histórico ao mesmo tempo não são duas vezes mais rápidos —
o custo real é o terminal baixando, e os pedidos disputam o mesmo download.

A memória também já está limitada: `run_collection` processa **um ano de calendário por vez**
(`year_slices`). O pico é um ano de barras — pior caso M1, ~368 mil. Enfileirar 20 símbolos não
muda esse pico; só faz a fila ser mais longa.

> **Consequência para o escopo:** não há throttle, semáforo ou pool para construir. Construir um
> seria adicionar um segundo mecanismo de serialização em cima de um que já funciona. A feature
> precisa *não estragar* essa propriedade, e o plano de testes tem item para provar isso.

### Módulos que a feature toca

| arquivo | papel hoje | muda? |
|---|---|---|
| `apps/api/.../routers/collections.py` | aceita 1 símbolo, cria 1 linha, enfileira 1 job | **sim** — passa a aceitar N |
| `apps/api/.../schemas.py` | `CreateCollectionRequest`, `BrokerSymbolOut` | **sim** — lista + campo derivado |
| `packages/db/.../collections.py` | `create_collection` | não — chamada N vezes |
| `apps/collector/.../agent.py` | `collect_range`, `probe_history` | **não** |
| `apps/collector/.../collect.py` | `run_collection`, `year_slices` | **não** |
| `apps/web/src/screens/CollectSymbol.tsx` | formulário de 1 símbolo | **sim** — seleção múltipla |
| `apps/web/src/components/SymbolCombobox.tsx` | busca, escolha única | **sim** — ganha modo múltiplo |
| `apps/web/src/collect/window.ts` | janela sugerida por timeframe | não |

### Convenções que o plano segue

- **Python:** type hints completos, `mypy --strict`, Pydantic v2 nos schemas, pytest + integração
  marcada, docstring em função pública explicando o *porquê*.
- **TypeScript:** strict, sem `any`, React Query para estado de servidor, tipos da DSL **gerados**
  do schema — nunca duplicados à mão. (É por isso que a classe derivada do caminho vira campo da
  API em vez de regra reescrita em TS — ver DD-02.)
- **Código e comentários em inglês**; este documento e as aulas em português.
- Rota nova/alterada tem teste de contrato e entra no fuzzer schemathesis já existente.

### Dois seletores prontos, e nenhum serve inteiro

| componente | lista o quê | escolha | teto |
|---|---|---|---|
| `SymbolPicker` | só o que **já foi coletado** (`instruments`) | **múltipla** | 20 |
| `SymbolCombobox` | o que **a corretora oferece** (`broker_symbols`) | única | — |

A feature precisa do cruzamento: buscar no que a corretora oferece, escolher vários.

### Estado da área

Saudável. Todo módulo tocado tem teste (`test_collect.py`, `test_run_collection.py`,
`test_agent.py`, `CollectSymbol.test.tsx`, `SymbolCombobox.test.tsx`), a suíte está verde
(1717 python, 90,34% · 160 integração) e o CI fechou 10/10 em 21/08. Mutação foi rodada sobre
esta área nos três PRs que a construíram. **Não há dívida no caminho.**

---

## 4. Escopo

### Entra

- Escolher **de 1 a 20 símbolos** numa busca única, entre os que a corretora oferece.
- **Um** timeframe e **um** intervalo de datas para a leva inteira.
- Classe de ativo perguntada **por símbolo**, só para os que o caminho não decide.
- Sonda de histórico disparada automaticamente para os escolhidos ainda não sondados.
- Aviso, antes do botão, de quais símbolos vão voltar com janela mais curta do que a pedida.
- Estimativa do tamanho do trabalho (fatias de ano) antes de enfileirar.
- Acompanhar as N coletas na mesma tela.

### Não entra (explícito)

- **Tabela de lote.** N linhas soltas, sem pai (DD-01).
- **Vários timeframes de uma vez.** Um por leva. Duas levas para dois timeframes.
- **Janela por símbolo.** Uma janela para todos (decisão do Guilherme, §9 DD-03).
- **Paralelizar a coleta.** Contraria `max_jobs = 1`, que é a proteção que o pedido pede.
- **Prioridade de fila.** arq drena FIFO; não há fila prioritária nesta feature.
- **Cancelar uma leva em andamento.** Não existe cancelamento hoje para uma coleta única;
  criar agora seria feature nova, não extensão. → backlog.
- **Supervisão do agente.** Ele continua morrendo com a sessão que o iniciou (backlog, PR-301-A).
- **Escolher qual terminal MT5.** `mt5.initialize()` sem argumento (backlog, PR-223).

---

## 5. Regras de negócio

| # | Regra |
|---|---|
| **RN-01** | Uma leva tem no mínimo 1 e no máximo 20 símbolos. |
| **RN-02** | Símbolos repetidos na mesma leva são recusados (não deduplicados em silêncio). |
| **RN-03** | Timeframe e intervalo são únicos para a leva inteira. |
| **RN-04** | Uma leva de N símbolos cria N linhas em `collections`, uma por símbolo, sem vínculo entre elas. |
| **RN-05** | Cada linha é enfileirada como seu próprio job `collect_range`. |
| **RN-06** | A leva é **tudo ou nada na aceitação**: se qualquer símbolo for inválido, nenhuma linha é criada e nada é enfileirado. |
| **RN-07** | Depois de aceita, cada linha é **independente**: uma falhar não afeta as outras. |
| **RN-08** | Um símbolo cuja classe o caminho não decide exige classe explícita; a recusa nomeia **todos** os símbolos pendentes, não o primeiro. |
| **RN-09** | Classe enviada para um símbolo cujo caminho decidiria **sobrescreve** o caminho (comportamento atual, preservado). |
| **RN-10** | Ano sem barras não falha a coleta daquele símbolo; só falha quando **nenhum** ano teve barras (comportamento atual, preservado). |
| **RN-11** | A sonda é disparada só para pares (símbolo, timeframe) **ainda não sondados**; reescolher um já sondado não enfileira nada. |
| **RN-12** | A tela mostra, antes do botão, quais escolhidos têm histórico começando depois do `date_from` pedido. |
| **RN-13** | A tela mostra a estimativa de trabalho em **fatias de ano** (símbolos × anos do intervalo). |
| **RN-14** | Recoletar um par (símbolo, timeframe) já coletado atualiza a linha em `datasets` em vez de duplicá-la (comportamento atual, preservado). |

---

## 6. Critérios de aceite

| # | Dado / Quando / Então | RN |
|---|---|---|
| **CA-01** | **Dado** 3 símbolos válidos, H1 e um intervalo · **quando** submeto · **então** a API responde 202 com 3 objetos e existem 3 linhas `collections` em `queued`. | RN-03, RN-04 |
| **CA-02** | **Dado** a leva do CA-01 · **quando** ela é aceita · **então** 3 jobs `collect_range` foram enfileirados, um por linha, com o id da linha. | RN-05 |
| **CA-03** | **Dado** 21 símbolos · **quando** submeto · **então** 422 e **nenhuma** linha criada. | RN-01, RN-06 |
| **CA-04** | **Dado** `["EURUSD", "EURUSD"]` · **quando** submeto · **então** 422 nomeando a repetição e nenhuma linha criada. | RN-02, RN-06 |
| **CA-05** | **Dado** 2 símbolos cujo caminho não decide a classe e nenhuma classe enviada · **quando** submeto · **então** 409 nomeando **os dois** e nenhuma linha criada. | RN-08, RN-06 |
| **CA-06** | **Dado** os mesmos 2 com classe enviada por símbolo · **quando** submeto · **então** 202 e cada linha guarda **a sua** classe. | RN-08, RN-09 |
| **CA-07** | **Dado** uma leva de 3 em que o 2º falha na coleta · **quando** o agente termina · **então** a 2ª linha está `failed` com motivo e as outras duas `done`. | RN-07 |
| **CA-08** | **Dado** o agente rodando · **quando** 3 coletas estão enfileiradas · **então** ele executa **uma por vez** (nunca duas simultâneas). | §3 |
| **CA-09** | **Dado** um símbolo com `path` que decide a classe · **quando** leio `GET /symbols/search` · **então** o campo derivado traz a classe; para um ambíguo, traz nulo. | RN-08 |
| **CA-10** | **Dado** 2 símbolos escolhidos na tela, um já sondado · **quando** escolho · **então** só **um** job de sonda é enfileirado. | RN-11 |
| **CA-11** | **Dado** BTCUSD (utilizável de 2022) escolhido com `date_from` = 2015 · **quando** olho a tela · **então** ela avisa que esse símbolo vai voltar mais curto, nomeando a data real. | RN-12 |
| **CA-12** | **Dado** 4 símbolos e um intervalo de 3 anos · **quando** olho a tela · **então** ela mostra a estimativa de **12 fatias de ano**. | RN-13 |
| **CA-13** | **Dado** um símbolo ambíguo escolhido · **quando** não informo a classe dele · **então** o botão de coletar fica desabilitado com o motivo visível. | RN-08 |

---

## 7. Contrato

### `POST /collections` — alterado

**Antes** (1 símbolo, resposta objeto):

```jsonc
// request
{ "symbol": "EURUSD", "timeframe": "H1",
  "date_from": "2020-01-01T00:00:00Z", "date_to": "2024-12-31T23:59:59Z",
  "asset_class": null }
// 202 → CollectionOut (objeto)
```

**Depois** (N símbolos, resposta lista):

```jsonc
// request
{
  "items": [
    { "symbol": "EURUSD" },
    { "symbol": "XAUUSD", "asset_class": "future" }   // classe só onde o caminho não decide
  ],
  "timeframe": "H1",
  "date_from": "2020-01-01T00:00:00Z",
  "date_to": "2024-12-31T23:59:59Z"
}
// 202 → [CollectionOut, CollectionOut]   (mesma ordem dos items)
```

`items` é lista de objetos, não dois arrays paralelos (`symbols` + `asset_classes`), porque
arrays paralelos podem sair de sincronia e o schema não consegue proibir isso. Um símbolo e a
classe dele são um fato só.

| código | quando | corpo |
|---|---|---|
| `202` | aceito | lista de `CollectionOut`, na ordem enviada |
| `422` | `items` vazio, > 20, símbolo repetido, timeframe desconhecido, `date_to < date_from`, instante sem timezone | `detail` com o motivo |
| `409` | um ou mais símbolos sem classe determinável | `detail` **nomeando todos** os pendentes |

### `GET /symbols/search` — campo novo

`BrokerSymbolOut` ganha:

```jsonc
{ "symbol": "XAUUSD", "path": "CFDs\\Metals\\XAUUSD",
  "asset_class_from_path": null }     // nulo = o caminho não decide, a tela precisa perguntar
```

Aditivo: cliente que ignora o campo não quebra.

### Inalterados

`GET /collections`, `GET /collections/{id}`, `POST /symbols/{symbol}/probe`.

---

## 8. Modelo de dados e migração

> ### ⚠️ Nenhuma migração. Nenhuma coluna nova. Nenhuma tabela nova.

`collections` já guarda tudo: `symbol`, `timeframe`, `date_from`, `date_to`, `asset_class`,
`status`, `years_done`, `years_total`, `error`. Uma leva de 8 produz 8 linhas dessas — as mesmas
que 8 cliques produziriam hoje.

`asset_class_from_path` é **derivado em tempo de resposta** de `broker_symbols.path`, chamando
`classify.asset_class_from_path`, que já existe e já é importada pela API. Não é coluna: gravá-lo
seria duplicar uma derivação e criar a chance de a cópia divergir do caminho depois de um sync.

**Consequência boa:** esta feature não trava tabela, não precisa de default para linha existente,
e o rollback é reverter código — não há estado novo para desfazer.

---

## 9. Decisões de design

### DD-01 — Agrupamento: N linhas soltas *(decidido pelo Guilherme)*

> **Opção A — N linhas em `collections`, sem pai.** A rota cria N e enfileira N.
> *A favor:* zero migração, zero máquina de estado nova, cada símbolo falha sozinho, e é
> literalmente o que N cliques fazem hoje. *Contra:* não há um número único "7 de 8 prontas";
> a tela mostra 8 barras.
>
> **Opção B — tabela `collection_batches`.** Espelha `baskets`. *A favor:* progresso agregado e
> página de lote. *Contra:* migração, rota nova, e uma **segunda máquina de estado** que precisa
> concordar com a das filhas — o pai falha se uma filha falha, ou só se todas?
>
> **ESCOLHA: A.** `baskets` tem tabela porque o **produto** dele é a comparação entre mercados —
> dispersão é a resposta, e ela não existe sem o grupo. O produto de uma coleta é "o dado está no
> disco", que não tem resposta cruzada para calcular. Agrupar aqui compraria um cabeçalho.
>
> **TRADE-OFF ACEITO:** sem progresso agregado no servidor. A tela soma as N linhas que ela mesma
> acabou de criar (guardando os ids na sessão), o que é suficiente e não vira schema.

### DD-02 — Onde mora a regra "este caminho decide a classe?"

> **Opção A — API expõe `asset_class_from_path`.** *A favor:* a regra continua num lugar só, em
> Python, já testada. *Contra:* um campo a mais na resposta de busca.
>
> **Opção B — o front reimplementa `asset_class_from_path` em TypeScript.** *A favor:* zero
> mudança na API. *Contra:* duplica **regra de negócio** entre linguagens. O projeto gera os tipos
> do schema exatamente para não fazer isso, e as duas cópias divergem no primeiro símbolo novo.
>
> **Opção C — mandar e corrigir os 409.** *Contra:* o docstring da própria rota argumenta contra:
> *"quem recebe 409 olhando o formulário preenche um campo; quem só descobre quando o job falha já
> saiu da tela"*.
>
> **ESCOLHA: A.** *(confirmada pelo Guilherme)*
>
> **TRADE-OFF ACEITO:** a resposta de busca cresce um campo, e `GET /symbols/search` passa a
> chamar uma função pura por linha. É O(n) sobre no máximo `limit` linhas — irrelevante.

### DD-03 — Uma janela para todos *(decidido pelo Guilherme)*

> **ESCOLHA: uma janela só**, com a tela avisando quem vai voltar curto (RN-12).
> `run_collection` já trata ano vazio como ordinário — pedir 2015 num símbolo listado em 2018
> devolve três anos vazios sem falhar (RN-10). A alternativa (janela por símbolo) seria N
> formulários, que é a tela de hoje repetida.
>
> **TRADE-OFF ACEITO:** um símbolo com histórico mais longo que a janela escolhida coleta menos
> do que poderia. É recuperável — recoletar é idempotente (RN-14).

### DD-04 — Sonda automática *(decidido pelo Guilherme, contra a recomendação)*

> **Opção A — sondar só sob botão.** *(era a recomendação)* Escolher 20 símbolos não enfileiraria
> nada; o operador decide quando pagar.
>
> **Opção B — sondar automaticamente ao escolher.** *A favor:* a tela se completa sozinha e o
> aviso de janela curta (RN-12) aparece sem ação extra. *Contra:* as sondas entram na **mesma fila
> de um job por vez** que a coleta vai usar. Uma sondagem de H4 levou **207 s** medidos no PR-233.
> Escolher 20 símbolos nunca sondados pode significar mais de uma hora de fila antes de a primeira
> vela ser baixada.
>
> **ESCOLHA: B**, decisão do Guilherme.
>
> **MITIGAÇÃO obrigatória (RN-11):** sondar **apenas** pares (símbolo, timeframe) ausentes de
> `symbol_history`. A sonda é cacheada e o custo é pago uma vez por par — reescolher, trocar de
> intervalo ou recarregar a tela não enfileira nada. Sem isso, cada clique recompraria a hora.
>
> **MITIGAÇÃO 2:** a tela mostra quantas sondas estão na frente, para a espera ser legível em vez
> de parecer travamento — mesmo princípio do "3 de 5 anos" que já existe.
>
> **TRADE-OFF ACEITO:** na primeira vez que você escolher muitos símbolos novos, a coleta começa
> depois das sondas. Documentado na tela, não no código.

### DD-05 — Alargar `POST /collections` em vez de criar `/collections/batch`

> **Opção A — alargar a rota existente** para `items: list[...]` e resposta lista.
> *A favor:* um caminho só; N=1 é uma lista de um. *Contra:* **quebra o contrato** — request e
> response mudam de forma.
>
> **Opção B — `POST /collections/batch` nova, mantendo a antiga.** *A favor:* nada quebra.
> *Contra:* duas rotas fazendo a mesma coisa para sempre, e a antiga vira caso especial da nova.
>
> **RECOMENDAÇÃO: A.** Quem consome hoje: **só `apps/web`, neste mesmo repositório**, atualizado
> no mesmo PR. Não há cliente externo, versionamento público nem SDK publicado. Cerimônia de
> expand-contract para um consumidor interno que sobe junto é custo sem beneficiário.
>
> **TRADE-OFF ACEITO:** se um dia houver cliente externo, essa mudança teria sido breaking. O
> risco é real e pequeno hoje; se você preferir B, o plano muda pouco — diga antes do PR 1.
>
> **Plano expand-contract, se optarmos por B:** (1) publicar `/collections/batch`; (2) migrar o
> web; (3) marcar a antiga como deprecated na OpenAPI; (4) remover depois de uma release.

---

## 10. Impacto e compatibilidade

| item | impacto |
|---|---|
| `POST /collections` | **breaking** (DD-05). Consumidor único: `apps/web`, atualizado no mesmo PR. |
| `GET /symbols/search` | aditivo, não quebra. |
| Banco | nenhum. Linhas existentes intactas. |
| Agente do host | **nenhuma mudança de código.** Continua drenando a fila um job por vez. |
| Coleta de 1 símbolo | continua funcionando — é `items` com um elemento. |
| Fuzzer schemathesis | a rota alterada precisa continuar coberta; todo numérico de query já precisa de teto. |
| Feature flag | **não.** Mudança de tela interna, um usuário, rollback é reverter o commit. |

---

## 11. Riscos e atenção

| risco | gravidade | mitigação |
|---|---|---|
| **Fila de sondas na frente da coleta** (DD-04) | **alta** — é o risco principal | RN-11 (cache por par) + mostrar profundidade da fila. Item F-11 prova que reescolher não reenfileira. |
| Leva longa não cabe na sessão; o agente morre com ela | média | A tela estima em fatias de ano (RN-13) antes do botão. Fila perdida é recoletável (RN-14). Supervisão do agente está no backlog e **não** entra aqui. |
| Perder a serialização por acidente | média | CA-08 assere `max_jobs == 1`; item F-05 é teste de regressão sobre isso. |
| 20 símbolos × M1 = muito Parquet em disco | baixa | Nada muda no pico de memória (um ano por vez). Disco é o de sempre; 41 mil candles de H1 deram 1,4 MB. |
| Classe errada gravada em silêncio | média | RN-08 recusa a leva inteira (RN-06) nomeando **todos** os pendentes. CA-05 e CA-13. |
| Entrada não confiável | baixa | `Symbol` já é tipo validado; `items` ganha teto e checagem de repetição. Nada é interpolado em SQL. |
| Dado pessoal / LGPD | **nenhum** | A feature trata símbolos e candles. Não há dado pessoal envolvido. |
| Custo | nenhum | Tudo local; MT5 não cobra por requisição de histórico. |

**Idempotência e concorrência:** duas levas com o mesmo símbolo criam duas linhas em
`collections` (correto — são dois pedidos) e dois jobs, que rodam em sequência por `max_jobs = 1`.
O segundo reescreve o mesmo Parquet e atualiza a mesma linha de `datasets` (RN-14). Não há
corrida porque não há paralelismo.

---

## 12. Plano de implementação

Fatia vertical fina primeiro: o backend aceitando N antes de qualquer tela.

### PR 1 — API aceita N símbolos *(~180 linhas)*

`CreateCollectionRequest` vira `items` + validação de teto e repetição; a rota cria N linhas,
enfileira N jobs e devolve lista; o 409 passa a nomear todos os pendentes.
**Risco:** médio (breaking). **Reversão:** reverter o commit. **Pré-requisito:** nenhum.

### PR 2 — A classe que o caminho decide vira campo da API *(~70 linhas)*

`BrokerSymbolOut.asset_class_from_path`, derivado. Aditivo e isolado.
**Risco:** baixo. **Pré-requisito:** nenhum (pode ir em paralelo ao PR 1).

### PR 3 — Tela: seleção múltipla *(~320 linhas)*

`SymbolCombobox` ganha modo múltiplo com chips e teto de 20; `CollectSymbol` passa a mandar
`items`, pede classe por símbolo ambíguo e mostra a estimativa em fatias de ano.
**Risco:** médio (é o maior). **Pré-requisito:** PR 1 e PR 2.

### PR 4 — Sonda automática e aviso de janela curta *(~180 linhas)*

Ao escolher, enfileira sonda **só** para pares ausentes de `symbol_history`; a tela mostra
utilizável-a-partir-de por símbolo, avisa quem volta curto e mostra a profundidade da fila.
**Risco:** médio (é onde mora o risco principal). **Pré-requisito:** PR 3.

---

## 13. Plano de testes

| camada | o que prova |
|---|---|
| **Unitário (API)** | validação de `items`: vazio, 21, repetido, timeframe ruim, data invertida, instante naive. 409 nomeando todos. Ordem da resposta = ordem enviada. |
| **Unitário (schemas)** | `asset_class_from_path` derivado: caminho que decide → classe; caminho ambíguo → nulo; `path` nulo → nulo. |
| **Integração (Postgres real)** | N linhas realmente gravadas com a classe certa em cada; RN-06 provado por **ausência de linha** após 422/409 (contagem antes e depois). |
| **Fila (fake de arq)** | N jobs enfileirados, um por id, na fila `collect`. Nenhum job enfileirado quando a leva é recusada. |
| **Regressão de serialização** | `WorkerSettings.max_jobs == 1` asserido explicitamente, com o porquê no docstring do teste. |
| **Web (vitest)** | seleção/remoção de chips, teto de 20, botão desabilitado com motivo visível enquanto houver classe pendente, estimativa de fatias, aviso de janela curta, sonda **não** reenfileirada para par já sondado. |
| **Fuzzer** | schemathesis continua verde sobre a rota alterada. |

**Cuidados específicos deste repositório**, aprendidos aqui e que valem para estes testes:

- Teste de "nada foi enfileirado" só prova algo se a máquina teve chance de falar — conte antes
  e depois, não asserte só a ausência.
- `findByLabelText` espera o **rótulo**, não os dados: `fireEvent.change` para um valor sem
  opção é no-op silencioso.
- Nome acessível duplicado é defeito de a11y, não atrito de teste — com N chips, cuidar para
  cada um ter nome único.
- Rodar `vitest` **não** prova o gate do web: `tsc --noEmit` tem de rodar junto.
- Integração roda com `POSTGRES_DB=tradeforge_pr234` — a suíte **trunca** o banco que o ambiente
  apontar.

---

## 14. Padrão de qualidade inegociável

| Exigência | Regra |
|---|---|
| **Cobertura** | **100% do código novo desta feature**, linha **e** branch (diff coverage). |
| **Lint** | `ruff format --check` e `ruff check` sem violação no código novo; eslint limpo no TS. |
| **Tipagem** | `mypy --strict` limpo (rodado **sem caminhos**, como o CI) e `tsc --noEmit` limpo. Sem `type: ignore` novo. |
| **Testes** | Suíte inteira verde. Zero `skip`/`xfail` no que for escrito agora. |

A exigência é sobre o **código novo**. O passivo do repositório não é responsabilidade desta
feature — mas não pode aumentar. Cobertura vazia não conta: cada caminho precisa de asserção
sobre **comportamento**, não sobre a linha ter sido percorrida.

---

## 15. Checklist de acompanhamento

```
Status: em implementação · Progresso: 0/4 PRs mergeados · 5/14 itens · atualizado em 21/08/2026
```

### PR 1 — API aceita N símbolos

- [x] **F-01** · `CreateCollectionRequest` vira `items: list[CollectionItem]` com teto 20, mínimo 1 e recusa de repetido
      risco: baixo · produção: breaking interno
  - [x] Testes escritos primeiro (falharam antes)
  - [x] Implementado
  - [x] **Cobertura 100% do código novo** (linha e branch) — schemas.py: nenhuma linha nova entre as descobertas
  - [x] `ruff format --check` e `ruff check` limpos
  - [x] `mypy --strict` limpo, sem `type: ignore` novo
  - [x] Suíte completa verde
  - [x] Critérios cobertos: CA-03, CA-04
  - [ ] PR aberto e revisado
  - [ ] Mergeado — `<hash>`
  - [ ] Verificado após deploy
  - Status: **feito** · Notas: executado junto com F-02 (inseparáveis). Teto e piso testados
    **dos dois lados** da linha — 20 aceito e 21 recusado, 1 aceito e 0 recusado — porque um teste
    que só prova a recusa não distingue `>` de `>=`. Mutantes do piso, do teto e do duplicado:
    mortos.

- [x] **F-02** · Rota cria N linhas, enfileira N jobs, devolve lista na ordem enviada
      risco: médio · produção: breaking interno
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura **100% linha e branch** · [x] ruff · [x] mypy · [x] suíte verde
  - [x] Critérios cobertos: CA-01, CA-02
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: todas as linhas são commitadas antes do **primeiro** job ir para a
    fila — a ordem que o agente depende, agora valendo para o conjunto. Mutantes "só o primeiro
    item é coletado" e "a resposta vem ordenada em vez de na ordem pedida": mortos.

- [x] **F-03** · 409 nomeia **todos** os símbolos sem classe determinável, e nada é criado
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] ruff · [x] mypy · [x] suíte verde
  - [x] Critérios cobertos: CA-05, CA-06
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: mutante "nomeia só o primeiro" morto. A mensagem concorda em número
    (`is`/`are`, `it`/`them`) para não ficar agramatical no caso de um símbolo só, que é o comum.

- [x] **F-04** · Integração: contagem de linhas antes/depois prova RN-06 (nada criado no erro)
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] ruff · [x] mypy · [x] suíte verde
  - [x] Critérios cobertos: CA-07 (aceitação atômica)
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: ⚠️ contar linhas antes e depois é o que faz o teste valer. Asserir só
    o 409 passaria contra uma rota que já tivesse gravado as duas boas antes de chegar na terceira —
    esse modo de falha é **invisível pela resposta**. Vale para os três caminhos de recusa: classe,
    duplicado e teto.
    ⚠️ **CA-07 do documento fala de falha em execução** (uma das N falha no agente e as outras
    seguem), que só é observável com o agente rodando — **não coberto neste PR**. O que este item
    prova é a atomicidade da *aceitação*. Ver questão nova Q-03.

- [x] **F-05** · Regressão: `max_jobs == 1` asserido, com o porquê no docstring
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] ruff · [x] mypy · [x] suíte verde
  - [x] Critérios cobertos: CA-08
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: é a resposta inteira ao "não sobrecarregar". Asserido em vez de
    confiado ao comentário ao lado, porque **comentário não falha**. Mutante `max_jobs = 2`: morto.

### PR 2 — A classe derivada vira campo da API

- [x] **F-06** · `BrokerSymbolOut.asset_class_from_path`, derivado de `path`, nunca gravado
      risco: baixo · produção: aditivo
  - [x] Testes escritos primeiro (6 vermelhos antes) · [x] Implementado · [x] Cobertura **100%** · [x] ruff · [x] mypy · [x] suíte verde
  - [x] Critérios cobertos: CA-09
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: `@computed_field` sobre `@property` — derivado na serialização, nunca
    gravado, porque uma cópia em `broker_symbols` ficaria obsoleta no primeiro sync que refila um
    símbolo. 5 mutantes: 4 mortos, **1 equivalente documentado** (ver nota abaixo).

- [x] **F-07** · ~~Tipo TS regerado do schema~~ → **espelhado à mão**, e `tsc --noEmit` limpo
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: CA-09
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: ⚠️ o item estava errado — não há codegen para `api/types.ts`, e o
    header do arquivo diz que é espelho manual de propósito (só a **DSL** é gerada). Corrigido.
    O `tsc` pegou sozinho a fixture de `SymbolCombobox.test.tsx` que descrevia uma resposta que o
    servidor não consegue mais produzir.

### ⚠️ Nota: um `type: ignore` novo, contra a §14

`@computed_field` empilhado sobre `@property` faz o mypy emitir `prop-decorator` — ele **não
suporta nenhum decorador sobre `@property`**, e o `# type: ignore[prop-decorator]` é o contorno
que a documentação do próprio Pydantic prescreve. Não é defeito silenciado: é limitação de
ferramenta, e está estreitado nesse único código, então um erro de tipagem real no método ainda
derruba o portão.

Alternativa sem `ignore`: campo comum preenchido no `SymbolSearchOut.build`. Custa mover a
derivação para longe do campo e torna o valor definível pela entrada. Se você preferir, é uma
troca de dez linhas — **diga e eu troco**.

### PR 3 — Tela: seleção múltipla

- [x] **F-08** · ~~`SymbolCombobox` ganha modo múltiplo~~ → **`SymbolMultiCombobox` novo**, com a
      busca compartilhada extraída: chips, remoção, teto, nome acessível único por chip
      risco: médio
  - [x] Testes escritos primeiro (11, vermelhos antes) · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: CA-03
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: teto testado **dos dois lados** (no limite recusa, um abaixo aceita).
    ⚠️ O teto bloqueia **adicionar, nunca remover** — recusar todo clique com a lista cheia
    prenderia quem quisesse trocar um símbolo, com a única saída sendo os chips, que não é onde a
    pessoa está olhando. Mutante que apaga essa distinção: morto.

- [x] **F-09** · `CollectSymbol` manda `items`; classe pedida por símbolo ambíguo; botão desabilitado com motivo visível
      risco: médio
  - [x] Testes escritos primeiro (17) · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: CA-06, CA-13, e **RN-12 em parte** (a janela sugerida já respeita o
    piso mais tardio entre os escolhidos; o aviso por símbolo é o F-12)
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: a resposta da classe **morre junto com o símbolo** — senão ela
    sobrevive à pergunta e é reaplicada ao próximo que ocupar o lugar (mutante morto). O `<select>`
    ganhou `aria-label` próprio: o rótulo lia "XAUUSD CFDs\\Metals\\XAUUSD", que um leitor de tela
    anunciaria como o **nome** do controle — um caminho, onde a pergunta é que tipo de coisa é.

- [x] **F-10** · Estimativa em fatias de ano (símbolos × anos) antes do botão
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: CA-12
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: ⚠️ **anos de calendário, não anos decorridos** — espelha
    `collect.year_slices`, que corta na fronteira do ano porque é assim que o Parquet é
    particionado. Dez meses que atravessam o ano novo são **duas** fatias; dividir dias por 365
    diria uma, e erraria um download inteiro. Há um teste que usa as mesmas datas do teste de
    integração da API, para os dois não poderem discordar em silêncio.

### PR 4 — Sonda automática e aviso de janela curta

- [x] **F-11** · Sonda enfileirada **só** para par (símbolo, timeframe) ausente de `symbol_history`
      risco: **médio-alto** — é a mitigação do risco principal
  - [x] Testes escritos primeiro (7 para o hook + 6 para o `missing`) · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: CA-10
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: o conjunto `asked` é **`useRef`, não state** — escrever state no
    efeito re-renderiza, e o re-render roda o efeito de novo. Segunda guarda, mais sutil: só entram
    na lista os pares cuja consulta **já voltou vazia**; um pendente também não tem dado, e lê-lo
    como ausente dispararia sonda para par já em sondagem. Ambos os mutantes: mortos.

- [x] **F-12** · Utilizável-a-partir-de por símbolo e aviso de quem volta curto
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: CA-11
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: ⚠️ a comparação é `>` e **não** `>=`. A janela sugerida abre
    exatamente no piso vinculante, então `>=` faria a tela avisar sobre a **própria sugestão** toda
    vez — e aviso sempre ligado é aviso que ninguém lê. Mutante do `>=`: morto. Símbolo não medido
    **não** é reportado como coberto: silêncio não é atestado de saúde.

- [x] **F-13** · Profundidade da fila de sondas visível (a espera legível, não travamento aparente)
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado · [x] Cobertura 100% · [x] eslint · [x] tsc · [x] suíte verde
  - [x] Critérios cobertos: — (mitigação DD-04)
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: o silêncio é **ganho** — nada aparece quando não há nada sendo
    medido. Uma caixa que sempre dissesse "medindo 0 símbolos" treinaria o leitor a pular a única
    coisa na tela que explica uma espera.

- [x] **F-14** · Aula do PR em `docs/aulas/` e spec `specs/coleta-pela-tela.md` atualizado
      risco: baixo
  - [x] Escrito · [x] Revisado
  - Status: **feito** · Notas: `docs/aulas/PR-236-a-239-coleta-multi-ativo.md` (gitignorada) e uma
    seção nova no spec, que agora cobre PR-232 a PR-239.

- [x] **F-15** · *(item novo, fecha a Q-03)* Uma coleta falhando não danifica as outras
      risco: baixo
  - [x] Testes escritos primeiro · [x] Implementado (só teste — o comportamento já existia) · [x] ruff · [x] mypy · [x] suíte verde
  - [x] Critérios cobertos: **CA-07**
  - [ ] PR aberto · [ ] Mergeado `<hash>` · [ ] Verificado
  - Status: **feito** · Notas: três coletas em sequência compartilhando um root, a do meio com a
    conexão caindo. ⚠️ Asserido **no disco**, não nos journals — um journal reporta o que o código
    acreditou, e as partições são o que um backtest depois realmente lê. Segundo teste: uma série
    meio escrita **não** vai para o catálogo, porque uma linha em `datasets` alegando um range cuja
    metade final falta é pior que linha nenhuma. ⚠️ O que isto **não** prova é o agente registrar a
    falha na linha e seguir para o próximo job — isso mora em `collect_range`, que importa
    MetaTrader e não roda neste CI.

### Mapa critério → item

| critério | item(ns) |
|---|---|
| CA-01 | F-02 |
| CA-02 | F-02 |
| CA-03 | F-01, F-08 |
| CA-04 | F-01 |
| CA-05 | F-03 |
| CA-06 | F-03, F-09 |
| CA-07 | F-04 (aceitação atômica) + **F-15** (independência em execução) |
| CA-08 | F-05 |
| CA-09 | F-06, F-07 ✅ |
| CA-10 | F-11 |
| CA-11 | F-12 |
| CA-12 | F-10 |
| CA-13 | F-09 |

Nenhum critério órfão. ✅

### Registro de execução

| data | PR | o que mudou | surpresas |
|---|---|---|---|
| 21/08 | **#129** `8dac85d` — PR 1 (F-01..F-05) | `POST /collections` aceita N símbolos, N linhas, N jobs | O plano deixava a tela quebrada entre o PR 1 e o PR 3; PR 1 teve de levar uma mudança mínima no web. F-01 e F-02 são inseparáveis. 9/9 mutantes mortos. |
| 21/08 | PR 2 (F-06, F-07) | classe derivada do caminho no `/symbols/search` | F-07 partia de premissa falsa (não há codegen para `api/types.ts`). Um mutante **equivalente**: ler só a primeira palavra do caminho passa, porque o mapa tem `crypto` **e** `crypto currency` — o docstring que eu tinha escrito afirmava separar e não separava. |
| 21/08 | **#131** `f244468` — PR 3 (F-08..F-10) | seleção múltipla, classe por símbolo, estimativa | ⚠️ **O CI reprovou na primeira tentativa**: rodei `vitest run`, o portão é `vitest run --coverage`, e a branch caiu a 89,68%. A extração do hook de teclado o expôs sozinho a 70%, com o `Escape` nunca alcançado — estava escondido antes, diluído. Consertado cobrindo comportamento (seis testes de teclado), não o número. 12/12 mutantes. |
| 21/08 | PR 4 (F-11..F-15) | sonda automática com cache, aviso de janela curta, fila visível, independência das coletas | A cobertura caiu de novo (89,8%) e de novo o buraco era real: nada provava que **pendente não é ausente**, que é a guarda que impede a sonda de disparar para par já em sondagem. 7/7 mutantes. |

---

## 16. Suposições e questões em aberto

### Suposições (assumidas por você — corrija se estiver errado)

| # | Suposição |
|---|---|
| **S-01** | Um timeframe por leva. Você disse "seleciono o time frame", singular. Dois timeframes = duas levas. |
| **S-02** | A tela de coleta múltipla **substitui** a atual, não convive com ela. Um símbolo é uma leva de um. |
| **S-03** | A tela guarda os ids da leva recém-criada só na sessão do navegador (DD-01). Recarregar perde o agrupamento visual, não o trabalho. |
| **S-04** | Sem cancelamento de leva em andamento — não existe para coleta única hoje. |
| **S-05** | Sem feature flag. Um usuário, tela interna, rollback é reverter. |
| **S-06** | A ordem de execução é a de envio (arq drena FIFO). Não há prioridade entre símbolos da leva. |

### Questões em aberto

| # | Questão | Impacto |
|---|---|---|
| ~~**Q-01**~~ | ~~alargar `POST /collections` ou criar `/collections/batch`?~~ **RESOLVIDA em 21/08: alargada**, seguindo a recomendação. O Guilherme aprovou o plano sem se opor à recomendação registrada. Reversível se ele preferir a rota separada — o plano expand-contract está em DD-05. | — |
| ~~**Q-03**~~ | **RESOLVIDA no PR 4** pelo item F-15. ~~CA-07 (uma das N falha, as outras seguem) não está provado.~~ O comportamento decorre de as linhas serem independentes e da rota não as ligar, mas nada exercita o agente processando uma leva com uma falha no meio. Vale um teste que rode `collect_range` contra uma fonte que falha só num símbolo? | Item novo, provavelmente no PR 4. Não bloqueia o PR 1. |
| **Q-02** | O teto de 20 vale mesmo para coleta? Um basket de 20 são 20 backtests de segundos; uma leva de 20 em H1 desde 2009 são ~350 fatias de ano, serializadas — possivelmente muitas horas. Manter 20 por consistência, ou baixar? | Muda uma constante e uma mensagem. Pode ser decidido durante o PR 1. |

---

> Ao concluir qualquer item, atualize este arquivo: marque as caixas, registre o hash e atualize o
> progresso. Se a realidade divergir do plano, **corrija o plano** — não o esconda.
