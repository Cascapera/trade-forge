# Backlog

Ideias e trabalho fora do escopo do PR atual. Formato: `- [origem: PR-XXX] descrição — motivo de adiar`.

- [origem: PR-001] **Deploy automático** — `develop` → staging, `main` → produção, via GitHub
  Environments (secrets separados, approval gate em produção). Adiado: não há infraestrutura
  alvo ainda; o core só vira deployável na Fase 1.
- [origem: PR-001] **React 19** — o `sdd.md §9` fixa React 18 e nós seguimos o spec. Migrar exige
  ADR. Adiado: zero benefício antes de existir UI de verdade.
- [origem: PR-001] **TypeScript 7** — já é a versão estável (7.0.2), mas `typescript-eslint` ainda
  exige `<6.1.0`. Fixamos 6.0.3. Revisar quando o typescript-eslint suportar.
- [origem: PR-004] **Operandos literais na DSL** — hoje uma comparação só aceita refs (`{"ref": ...}`).
  Condições como `RSI < 30` exigem constante do lado direito (`{"value": 30}`). Adiado porque o
  spec da v1 não lista indicadores com limiar (RSI/ADX chegam na Fase 2) e porque adicionar uma
  variante de operando é uma mudança **aditiva**: estratégias já salvas continuam válidas, sem
  bump de `schema_version`. Fazer junto do primeiro indicador que precise.
- [origem: PR-004] **`apps/web` consumir `@tradeforge/schema`** — o validador TS existe e é testado,
  mas nenhuma tela o usa ainda. Entra no PR do Strategy Builder (Fase 1).
- [origem: PR-001] **Branch protection no GitHub** — exigir CI verde + 1 aprovação para mergear em
  `main` e `develop`. Precisa ser configurado na UI do GitHub (não é código); fazer junto do
  primeiro push.
- [origem: PR-103] **Quarentena de candle corrompido no collector** — a validação nova de `Candle`
  (tz-aware, extremos contendo o corpo) faz o backfill abortar inteiro num único candle sujo do
  MT5, sem relatório. Falhar alto é melhor que persistir lixo, mas o operador de um backfill de
  dez anos fica sem saída. Precisa pular a barra e reportá-la no gap report que já existe.
  Adiado: escopo do PR-102, não do núcleo.
- [origem: PR-103] **`Broker.trades()` sem escopo explícito** — o contrato diz "os round trips
  desta execução", e o `MT5Broker` da Fase 2 terá que filtrar por magic number para honrá-lo,
  senão o histórico de deals da conta inteira (outros EAs, outros símbolos, sessões anteriores)
  entra no resultado e a propriedade de reconciliação vira falsa em live. Decidir no PR de
  `MT5Broker`: `trades(symbol)` ou filtro por magic number.
- [origem: PR-103] **Preço negativo** — `Fill.price > 0` é correto para forex e ações, e será
  errado quando entrar futuro: o WTI fechou a -37 dólares em abril de 2020, e spreads de
  calendário são rotineiramente negativos. Revisar quando `AssetClass.FUTURE` sair do papel.
- [origem: PR-103] **Fill parcial** — hoje o `Portfolio` **recusa** (`EngineError`), porque
  aceitá-lo em silêncio inflava o P&L pelo volume inteiro da posição. Fill parcial é
  comportamento normal do MT5 em mercado fino, então a Fase 2 precisa decidir: ou o `MT5Broker`
  agrega os parciais antes de devolver um `Fill`, ou o ledger passa a suportar posição parcial.
- [origem: PR-105] **Fiação do `take_profit`/`risk_multiple` (rr) no worker** — o compilador só
  consome `exit.stop_loss`; o `rr` do `take_profit` é passado à mão ao construir o `BacktestBroker`
  (o golden faz `take_profit_rr=Decimal(2)`). Isso é coerente com a fronteira 104/105 (o alvo é do
  broker), mas um documento com `rr` **compila sem erro** e, se o worker do PR-107 construir o
  broker sem o `rr`, a posição roda sem alvo — divergência silenciosa. O worker deve ler o `rr` da
  DSL e/ou assertar consistência ao montar broker+estratégia.
- [origem: PR-105] **Slippage em exits protetivos** — stop/alvo preenchem no nível exato, sem
  derrapagem (só o gap-through-stop via `min(open, stop)` modela pessimismo). Um stop real é ordem
  a mercado e derrapa; o comportamento atual é levemente otimista. Travado por teste explícito
  (`test_a_protective_exit_fills_at_the_level_without_slippage`). Modelar derrapagem no stop quando
  houver dado de tick/spread para calibrá-la.
- [origem: PR-105] **`RiskManager.allow` é sempre `True`** — o veto (limite de perda diária, kill
  switch) precisa de estado que o método ainda não recebe: o equity de abertura do dia e o relógio
  da barra. É concern de sessão (salvaguardas do `sdd.md §11`); fiar quando o worker/sessão existir.
- [origem: PR-106] **Drawdown máximo abs e pct de vales diferentes** — em `metrics._drawdown` o maior
  recuo em dinheiro e o maior recuo percentual são maximizados **independentemente**, então podem vir
  de eventos distintos quando os picos estão em níveis diferentes. Cada métrica é o máximo real na sua
  unidade (defensável), mas se a Fase 3 quiser reportar "o drawdown" como um evento único, os dois
  precisam virar um par acoplado (o vale que maximiza um, não os dois separados).
- [origem: PR-106] **Base do CAGR vs janela** — `_cagr` usa `initial_capital` como base mas o span
  começa em `equity_curve[0].time`. Consistente enquanto a 1ª barra não tem posição aberta (o normal);
  se algum dia a série começar já marcada a mercado, a taxa mistura base e janela. Rever se/quando o
  worker gerar curvas que não começam no capital inicial.
- [origem: PR-106] **Sharpe/Sortino não anualizados** — são calculados sobre retorno por-trade
  (`net/initial`), sem composição nem fator de anualização. Escolha determinística e documentada, mas
  a UI (PR-108) precisa rotular como "por trade", não confundir com o Sharpe anualizado padrão de
  mercado. Decidir na UI se anualizamos (precisa de frequência de trades) ou só rotulamos.
- [origem: PR-106] **Property-tests faltando nas métricas** — falta (a) reconciliação
  `net_profit == sum(net_pnl)` sobre sequências aleatórias de trades e (b) r_multiple para short num
  property-test dedicado. A aritmética de short é coberta indiretamente e os goldens/bordas são fartos,
  mas um property fecharia a lacuna. Fazer junto do próximo PR que tocar `metrics.py`.
- [origem: PR-107] **Extrair o leitor de Parquet para um pacote compartilhado** — `apps/api` depende de
  `tradeforge-collector` só pelo `read_candles`, e o collector é uma borda Windows-bound (importa MT5,
  ainda que lazy). O leitor Parquet↔Candle é concern de dados, não de coleta; movê-lo para um
  `packages/data` (ou `packages/db`) do qual collector E api dependem removeria a dependência app→app.
  Adiado: refactor fora do escopo do PR-107 (mexeria no collector já mergeado).
- [origem: PR-107] **Slippage no venue config** — o worker passa `slippage_ticks=0` fixo; a API não
  expõe slippage porque não há onde persisti-lo (o `Backtest` não tem coluna). Reprodutibilidade exige
  que tudo do "venue simulado" fique gravado. Incluir slippage no `cost_model` JSONB (ou coluna nova)
  e ler no worker. Fazer quando slippage configurável importar (comparar cenários otimista/pessimista).
- [origem: PR-107] **Progresso do worker é grosso** — publica só `running` (0%) e `done` (1%); não há
  progresso intra-run (ex.: % de candles processados). O event loop do WS e o canal pub/sub já
  suportam; falta o worker publicar no meio do `run`. Fazer quando um backtest longo tornar o "0→100"
  frustrante na UI (PR-108).
- [origem: PR-107] **Worker roda SQLAlchemy síncrono dentro do arq async** — bloqueia o event loop do
  worker durante o `run` (CPU-bound) e as queries. Aceitável na Fase 1 (um job por vez, processo
  dedicado), mas ao escalar concorrência do worker, mover o trabalho pesado para um executor de threads
  ou usar sessão async. Anotar como dívida de escala, não de correção.
- [origem: PR-108] **Builder recursivo de condições** — o form guiado do PR-108 cobre comparações e UM
  nível de all/any (o caso das estratégias-demo). A DSL suporta all/any/not aninhados em qualquer
  profundidade; um editor de árvore recursivo (visual, arrastar/soltar) é o design final da Fase 2
  (`sdd.md §3.3.5`). Fazer quando setups compostos exigirem aninhamento profundo.
- [origem: PR-202] **Plugar AnchoredVWAP na DSL** — a classe de engine existe e conforma ao protocolo
  `Indicator`, mas NÃO está em `INDICATOR_BUILDERS` nem no `strategy.schema.json`. Hoje só os testes
  garantem o `ENGINE_CONTEXT` (via `localcontext` manual); em produção, o wiring precisa passar por
  `run()`. Ao ligar: (a) nó de schema com params próprios (source + volume, sem period); (b) alinhar
  o enum de `source` — `_price_reader` aceita `close`/`open` além dos 3 pedidos (hlc3/high/low), então
  o schema deve restringir ou o engine relaxar, senão a validação de 2 camadas diverge; (c) decidir a
  âncora na DSL (fixa? no último swing? re-ancorável). O `SwingDetector` também não tem exposição DSL.
- [origem: PR-201] **Indicadores do spec adiados** — o spec da Fase 2 lista RSI, ATR, Bandas de
  Bollinger, ADX e máx/mín de N períodos. Este slice do PR-201 entregou **RSI + operando literal**
  (`RSI < 30`); o Guilherme decidiu testar com esses antes de adicionar mais. Adiados para fatias
  seguintes (ou PR-201b): **MACD** (composto de EMAs, multi-saída — não estava no spec, adição
  aditiva via ADR-03/ADR-13), **ATR**, **Bollinger**, **ADX**, **máx/mín de N períodos**. ATR/ADX
  exigem True Range (dependência do candle anterior) + suavização de Wilder — a mesma base do RSI já
  implementada. Novos operadores do spec (`between`, `rising`, `falling`) também pendentes.
- [origem: PR-109] **GIF animado da vitrine** — o README embute uma screenshot estática da tela de
  resultados (gerada via `npm run screenshot`, reusa o mock do E2E). O spec pedia um GIF; um GIF do
  fluxo (builder → run → results) precisaria de gravação de tela animada, que não dá pra gerar
  headless. Gravar quando houver ambiente com screen capture (ou usar `playwright` com vídeo do
  contexto e converter para GIF).
- [origem: PR-202] **`confirmed_at` no `LiquidityPool` para blindar o sweep contra lookahead** — o
  `SweepDetector` só pode ser varrido por uma barra posterior à poça, mas hoje o backstop compara com
  `pool.time`, que é o tempo do *swing* (ocorrência), não o da confirmação. Como um swing de força N é
  confirmado N barras depois, `pool.time` está sempre no passado e o check nunca dispara na prática — a
  garantia real é o **contrato do chamador** (alimentar a barra em `update`, só então `track` das poças
  que ela produziu), documentado na docstring de `SweepDetector`. Se o chamador inverter, uma poça pode
  ser varrida pela barra que a revelou, sem nada falhar alto. Correção definitiva: o `LiquidityDetector`
  sabe o instante da confirmação (é quando `update` devolve a poça) — carimbar `confirmed_at` no
  `LiquidityPool` e trocar o backstop por `candle.time > pool.confirmed_at`. Fecha o buraco sem
  aquecimento e sem depender de ordem de chamada. **Fazer no PR que fizer a fiação** dos detectores
  (hoje `SweepDetector` e `LiquidityDetector` não têm chamador fora dos testes).
- [origem: PR-202] **Toque de raspão desarma o flip — CONFIRMAR REGRA COM O GUILHERME antes do setup
  de flip.** O toque de uma zona é não estrito (`low <= top` na demanda), então uma barra cuja mínima
  é *exatamente* o topo — que nunca entrou na zona — já conta como toque. Se ela fechar acima, a zona
  vira `departed` e perde `flippable` para sempre, e o rompimento posterior deixa de ser flip.
  Verificado: com `low=100.00` numa zona [90,100] o flip some; com `low=100.01` ele existe. Um tick
  numa barra anterior decide se o setup arma. O código segue a regra ditada ao pé da letra (ele disse
  "não pode tocar nela, subir, e depois vir flipar"), então não é bug — mas em gráfico real quase toda
  zona de demanda é raspada e abandonada antes de ser rompida de verdade, e o flip pode quase nunca
  armar. **Ação:** medir a frequência de `flipped` em dados reais e perguntar a ele se "tocar", para
  efeito de `departed`, exige penetração real (`low < top`) em vez de encostar. É pré-requisito do
  setup de flip, não dívida técnica.
- [origem: PR-202] **Order block — arestas conhecidas do detector, todas de baixo impacto.** (a) Um gap
  de direção *oposta* entre dois gaps a favor conta como "pausa" no agrupamento de runs, gerando duas
  zonas onde a regra literal ("uma barra sem gap basta") diria que a barra do meio tem gap; exige
  `c9.low > c11.high` e `c10.high < c12.low` no mesmo trecho, e o espírito da regra favorece o
  comportamento atual. (b) Se um run de gaps consecutivos começa antes do `origin_time`, o filtro de
  perna remove o prefixo e a zona é marcada no primeiro gap *sobrevivente*, não no primeiro do run
  (verificado que o filtro só remove prefixo, nunca fragmenta o meio). (c) Não há limite de zonas por
  rompimento: uma perna com N gaps pode devolver ~N/2 zonas num único `update` (pior caso ~250 com
  `_MAX_LOOKBACK=500`), o que é um contrato surpreendente. (d) A origem de um CHoCH numa barra externa
  usa o topo anterior, não o desta barra — a janela da perna começa cedo demais, o que é permissivo,
  não vazante.
- [origem: PR-203] **`OrderRequest` não valida ordem limite do lado errado** — uma compra limite
  *acima* do mercado (ou uma venda *abaixo*) é recusada no `Signal`, mas não no `OrderRequest`. Quem
  submeter direto ao broker a vê preencher na abertura, virando uma ordem a mercado silenciosa. Hoje
  todo caminho passa pelo `Signal` (o `run()` constrói o `OrderRequest` a partir dele), então a engine
  está protegida — mas a proteção é geográfica, não estrutural. Fechar quando o broker ganhar um
  segundo chamador (o `MT5Broker` da Fase 2, ou a maquinaria de entrada se ela submeter direto).
- [origem: PR-203] **Condução de stop: breakeven no 1º BOS a favor, depois atrás dos topos/fundos
  válidos** — decisão fechada com o Guilherme em 21/07/2026, deliberadamente FATIADA para depois da
  maquinaria de entrada. Hoje o stop é fixo: armado no fill dentro do `_Protection` e nunca mais
  tocado. Mover o stop de uma posição aberta é **peça nova no protocolo `Broker`** (`modify_stop` ou
  equivalente), logo exige ADR próprio + `engine-guardian`. Parciais entram na mesma fatia. Motivo de
  adiar: primeiro ver os setups abrindo e fechando operação com stop fixo; gestão de trade depois.
- [origem: PR-204] **Reconciliação estratégia↔broker em live** — o `Signal` é fire-and-forget: a
  `StructureStrategy` não tem canal de confirmação do que o broker/loop fez com a intenção. Quatro
  sub-casos com a mesma causa raiz, para resolver juntos no PR do `MT5Broker` (provavelmente com
  eventos de ordem no `Context`, na linha do ADR-0015): (a) trade manual no mesmo símbolo faz o
  fallback de `position` em `_observe_fill` queimar a zona errada e esquecer `_armed`, deixando
  ordem órfã no book; (b) veto do risk manager ou sizing zero descartam a ordem com `placed=True`
  já gravado — a estratégia acredita ter ordem no book (fantasma; desde o ADR-0015 a zona não é
  mais queimada nesse caso, só o fantasma persiste, com cancel espúrio inofensivo ao morrer);
  (c) descarte do ADR-0014 (barra que atravessa ordem + stop juntos): sem fill, a zona não queima
  e o nome armado fica fantasma — backtest conservador vs. live, onde daria fill+stop (scratch
  trade que queimaria a região); (d) nota do `client_id`: o formato `%Y%m%dT%H%M` trunca segundos
  e é o **contador** que garante unicidade abaixo da resolução de minuto — irrelevante com piso M1
  do MT5, mas não "simplificar" o contador no futuro. **(e) [PR-205] O executor MT5 precisa mapear
  exit de stop para `reason == "sl"` literalmente** — `_trade_outcome` classifica o desfecho por
  essa string, e a escada do choch lê stop como "avança para a próxima zona" e qualquer outro
  exit como "venceu, encerra a escada". Um rótulo diferente inverte a regra do autor em silêncio.
  **(f) [PR-205] `won` hoje = qualquer exit que não é `"sl"`.** Só existem `"sl"`/`"tp"`; quando
  nascer um `exit.condition` de estratégia, perguntar ao Guilherme se saída por condição encerra
  a escada (tratamento atual) ou a faz avançar.
- [origem: PR-205] **CHoCH contrário confirmado com posição aberta é perdido pelo qualifier** —
  `StructureStrategy.on_bar` retorna cedo quando há posição, então o `break_` daquela barra nunca
  chega ao `qualify` e a escada nova não é instalada; o setup fica em silêncio até o próximo CHoCH.
  Janela estreita mas real: exige a âncora do CHoCH entre o topo da zona e o topo + buffer (o
  fechamento confirma a virada sem acionar o stop). Conservador e determinístico — a escada velha
  se auto-cura (o fechamento contrário fica além do topo de todo degrau da perna, mitigando todos),
  então nunca há trade errado, só um trade do método que o backtest não toma. Decidir com o
  Guilherme se um choch perdido deve ser re-qualificado quando a posição fechar.
- [origem: PR-206] **Fiação da DSL para os setups de estrutura (choch, continuação) + `max_bos`** —
  hoje `ChochQualifier`/`ContinuationQualifier` e o `StructureStrategy` só existem na engine; o JSON
  Schema (`packages/schema`) e os tipos TS ainda não têm um nó de estratégia de estrutura, nem os
  parâmetros `allow_secondary`/`stop_buffer`/`max_bos`. Quando entrar, é mudança **aditiva** (novo
  membro da união de estratégia) → mantém `schema_version` (ADR-0013). O `max_bos` da continuação é
  `int | None` (None = ilimitado, 1 = one-shot) — expor como opcional com default null. Fazer no PR
  que ligar os setups de estrutura à API/builder, não antes (nenhuma tela os usa ainda).
- [origem: PR-206] **Continuação com posição aberta perde o BOS a favor** — mesmo padrão do CHoCH
  contrário acima: `on_bar` retorna cedo com posição aberta, então um BOS que confirmaria uma nova
  perna de continuação enquanto um trade da perna anterior ainda está aberto nunca chega ao
  `qualify`. Conservador (não instala escada nova cedo demais), mas é um trade do método que o
  backtest pode não tomar. Mesma decisão pendente do item do CHoCH contrário — resolver os dois
  juntos se/quando o early-return por posição for revisitado.
- [origem: PR-204] **Churn de ping-pong entre duas zonas vivas** — com a queima no fill (ADR-0015),
  um qualifier patológico que alterna os nomes entre duas zonas vivas cancela/rearma a cada barra.
  Nenhum invariante quebra (uma ordem viva por vez, cancel antes de entry na mesma barra, fill
  duplo impossível), mas em live é round trip de cancelamento por barra. O freio (histerese ou
  cooldown por zona) é decisão de método do Guilherme, não da maquinaria — decidir quando um
  qualifier real exibir o padrão.
- [origem: PR-204] **`side` do CANCEL sempre resolvível (M19)** — o loop ignora o `side` de um
  `Signal` de cancel (`broker.cancel(client_id)` não roteia por lado). Mutante equivalente hoje;
  vira relevante num futuro `MT5Broker` que roteie cancelamentos por lado. Testar quando existir
  um consumidor que leia o campo.
- [origem: PR-207] **`stop_loss` do lado errado do nível de repouso é descarte silencioso** — uma
  compra-stop em 1.10500 com `stop_loss` em 1.10600 (SL *acima* do gatilho, erro de sinal) é aceita
  pelo `Signal` e depois descartada sem ruído por `_survives_the_gap`, porque o fill nasce já além
  do próprio stop. A limite tem exatamente o mesmo buraco desde o ADR-0014. A validação de lado
  `stop_loss` × (`limit_price` | `stop_price`) no `Signal` fecharia os dois de uma vez — mesma
  família da validação de lado que o `stop_price` já ganhou contra o `reference_price`. Fora do
  escopo do PR-207 porque muda o contrato da limite também, e isso pede seu aval.
- [origem: PR-208] **Ordem órfã no live quando aparece uma posição estranha** — se uma posição que
  a estratégia não abriu surgir com uma ordem descansando (trade manual na mesma conta, reconexão
  do adaptador que replica estado), o fallback de posição do `_observe_fill` derruba o nome e marca
  a virada como gasta, deixando no broker uma ordem viva que ninguém mais consegue cancelar. Mesma
  forma que `setups.py` já tem, e **inalcançável no backtest** (só a nossa ordem abre posição), por
  isso não é bug deste PR. O conserto real é reconciliação no adaptador live — casar ordens e
  posições por `client_id` na reconexão — e vale resolver junto com o item de reconciliação do MT5
  (o mesmo em que o executor precisa rotular exit de stop como `"sl"` literalmente).
- [origem: PR-208] **`Candle` não valida positividade, e o lado vendido reage diferente do comprado**
  — `Candle.__post_init__` só checa contenção do corpo e UTC, então um candle de preço não positivo
  é construível. Com ele, `Mme9BreakoutStrategy._entry_for` diverge por lado: no LONG a guarda de
  `stop_loss <= ZERO` faz a estratégia **calar** (não arma); no SHORT não há espelho — `stop_price`
  seria o `low` não positivo e o `Signal.__post_init__` **levanta `ValueError`** em vez de não armar.
  Preço negativo não é hipótese de laboratório (WTI liquidou a −37,63 em abril/2020). O conserto
  certo é no `Candle` (validar preço positivo de uma vez, para a engine inteira), não uma guarda a
  mais no setup — por isso ficou fora do PR-208. Decidir se o modelo aceita preço negativo como
  dado válido; se aceitar, a guarda do LONG é que está errada.
- [origem: PR-210] **`Portfolio.initial_capital` é um acessor morto** — a property existe desde o
  PR-105 e nenhum chamador a lê (o `compute_metrics` recebe o capital inicial como argumento, não
  do portfólio). É a única linha do módulo sem cobertura, e por isso a única que faz o `portfolio.py`
  não fechar 100%. Não foi tocada neste PR porque é anterior a ele e um PR = um escopo: decidir se
  apaga a property ou se o `RunResult`/métricas passam a lê-la em vez de receber o número por fora.
- [origem: PR-213] **Nenhum setup da camada swing/SMC é lançável pela DSL** — o
  `PontoContinuoStrategy` nasceu com o construtor já preparado para vir de JSON (o `average` é
  validado em runtime com `.get`, não confiando só no `Literal`, exatamente porque quem vai chamar
  é uma camada de wiring sem tipos), mas esse chamador não existe: `apps/api/.../runner.py:128` só
  faz `compile_strategy(definition)`, que entende indicador + comparação e mais nada. Vale para o
  `Mme9BreakoutStrategy` e para o `StructureStrategy` também — tudo que foi construído desde o
  PR-202 é invisível para a API e para a UI. Fora do escopo aqui porque é um PR de wiring (nó de
  schema + fábrica + tipos gerados), não de engine, e é o próximo passo combinado.
- [origem: PR-213] **`_MIN_CORRECTION` é constante de classe, não parâmetro** — o "2 correções" do
  Ponto Contínuo é regra do autor e deliberadamente não é um botão que uma busca de parâmetros deva
  girar, ao contrário do `breakeven_at_r`. Fica anotado porque no dia em que virar parâmetro os
  quatro testes de contagem (consecutividade, estritura das duas comparações, queima no
  cancelamento) são o que impede a mudança de passar despercebida — e porque a docstring da classe
  descreve o 2 como escolha dele, o que um leitor pode confundir com configurabilidade.
- [origem: PR-216] **O detector de gaps não conhece horário de pregão** — ele classifica como
  "anômalo" tudo que não é fim de semana, então uma ação americana (que negocia ~6,5 h por dia)
  produz um gap anômalo por madrugada: 499 deles num backfill de 2 anos do AAPL. O relatório vira
  ruído exatamente onde deveria avisar, e um gap de verdade se esconde no meio. Precisa da sessão
  do instrumento (o MT5 a expõe em `symbol_info().session_*`). Adiado: escopo do PR-216 é o
  relógio do servidor, não a classificação de gaps.
- [origem: PR-217] **As constraints do `rev_0001` estão com prefixo duplicado no banco** — o
  alembic aplica a convenção de nomes do `base.py` POR CIMA do nome passado, e o `rev_0001`
  passa o nome já completo. Resultado medido no Postgres: `ck_backtests_ck_backtests_date_range`
  onde o modelo espera `ck_backtests_date_range` — vale para **todas** as checks das tabelas
  criadas lá. Consequências: um `--autogenerate` reporta diferença fantasma, e qualquer
  `op.drop_constraint` futuro que use o nome do modelo falha. Conserto é uma migration que
  renomeia (`ALTER TABLE ... RENAME CONSTRAINT`), mecânica mas larga. Fora do escopo do PR-217,
  que só precisava não repetir o erro — a `rev_0002` passa o nome **nu** e sai correta.
- [origem: PR-216] **Medir o offset com o mercado fechado** — `offset_is_plausible` recusa uma
  medição impossível (fim de semana, madrugada), mas uma parada mais curta que a faixa de ±14 h
  passa despercebida: rodar duas horas após o fechamento mede duas horas a mais e parece normal.
  Separar de verdade exige uma segunda leitura segundos depois, para ver se o tick ainda avança —
  o que compra certeza com tempo de parede e não-determinismo. Hoje a saída é `--server-offset`.
  Reabrir se alguém agendar backfill fora do pregão sem poder declarar o offset.
- [origem: PR-218] **Os testes de integração apagam o banco para onde apontarem** — a fixture
  `dsn()` (`packages/db/tests/conftest.py:30`) devolve `PostgresSettings().sqlalchemy_dsn`, ou
  seja, **o banco que as variáveis de ambiente disserem**, e a fixture `session` faz
  `TRUNCATE trades, backtest_metrics, backtests, strategies, datasets, instruments RESTART
  IDENTITY CASCADE` antes de cada teste. No CI isso é inofensivo (o workflow sobe um Postgres
  descartável como service). Na máquina do dev **não é**: rodar `POSTGRES_HOST=localhost
  POSTGRES_PORT=5433 uv run pytest -m integration` esvazia o banco de desenvolvimento, e foi
  exatamente o que aconteceu em 04/08 — os três backtests de 03/08 e os 4 instrumentos semeados
  se perderam (o Parquet, que é o dado caro, não foi tocado). Nada avisa: o comando é o mesmo
  que o CI roda e a suíte passa 36/36.
  Conserto candidato: a fixture criar/derrubar um banco próprio (`tradeforge_test`) a partir do
  DSN recebido, em vez de usar o banco nomeado nele — assim o comando fica idêntico em CI e
  local e a segurança não depende de quem lembrou de exportar a variável certa. Uma recusa
  explícita ("o DSN aponta para um banco com dados; use um banco de teste") é o mínimo.
  **Paliativo que já funciona** (verificado em 04/08, PR-219): `POSTGRES_DB` existe e é
  respeitado, então dá para rodar em segurança criando o banco à mão primeiro —
  `docker compose exec -T postgres psql -U tradeforge -d postgres -c "CREATE DATABASE
  tradeforge_test OWNER tradeforge;"` e depois `POSTGRES_HOST=localhost POSTGRES_PORT=5433
  POSTGRES_DB=tradeforge_test uv run pytest -m integration --no-cov`. Passa 36/36 e não toca
  no banco de desenvolvimento. O item continua aberto porque isso depende de lembrar da
  variável, que é exatamente o que falhou.
- [origem: PR-220] **Um buraco no meio de uma série de snapshot é acidental, não estrutural** —
  `_AverageTrail.record` pula leituras `None`, e isso só produz buraco à esquerda (aquecimento)
  porque `SMA.value()` e `EMA.value()` nunca voltam a `None` depois de aquecer. **`VWAP.value()`
  devolve `None` quando não houve volume** (`indicators.py`), então no dia em que um trail for
  construído sobre VWAP uma barra sem negócio no meio da sessão produz exatamente o buraco que o
  docstring de `SnapshotSeries` chama de "sem causa legítima" — em silêncio, e num indicador
  intradiário isso é rotina, não exceção. Achado pelo engine-guardian na revisão do PR-220.
  Conserto quando existir o primeiro trail não-média: ou o ponto passa a admitir valor nulo (e o
  desenho quebra a linha ali de propósito), ou o trail recusa lacuna interna. Hoje é inalcançável
  — o único produtor é a média — por isso fica anotado em vez de resolvido.
- [origem: PR-223] **"Uma região é oferecida no máximo uma vez" não tem prova** — na varredura que
  o rompimento faz, `structure.py:1186` guarda `r.index > self._index`. Trocado por `>=`, a suíte
  inteira passa. O efeito da mutação é real: uma região nascida na *própria* barra do rompimento
  sobrevive à limpeza e, se for do mesmo lado, pode ser oferecida de novo num rompimento posterior
  com outro `confirmed_at` — dois `TrackedZone` para o mesmo gap, e um deles com a idade errada.
  Fora do escopo do PR-223 porque o comportamento é **idêntico ao da base** (`g.time >
  break_.time` também esvaziava tudo); o PR não mexeu nessa linha. Achado pelo engine-guardian.
- [origem: PR-223] **Coletar dados pela tela, com busca de ticker no MT5** — pedido dele em
  06/08/2026: escolher ativo e timeframe no front e o sistema coleta, mais um campo de busca que,
  ao digitar o ticker, pergunta ao MT5 se aquele símbolo existe. Motivação real: ele troca de
  corretora/mercado dentro do MT5, então a lista de símbolos disponíveis **muda**, e hoje a única
  porta é a CLI do collector.
  ⚠️ **A restrição que decide a forma:** `AGENTS.md §5.4` / ADR-02 — nada fora de `apps/collector`
  e `apps/executor` importa a lib `MetaTrader5` — e o MT5 só roda no **host Windows**, enquanto a
  API e o worker rodam **em container**. A API não pode falar com o MT5, nem hoje nem depois.
  Desenho candidato, que reusa o encanamento que já existe em vez de inventar transporte:
  1. **Um segundo worker arq, rodando no host** (fora do docker, ao lado do terminal MT5),
     consumindo uma fila `collect` do mesmo Redis. A API só enfileira; o host coleta, escreve o
     Parquet e cataloga. A tela acompanha por polling, exatamente como já faz com backtests.
     O preço: ele precisa manter esse processo no ar, como já mantém o terminal aberto.
  2. **Busca de símbolo não pode ser round-trip por tecla.** O mesmo worker publica um *snapshot*
     do catálogo do broker (`mt5.symbols_get()` — 9550 símbolos na conta dele) para o Postgres, e
     a API serve a busca daí: instantânea, e continua funcionando com o terminal fechado. Um botão
     "sincronizar símbolos" cobre a troca de corretora, que é o caso que ele descreveu.
  3. ⚠️ **Bloqueio conhecido, e ele já anunciou que vai cair nele:** `mt5_source.py` chama
     `mt5.initialize()` **sem argumento**, então com dois terminais instalados não há como
     escolher qual. Precisa de `--terminal-path` → `initialize(path=...)` antes de "trocar de
     corretora" virar operação de tela.
  4. Timeframe **não** é trabalho: a cadeia inteira já é agnóstica (a DSL é dona da lista dos 8,
     a tela renderiza `TIMEFRAMES`, o collector mapeia por `getattr(mt5, f"TIMEFRAME_{tf}")` e a
     constraint do banco aceita os 8). Hoje só existe H1 em disco porque só H1 foi coletado.
- [origem: PR-223 / coleta 06/08] **As barras D1 chegam carimbadas um dia adiantado** — medido em
  06/08/2026 no backfill de AAPL D1: **499 de 499** barras batem, candle a candle, com o pregão do
  **dia seguinte** em UTC. A barra rotulada `2024-08-01 21:00Z` contém o pregão de 02/08.
  Não é bug do collector: o servidor do broker é **UTC+3**, o dia dele abre à meia-noite do
  servidor, e `--server-offset +3` converte isso corretamente para 21:00Z do dia anterior. O
  carimbo é o instante de **abertura** da barra, que é a mesma convenção do H1 (13:00Z abre o
  pregão das 13:30). Só que no D1 o instante de abertura cai na **data anterior** em UTC.
  Consequências, em ordem de gravidade:
  1. ⚠️ **Lookahead latente em multi-timeframe.** Uma barra D1 carimbada em 21:00Z do dia X resume
     o pregão de X+1. Num backtest que misturasse D1 com H1/M15 ela seria consumida **antes** das
     barras intradiárias que ela sumariza — lookahead puro, e do tipo que melhora o resultado em
     silêncio. Hoje é inalcançável (um backtest roda **um** timeframe), por isso fica anotado em
     vez de resolvido; vira bloqueante no dia em que multi-timeframe existir.
  2. **A data reportada dos trades sai um dia errada** num backtest D1: um trade "de 01/08" na
     tela aconteceu de fato em 02/08.
  3. **A janela do backtest desloca.** `_candles_to_run` filtra por `date_from <= time <= date_to`,
     então pedir `date_from=2024-08-01` inclui uma barra que é o pregão de 02/08 e exclui a de
     01/08 (carimbada em 2024-07-31 21:00Z).
  Conserto candidato: normalizar o carimbo do D1 (e de qualquer timeframe >= D1) para a **data da
  sessão** que a barra representa, em vez do instante de abertura no servidor. Precisa da sessão
  do instrumento, que é a mesma dependência do item dos gaps anômalos acima — os dois se resolvem
  juntos. Intraday (M5/M15/H1/H4) **não** tem o problema: conferidos contra o H1, batem exatamente
  (2948, 2970 e 996 comparações, zero divergências) e nunca cruzam a fronteira do dia.
- [origem: coleta 06/08] **O MT5 recusa `copy_rates_range` acima de ~11 mil barras** — medido com
  AAPL M5: 2 anos (~42 mil) e o ano de 2025 inteiro (~19 mil) devolvem `(-2, 'Terminal: Invalid
  params')`, enquanto 3 meses (4.799), 2024 parcial (7.993) e 2026 parcial (11.161) passam. O erro
  **não distingue** "timeframe indisponível" de "pedi demais", e a mensagem que o collector propaga
  também não. Paliativo usado: coletar **por ano-calendário**, porque `write_candles` usa
  `existing_data_behavior="delete_matching"` e substitui exatamente as partições de ano da rodada
  — fatiar *dentro* de um ano apagaria o pedaço anterior. Para 2025 foi preciso buscar duas metades
  em diretórios temporários e gravar o ano numa única chamada. Conserto candidato: o collector
  fatiar sozinho quando o intervalo pedido exceder o limite, acumulando antes de escrever; e a
  mensagem de erro sugerir a janela menor em vez de só repassar o código do terminal.
- [origem: coleta 06/08] **Backfill em pedaços deixa o catálogo descrevendo só o último pedaço** —
  `record_dataset` (`packages/db/.../instruments.py`) faz upsert com
  `index_elements=[Dataset.instrument_id, Dataset.timeframe]`, então cada rodada **substitui** a
  linha em vez de estender a cobertura. Como o limite de ~11 mil barras do MT5 (item acima) obriga
  a coletar por ano, a linha final descreve apenas o último ano coletado. Medido em 06/08, disco
  contra catálogo: EURUSD e GBPUSD H1 têm **9804** candles desde 01/01/2025 no Parquet e o catálogo
  diz **3605** desde 01/01/2026; AAPL M5 tem **38 391** desde 01/08/2024 e o catálogo diz
  **11 161** desde 02/01/2026. Escrever pelo `write_candles` direto (como foi preciso fazer para
  fundir as metades de 2025) não cataloga nada.
  Não bloqueia nada hoje: o backtest lê o Parquet do disco e **nada** na API ou na web consulta
  `datasets` — o próprio `config.py` diz que "the `datasets` row only proves coverage, the bytes
  live on disk". Mas é exatamente a tabela cujo propósito é provar cobertura, e ela está afirmando
  menos do que existe. Conserto candidato: o upsert unir a faixa (`least(date_from)`,
  `greatest(date_to)`) e recontar do Parquet em vez de confiar no que a rodada trouxe — recontar é
  o único jeito de a linha ficar verdadeira depois de uma escrita que não passou pelo CLI.
  ⚠️ **AAPL H1 não está no catálogo** por outro motivo, já registrado: foi coletado em 03/08 e o
  banco foi truncado em 04/08 pelos testes de integração; os instrumentos foram resemeados, os
  datasets não.
- [origem: PR-226+ / 11-08-2026] **`upsert_dataset` carimba `collected_at` pelo relógio do
  processo, não pelo do banco** — `instruments.py` monta o `set_` do `ON CONFLICT` com
  `dt.datetime.now(tz=dt.UTC)`. Está correto no essencial (ao contrário do `updated_at` dos
  instrumentos, que não se movia e foi corrigido em `fix/instruments-updated-at`): a coluna
  **avança** a cada upsert. O que diverge é a fonte do tempo. `models._created_at` declara a
  regra do projeto — *"server_default, not a Python default: the database's clock is the one
  clock every writer shares, whatever timezone the machine that inserted thinks it is in"* — e
  esse caminho é exatamente o que a regra descreve: quem escreve é o collector no **host
  Windows** e a linha vive no **container**, dois relógios que não têm obrigação de concordar.
  Consequência real, embora pequena: `datasets.collected_at` e `instruments.updated_at` passam a
  ser medidos por relógios diferentes, então ordenar ou comparar as duas colunas entre si pode
  inverter eventos próximos. Não bloqueia nada — nada na API nem na web lê `datasets`
  (ver o item da cobertura acima). Conserto: trocar por `func.now()`, uma linha, junto do
  primeiro trabalho que já for tocar `upsert_dataset` — provavelmente o item da cobertura, que
  vai reescrever esse `set_` de qualquer forma.
- [origem: PR-228] **Agregar candles quando uma corrida passa do teto do gráfico** — o
  `GET /backtests/{id}/candles` recusa (422) uma corrida que leu mais de 50 000 barras, em vez de
  reduzir. Recusar é deliberado: **decimar está errado** — um candle não é uma amostra de preço, é
  o resumo de um intervalo, e jogar fora nove de cada dez apaga máximas e mínimas, inclusive a
  máxima que estopou o trade. O gráfico ficaria liso, plausível e sem a barra que explica o trade
  ao lado. A redução correta é **agregar** (primeiro open, maior high, menor low, último close),
  que preserva os extremos — mas é uma regra com borda de verdade (o que fazer com o balde
  incompleto no fim da janela) e merece testes próprios. Adiado por não ter caso de uso: o maior
  dataset do projeto tem 38 986 barras (EURUSD M15) e a corrida mais longa já executada leu
  12 883, então hoje **tudo cabe inteiro** e nada é reduzido. Fazer quando houver M1 coletado.
- [origem: PR-228] **Endpoint genérico de candles por instrumento** — hoje as barras só são
  servidas presas a uma corrida (`/backtests/{id}/candles`), de propósito: a janela é a
  procedência que a corrida gravou, então o gráfico não pode ser pedido para um período que ela
  não executou. Um `GET /instruments/{symbol}/candles?timeframe&from&to` é o superconjunto e vai
  fazer falta na hora de **pré-visualizar o dado antes de lançar** e na tela de coleta (item do
  PR-223). Fazer quando existir esse segundo consumidor — não antes, porque um endpoint com
  janela livre convida a tela de resultado a montar a janela sozinha e a errá-la em silêncio.
- [origem: PR-228] **Docstring órfã em `apps/web/src/api/hooks.ts`** — o bloco que descreve o
  snapshot por trade ("The entry picture for one trade, fetched only once someone opens it")
  está imediatamente acima de `useCreateBasket`, não de `useTradeSnapshot`. Documentação
  apontando para a função errada, sem efeito em runtime. Mover junto do próximo trabalho que já
  for tocar o arquivo.
- [origem: PR-229] **Ancorar a curva do `/overlays` na trilha que a corrida persistiu** — hoje o
  teste de valor do endpoint constrói a expectativa dirigindo o mesmo indicador sobre as mesmas
  barras. Isso pega janela errada, contexto decimal errado, semente e alinhamento errados, mas
  compartilha a implementação da EMA. A âncora **forte** já existe no banco e não precisa ser
  inventada: uma corrida grava a média que julgou cada entrada dentro do `snapshot` daquele trade
  (`_AverageTrail`), *durante* a corrida. Comparar o ponto servido no instante do snapshot com o
  valor gravado ali fecha o laço contra números escritos pela corrida, não ao lado dela. Adiado
  porque a fixture de integração não produz trade preenchido: numa série que sobe de forma
  monotônica o MME9 rearma a cada barra e a ordem em repouso nunca é tomada — construir o cenário
  que preenche é trabalho próprio. Fazer junto do PR que desenhar as regiões (ele vai precisar de
  uma fixture que negocia de qualquer jeito).
- [origem: PR-230] **A janela terminando na barra que CONFIRMA uma região** — o replay de
  `_zones_of` já é provado alimentar o último candle da janela, mas só pelo lado da *mitigação*:
  `test_the_last_bar_of_the_window_is_replayed_like_every_other` fecha a janela na barra cujo
  pavio toma a secundária. O simétrico não tem cenário — uma janela que termina exatamente no
  rompimento que **revela** regiões novas, que sob um replay curto sumiriam da resposta sem que
  nada no payload pudesse dizer que faltou alguma. Não é bloqueante e não é regressão deste PR:
  o laço é o mesmo para os dois eventos (não há ramo separado para "confirmar" e "mitigar"), e o
  mutante genérico de truncamento já morre pelo teste que existe. O que falta é o cenário que
  afirma o caso **pelo seu próprio nome** — construí-lo custa um probe novo sobre o
  `GAPPING_IMPULSE` com a janela cortada na barra 9. Fazer junto do próximo trabalho que já for
  tocar a marcação de regiões.
- [origem: PR-203] **`_key` diverge em float ≥ 1e16 e a reutilização de estratégia falha em
  silêncio** — o estudo compara documentos por `json.dumps(sort_keys=True)` para reusar a linha
  já existente em vez de colidir no `unique(name, version)`. Medido no round-trip real por
  `::jsonb`: `0.1`, `2.0`, `3.30`, `1e-07` e inteiros de 20 dígitos batem byte a byte, mas a
  partir de `1e16` o Python escreve `1e+16` e o Postgres devolve `10000000000000000` — as duas
  cadeias diferem, a reutilização não acontece, e o ponto vira **versão N+1 de uma linhagem
  nova sem que nada dê erro**. Não é alcançável hoje: todo parâmetro numérico do DSL tem teto
  (`le=1000`, `le=100`, `le=10_000`), e o único float sem teto é `Condition.value`
  (`packages/schema/.../models.py:80`). Consertar comparando por JSONB do lado do banco, ou pôr
  teto no `Condition.value`, no dia em que uma grade puder varrer esse campo.
- [origem: PR-204] **A corrida de teste órfã, entre criá-la e ligá-la à dobra** — o resume pula a
  dobra que já tem `test_backtest_id`, o que fecha o caso normal. Resta uma janela estreita: o
  `_process_fold` grava a corrida out-of-sample (`session.commit()`) e só depois escreve
  `fold.test_backtest_id = test.id` num segundo commit. Um worker morto **entre os dois** deixa
  uma corrida real, executada e sem dono — e o retry, vendo a dobra sem link, cria uma segunda.
  Não é regressão deste PR (a ordem é obrigatória: `test_backtest_id` é FK e o CHECK da dobra
  recusa corrida de teste sem escolha por trás) e o sintoma é uma linha a mais no run log, não um
  número errado no relatório. Consertar escrevendo as duas coisas na mesma transação, ou
  adotando a corrida órfã pelo par (estratégia escolhida, janela de teste) antes de criar outra.
- [origem: PR-204] **O portão de cobertura não enxerga o job que ele mais precisa medir** —
  `ci.yml:53` roda `uv run pytest` sem integração e é aí que mora o `--cov-fail-under=90`; a
  integração roda em `ci.yml:119` com `--no-cov`. Consequência estrutural: `worker.py` (30%),
  `routers/walkforwards.py` (47%), `studies.py`, `backtests.py` e `baskets.py` contam como
  descobertos mesmo com 103 testes de integração passando por eles, e a margem do portão vive
  em torno de 0,2 pp — o PR-204 furou o piso pela primeira vez e o conserto foi escrever teste
  sem banco, que é honesto mas não resolve a causa. Ver [[portao-de-cobertura-nao-ve-integracao]].
  Opções: combinar os dois relatórios (`coverage combine` entre os jobs) e medir o total, ou
  medir a integração com piso próprio. Não fazer dentro de um PR de produto.
- ~~[origem: PR-201]~~ **FEITO no PR-206-A.** A linha do builder virou uma união discriminada por
  forma (`comparison` | `between` | `trend`), o seletor de operador passou a oferecer os onze em
  três `optgroup`, e a completude é provada **no compilador** (`MissingOp extends never`) contra os
  tipos gerados do schema — um operador novo em Python que ninguém colocar num grupo faz o
  `builder.ts` parar de compilar. Originalmente: **`between`, `rising` e `falling` não são construíveis pela tela** — os três nós
  novos existem na DSL, na engine e no validador semântico, com teste de ouro cada um, mas a linha
  de condição do builder é `esquerda | operador | direita` e eles não cabem nela: `between` tem
  três operandos e `rising`/`falling` têm um mais um contador de barras. Reestruturar essa linha É
  o **PR-206** (builder visual de blocos), que o próprio `specs/fase-2.md` separa. Até lá são
  alcançáveis por JSON — o que serve para o motor e para a fixture, e não serve para ele. ⚠️ Ao
  fazer o PR-206, lembrar que `OPS` no `strategy/builder.ts` é hoje uma lista só de operadores
  binários: acrescentar os três ali **sem** mudar a linha produz documentos que o schema recusa.
- [origem: PR-201] **`stop_buffer_ticks` e `stop_buffer` convivem e ninguém explicou a diferença**
  — descoberto sondando o `setupSpec`: `mme9_breakout` e `ponto_continuo` têm `stop_buffer_ticks`
  (inteiro, 0 a 10000, default 0) e os dois setups de estrutura têm `stop_buffer` (número, 0 a 10,
  default 0,1, fração da largura da região). Nomes quase iguais, unidades diferentes, e a tela
  mostra os dois com o mesmo tipo de campo. Não é bug — é dois conceitos com um nome parecido. Vale
  renomear um dos dois ou fazer a dica dizer a unidade, no próximo PR que tocar a tela do builder.
- ~~[origem: PR-201, achado do guardian]~~ **FEITO no PR-201-B.** `_period_builder` e `_period_source_builder` levantavam
  `KeyError` cru** quando falta `params.period` (`indicators.py`), contra a promessa do módulo de
  entregar "uma frase, não um traceback" — o `build_indicator` ao lado já refuta tipo desconhecido
  e id ausente com `EngineError` legível. Não é regressão do PR-201: o `_period_source_builder`
  sempre fez isso, e o builder novo herdou o padrão. Alcançável só por documento que não passou
  pelo schema (a engine recebe mapping cru, por desenho). Consertar junto do próximo PR que tocar
  o registro de indicadores — que é o PR-201-B (Bollinger e ADX).
- [origem: PR-201, achado do guardian] **PARCIAL — `hypothesis` não cobre `ATR`/`Highest`/`Lowest`**
  (o PR-201-B cobriu Bollinger e ADX: bandas nunca se cruzam e aquecem juntas; `+DI + -DI <= 100`,
  que é o limite que pega denominador errado). Segue valendo para os três de PR-201-A. Originalmente: — existe
  property de aquecimento para o RSI e nenhuma para `ATR`/`Highest`/`Lowest`. As candidatas
  óbvias: o ATR nunca é negativo e nunca é menor que o maior `high-low` da janela dividido pelo
  período; o `Highest` é sempre >= ao `Lowest` do mesmo período; e ambos são `None` exatamente
  até a barra `period`. Barato, e pega a classe de erro que cenário escrito à mão não pega.

- [origem: PR-201-B] **O ADX não pertence ao painel de preço** — as três linhas vivem em 0–100 e o
  gráfico é de preço, então um `adx.adx` desenhado sai fora de escala e achata os candles.
  ⚠️ **Não é regressão deste PR: o RSI tem o mesmo problema desde que existe** (`INDICATOR_TYPES`
  sempre o ofereceu), e nenhuma estratégia dele desenha um até agora. O conserto é um painel
  separado por escala no `PriceChart`, ou uma regra de "indicadores limitados não vão no painel de
  preço". Enquanto isso, um Bollinger desenha certo porque é medido **em preço**.
- ~~[origem: PR-201-B]~~ **FEITO no PR-206-B2.** O `IndicatorForm` deixou de ter campos escritos à
  mão (`period`, `source`) e passou a guardar `values` por nome, dirigido pelo spec — o mesmo
  desenho que o `SetupForm` já usava. O leitor de parâmetros saiu de dentro do `setups.ts` para um
  `params.ts` compartilhado, então indicador e setup leem kind, limites, default e nulabilidade do
  mesmo lugar. Efeito colateral bom: sumiu o `if (takesSource)` na dobra — a chave errada não tem
  mais de onde vir. Originalmente: **O builder não tem controle para `deviations`** — a tela emite só
  `{period, source}`, então uma banda montada nela é sempre de 2,0 desvios. O documento é válido
  (o schema tem default 2.0), então não é bug: é um parâmetro inalcançável pela tela. O caminho é o
  mesmo do PR-206 (builder visual), que já precisa de trabalho no formulário de indicador.
- ~~[origem: PR-201-B]~~ **FEITO no PR-206-B2.** `readParams` ordena required primeiro para os
  dois, e como `period` é o único dos três do Bollinger sem default, isso sozinho já o traz para o
  topo. Originalmente: **`IndicatorSpec.params` vem em ordem alfabética** — é a ordem em que o JSON
  Schema lista `properties`, então um formulário que renderiza a lista em sequência mostra
  `deviations` **antes** de `period`. Passou desapercebido até agora porque `period` < `source` nos
  outros sete. Vale o mesmo tratamento que o `setupSpec` já tem (required primeiro), no PR que tocar
  o formulário de indicadores.
- ~~[origem: PR-201-B]~~ **FEITO no PR-206-B1.** Os campos de operando viraram picker: o catálogo
  oferece `price.*` e, por indicador declarado, `refsFor` decide a grafia — id nu para os
  single-valued, um item por componente para os compostos, e **nunca** o `bb` sozinho. Para isso as
  classes `Bollinger`/`ADX` passaram a publicar os componentes via `json_schema_extra`, e
  `COMPOSITE_COMPONENTS` deixou de ser um literal para ser **derivado da união** — o número de
  cópias em Python caiu de duas para uma, e os nomes chegam ao schema como **dado** em vez de
  prosa. Originalmente: **Referenciar um componente exige digitar `bb.upper` à mão** — os campos de
  operando do builder são texto livre, então funciona, mas nada oferece os três nomes nem avisa que
  `bb` sozinho é recusado. O erro que chega é legível (a camada semântica lista os componentes), o
  que torna isto atrito e não defeito. Some no PR-206.
- ~~[origem: PR-301-A] **O laço não seleciona símbolos no MarketWatch**~~ — **FECHADO no PR-301-B**:
  `LiveSource.subscribe` chama `symbol_select(symbol, True)` na subida e outra vez depois de cada
  reconexão (Market Watch é estado do terminal, e sessão reatada é conversa nova). Um símbolo que
  o terminal recusa agora para o watch com uma frase, em vez de virar silêncio permanente.
- [origem: PR-301-A] **Um candle é anunciado até `--every` segundos depois de fechar** — o MT5 não
  tem push, então o laço só percebe o fechamento no poll seguinte. Com o default de 5 s isso é 5 s
  no pior caso. Não é defeito (é a natureza do poll), mas é latência que a sessão de paper herda, e
  vale medir antes de alguém culpar a engine por "entrar tarde".
- [origem: PR-301-A] **Ninguém lê os streams ainda** — `candles.{symbol}.{tf}` é escrito e nenhum
  consumer group existe. Isso é o **PR-302** (PaperBroker). Enquanto isso os streams crescem até o
  `maxlen` de 10 000 e param, que é o desenhado.
- [origem: PR-301-A] **O D1 continua carimbado um dia adiantado** — a memória
  `d1-carimbado-um-dia-adiantado` mediu 499 de 499 batendo com o pregão do dia seguinte no
  backfill. ⚠️ **Ao vivo isto virou recusa no PR-301-B**: `tradeforge-collector live SYM D1` para
  com a frase e o motivo, porque o consumidor do stream é uma sessão de paper que não lê log.
  Falta o conserto de verdade — descobrir a fronteira do dia do broker e carimbar o D1 por ela,
  no backfill **e** ao vivo, e então soltar a recusa (`cli._UNSTAMPED_TIMEFRAMES`).
- [origem: PR-301-A] **O comando `live` é um processo de primeiro plano sem supervisão** — morre
  com o terminal que o iniciou e não volta sozinho. O 301-B tratou a reconexão *do MT5* e o
  buraco que um processo reiniciado deixa (ele retoma do rabo do stream), mas **reiniciar o
  processo** continua sendo trabalho de fora: pertence ao mesmo lugar que for cuidar do executor
  na fase 3.
- [origem: PR-232 / medição de 19/08] **O `CostModel` é um número por run, e o spread muda uma
  ordem de grandeza dentro de uma janela longa** — medido no EURUSD H1 deste broker: 11 pontos em
  2010, 1 ponto de 2020 em diante. Cobrando um valor só na janela padrão (16,6 anos no H1/H4/D1),
  o erro máximo é ~10 ticks, que contra a mediana de stop já medida (0,00116 = 116 ticks) é **~9%
  de 1R** por round trip. Não é ruído. Conserto: custo variável no tempo — um `CostModel` que
  aceite uma curva em vez de um escalar, ou custo lido da própria barra (`rates['spread']`, que
  existe e hoje é ignorado no backtest). ⚠️ Não é regressão de nada: é um limite que sempre
  existiu e que só ficou visível quando a janela passou a poder ser longa.
- [origem: PR-232 / medição de 19/08] **O piso de 2010 é do EURUSD deste broker e precisa ser por
  símbolo** — a fronteira "spread carimbado vs medido" foi medida num par de forex. Uma ação
  americana, um índice ou um cripto têm cada um a sua data (e alguns nem têm histórico antes de
  existirem). A sonda do PR-233 deve descobrir isso por símbolo em vez de assumir 2010; usar o
  número do EURUSD para tudo seria a mesma família de erro que este PR passou a sessão inteira
  consertando.
- [origem: PR-232 / medição de 19/08] **24 dos 84 símbolos deste broker não são classificáveis
  sozinhos** — `asset_class_from_path` conhece `forex/stocks/shares/indices/indexes/futures/crypto`,
  e as raízes reais aqui são `Forex` (60, OK), **`CFDs` (14)**, **`Crypto Currency` (7)** e
  **`Metals` (3)**. O collector **recusa em vez de chutar**, o que está certo — classe errada dá
  aritmética de tick errada e P&L errado por um fator constante, que é o bug que parece uma ótima
  estratégia — mas significa que o botão "coletar" do **PR-234 falha em 29% dos símbolos**.
  Duas saídas: (a) alargar o mapa, barato e envelhece a cada corretora nova; (b) a tela
  **perguntar** a classe quando o path não decide, reusando a recusa que já existe. (b) parece
  melhor: transforma um erro de CLI num campo de formulário, e não finge saber o que não sabe.
- [origem: PR-232 / coleta de 19/08] **O BTCUSD do broker fecha no fim de semana** — 216 dos 234
  gaps do H1 são de fim de semana. Cripto negocia 24/7 no mundo real, então o que está sendo
  testado é o **CFD do broker sobre o bitcoin**, não o bitcoin. Não é bug de nada; é uma
  propriedade do instrumento que a tela deveria dizer antes de alguém tirar conclusão de um
  backtest de cripto. Candidato a sair junto do PR-233, que já vai olhar a forma do histórico.
- [origem: PR-232] **`pytest -m integration` apaga o snapshot de símbolos** — `broker_symbols`
  entrou na lista de TRUNCATE dos dois conftest, porque um teste que sincroniza vazaria linhas
  para o próximo. O efeito colateral é operacional: rodar a suíte de integração deixa a busca da
  tela vazia até alguém apertar *sync from MT5*. Some quando existir banco de teste separado —
  ver [[integracao-apaga-o-banco]], que é o mesmo problema com seis tabelas a menos.
- [origem: PR-232] **O agente do host não tem supervisão** — mesmo problema do comando `live`
  (origem PR-301-A): morre com o terminal que o iniciou e não volta sozinho. Agora são **dois**
  processos que o Guilherme precisa manter no ar no Windows, o que reforça o caso de resolver
  isso de uma vez (serviço do Windows, ou um supervisor).
- [origem: PR-232] **Dois terminais MT5 instalados continuam sem escolha** — `mt5.initialize()`
  é chamado sem argumento, então "trocar de corretora pela tela" ainda depende de qual terminal
  o Windows resolve primeiro. Já estava anotado desde o PR-223; agora tem consequência visível,
  porque o botão *sync from MT5* fotografa o catálogo de um terminal que o usuário não escolheu.
- [origem: PR-232] **A busca é por prefixo, e `usd` não acha `EURUSD`** — decisão consciente (um
  ticker é lembrado pela frente, e substring enterraria AAPL sob tudo que tem essas letras no
  meio), mas é atrito real para quem procura por moeda de cotação. Conserto candidato: casar
  prefixo **primeiro** e substring depois, na mesma resposta, com os do prefixo no topo.
- [origem: PR-301-B] **Terminal ligado mas deslogado do broker não é detectado** — `terminal_info()`
  devolve objeto (não `None`), então o laço não vê `ConnectionError`; o `copy_rates_from_pos`
  responde com histórico em cache que parou de avançar, e isso é **indistinguível de mercado
  parado**. `terminal_info().connected` diz a verdade, mas separar "parou" de "está quieto" exige
  estado ao longo do tempo — pertence a quem supervisiona o laço, não a uma chamada dentro dele.
- [origem: PR-301-B] **O teto do gap-fill é por barras, não por relevância** — `--max-backfill`
  default 500. Um outage maior que isso é anunciado com o intervalo que ficou faltando e o
  operador tem de rodar um `backfill`; nada automatiza essa costura entre o Parquet e o stream.
- [origem: PR-301-B] **A reconexão remede o relógio do servidor** — `connect()` chama
  `_measure_offset` quando o offset não foi declarado, e com mercado fechado isso é recusado (é
  o desenho). Consequência operacional: **um `live` que deve sobreviver a um fim de semana
  precisa subir com `--server-offset`**, senão o terminal pode voltar são e a reconexão continuar
  falhando, corretamente, até a abertura.
- [origem: PR-206-C2] **Uma ref pendurada aparece na lista mas não é editável** — o picker não tem
  mais caixa de texto (as quatro formas da gramática viraram controles), então uma ref que deixou
  de ser ofertável — porque o indicador foi renomeado — é acrescentada à própria lista para ficar
  visível. Dá para trocá-la por qualquer opção válida, não dá para corrigir a digitação dela. É
  o comportamento certo (a única correção de uma ref morta é escolher uma viva), mas se ele
  incomodar, o caminho é renomear a ref junto com o indicador em vez de devolver a caixa.
- [origem: PR-206-C2] **Não há como reordenar nem mover um nó entre grupos** — dá para adicionar,
  negar e remover, e é só. Arrastar um nó para dentro de um grupo (ou para fora) é o que falta
  para o builder ser "visual" no sentido pleno do `specs/fase-2.md`. Não bloqueia nada: a mesma
  árvore é construível adicionando na ordem certa.
- [origem: PR-206-C1] **A tela carrega três campos que não mostra** — `description`,
  `max_open_positions` e `max_daily_loss_percent` agora sobrevivem ao round-trip (abrir e salvar
  não os apaga), mas **nenhum controle os edita**. Foi escolha de escopo: sem eles no formulário o
  round-trip perderia dado de documentos reais, e com controle para eles o PR viraria outro. Três
  campos de texto no bloco de risco resolvem.
- [origem: PR-206-C1] **Nada leva o usuário à rota `/strategies/:id`** — ela existe e funciona, e
  só se chega nela digitando a URL. O `useStrategies` (lista) já existe desde o PR-104. Falta o
  link, que é meia hora e provavelmente pertence ao PR que fizer a tela de lista de estratégias.
- ~~[origem: PR-206-C1]~~ **FEITO no PR-206-C2.** `SideForm.rows` virou `ConditionNode[]`, com
  `group` e `not` como nós, e o editor virou recursivo. A fixture `nested_logic` saiu da lista de
  recusa e entrou na de round-trip byte a byte — que é o **aceite do PR-206 no spec, e o fim da
  fase 2**.
- [origem: PR-206-C1] **Abrir e salvar `setup_structure_continuation_defaults` escreve os
  parâmetros que o documento omitia** — não é perda (é a mesma estratégia, soletrada), e é ponto
  fixo a partir do segundo salvamento, o que está preso por teste. A causa é que o formulário tem
  **um** estado vazio por campo e ele significa "não definido"; o documento distingue "ausente" de
  "desligado" e o formulário não. Resolver de verdade exigiria um terceiro estado por campo —
  registrar antes de alguém "consertar" isso sem saber o que está trocando.
- [origem: PR-206-B2] **Adicionar um indicador não pré-preenche mais o `period`** — não é defeito,
  é a consequência de a tela ler o schema: `period` não tem default no Pydantic, então a caixa
  nasce vazia e o botão de rodar fica desabilitado até alguém escolher a janela. O `14` que
  aparecia antes era um número escrito no TypeScript — um SMA(14) que ninguém escolheu. Se em
  algum momento isso incomodar, a correção certa é **dar um default ao `period` no Pydantic**, não
  reescrever o número na tela. Anotado para o caso de ele reclamar do atrito.
- [origem: PR-206-B2] **`stop_buffer_ticks` vs `stop_buffer` continua aberto** — este PR tocou a
  tela do builder e **não** resolveu (um PR, um escopo). Segue valendo: nomes quase iguais,
  unidades diferentes, e a tela mostra os dois com o mesmo tipo de campo.
- ~~[origem: PR-206-B1]~~ **FEITO no PR-206-C2.** A entrada "a closed candle" no picker abre um
  número de barras atrás e um seletor de campo, exatamente como estava previsto — e com isso a
  **caixa de texto livre sumiu do picker**: as quatro formas da gramática de ref são controles
  agora. O `CANDLE_PATTERN` é montado a partir de `SOURCES`, então a lista de campos existe uma
  vez só. Originalmente: **`candle[-N].field` só é alcançável digitando, atrás do `custom…`** — é a
  única das quatro formas da gramática de ref que **não é enumerável**, porque N não tem teto.
- [origem: PR-201-B, achado do guardian] **O motor não recusa um indicador declarado com `id: "price"`
  ou `"candle"`** — o schema recusa (`RESERVED_IDS` em `semantic.py`), então é inalcançável pela API,
  mas num mapping direto o canal nasce e fica **permanentemente inalcançável**: o
  `compile_operand("price.close")` casa `_PRICE_REF` primeiro e o indicador declarado desaparece sem
  uma palavra. Mesma família do `price.clsoe` que este PR fechou, e alcançabilidade idêntica (a
  engine aceita mapping cru por desenho). O conserto é uma linha em `compile_strategy`, junto do
  `duplicate indicator id` que já está lá. Fora de escopo aqui porque não é regressão do PR-201-B.
- [origem: PR-201-B, achado do professor, **verificado**] **O motor não recusa um componente que não
  existe** — `compile_strategy` aceita `{"ref": "bb.uppper"}` sobre um `bb` do tipo BOLLINGER sem
  levantar nada, e o canal resolve para nulo em toda barra. A camada semântica pega isto para tudo
  que entra pela API, e a trava nova do `compile_operand` só cobre as cabeças **reservadas**
  (`price`/`candle`), não um componente errado de indicador declarado. Alcançável só por mapping cru
  — mesma classe do item do `id: "price"` acima, e mesma família do `price.clsoe` que este PR fechou.
  ⚠️ O conserto é barato e o material já existe: no `compile_strategy` os canais estão montados
  **antes** de as condições serem compiladas, então dá para passar o conjunto de canais válidos ao
  `compile_operand` e recusar um ref de indicador que não seja um deles — o que de quebra pega
  `{"ref": "sma_lenta"}` para uma `sma_lento` declarada, que hoje também é nulo silencioso. Fora de
  escopo aqui: não é regressão do PR-201-B, e um PR = um escopo.
- [origem: PR-234, **medido em 20/08/2026**] **O catálogo e o disco já divergiram na máquina do
  Guilherme**: `select count(*) from datasets` devolve **0** enquanto `data/ohlcv` tem **42 arquivos
  Parquet** de AAPL, BTCUSD, EURUSD, GBPJPY e GBPUSD. As linhas se perderam numa rodada de
  `pytest -m integration` (que TRUNCA `datasets`), e o efeito é que a tela de backtest não oferece
  nada apesar de os dados estarem lá. O PR-234 impede que isso volte a acontecer *por recoleta*
  (`storage.coverage` lê o disco), mas **não repara o que já se perdeu**. Falta um `tradeforge-db
  reconcile` que varra `data/ohlcv` e reescreva `instruments`/`datasets` a partir dos arquivos — o
  mesmo `coverage()` já faz metade do trabalho.
- [origem: PR-234] **A suíte de integração continua sem banco próprio**, e agora está provado que dá
  para ter um: `POSTGRES_DB=tradeforge_pr234 uv run pytest -m integration` roda os 158 testes
  inteiros contra um banco descartável, sem tocar nos dados de trabalho. O conserto é fazer disso o
  default do `conftest` (criar/derrubar o banco na fixture de sessão) em vez de depender de quem
  roda lembrar da variável. Enquanto não for, o TRUNCATE continua sendo uma armadilha carregada.
- [origem: PR-234] **O progresso da coleta é lido por polling e não pelo pub/sub** que o spec
  previa. A decisão é medida: uma coleta emite no máximo um evento por ano de calendário — cinco ou
  dez no total — e o `GET /collections/{id}` a cada 3 s cobre isso sem WebSocket novo. Publicar num
  canal sem assinante seria código morto, que é exatamente o defeito que o PR-233 fechou. Se o
  progresso um dia ficar mais fino que "um ano", o `ws.py` já tem o formato para copiar.
- [origem: PR-234] **`BARS_PER_YEAR.M30` é interpolado, não medido.** Os outros sete números vieram
  de contagem real no EURUSD 2024; o M30 foi posto entre M15 e H1 porque ninguém contou. Está
  marcado no código. Uma medição de um minuto resolve, e enquanto não vier a janela sugerida de M30
  é a única que não tem lastro.
- [origem: PR-234] **Nada reconcilia `datasets` quando um arquivo Parquet é apagado à mão.** A
  cobertura é derivada do disco *na coleta*, então apagar um ano depois deixa a linha alegando mais
  do que existe até a próxima recoleta. O mesmo `reconcile` do primeiro item resolve os dois.
- ~~[origem: aceite do PR-234, **medido em 20/08/2026**]~~ **FEITO no PR-235.** ⚠️ **A invariante
  `net_profit = gross_profit + gross_loss` é verdadeira quando calculada e falsa depois de
  gravada, e derruba o backtest.** O
  CHECK `ck_backtest_metrics_net_profit_balances` recusou uma linha cujos três números fechavam em
  `Decimal`: as parcelas atravessam para `NUMERIC(20,8)` **cada uma por si**, e a soma dos
  arredondados não é o arredondado da soma. Medido no backtest do AUDCAD H1 coletado pela tela:

  ```
  em Decimal   -9944.7976321644 == 802.6520847921 + (-10747.4497169565)   ✅
  em NUMERIC   -9944.79763216   ≠  802.65208479   + (-10747.44971696)
                                 = -9944.79763217                          ❌  (1 na 8ª casa)
  ```

  ⚠️ **É intermitente**, que é o pior modo de falha possível: com números cujos arredondamentos
  concordam a linha grava normal, e foi por isso que todos os backtests anteriores passaram. O
  conserto é em `packages/db/src/tradeforge_db/results.py:176` (`_metrics_row`) e **não toca a
  engine**: quantizar as parcelas para escala 8 e derivar `net_profit` da soma **das gravadas**, de
  modo que as partes continuem sendo a verdade e o total siga elas. Afrouxar o CHECK para uma
  tolerância está descartado — ele existe para pegar erro de sinal em `gross_loss`, e trocá-lo por
  tolerância é trocar falha barulhenta por falha silenciosa. Aumentar a escala não resolve: o
  desencontro reaparece na última casa, qualquer que seja ela. O teste precisa **reler do banco**,
  porque `Decimal` compara numericamente e a perda de quantização só aparece no fio.
  Mesma família, **verificada e consertada junto no PR-235**: `ck_trades_net_pnl_balances`
  (`net_pnl = gross_pnl - costs`) tem o mesmo buraco. Não apareceu em 1.200 combinações de
  `tick_value` × spread varridas com a engine de verdade, mas é alcançável — basta o custo cair na
  fronteira de arredondamento. É o mesmo bug, só mais raro.
- [origem: aceite do PR-234] **A metade "rodar um backtest nele" do aceite ficou pendente** por
  causa do item acima, que é anterior ao PR-234 e independe dele. A metade da coleta fechou: AUDCAD
  H1 coletado pela tela em 7 fatias (2020 → 2026), 41.216 candles, 358 gaps, uma linha em
  `datasets` e 7 partições no disco. Refazer o backtest do AUDCAD assim que o CHECK for consertado.

- [origem: PR-235] **Ninguém reconcilia `gross_profit` com a soma dos trades gravados.** Depois do
  PR-235 cada linha fecha **consigo mesma**, mas `backtest_metrics.gross_profit` é o arredondado da
  soma dos `net_pnl` **não arredondados**, enquanto a tabela `trades` guarda os arredondados. Somar
  a coluna `net_pnl` dos vencedores pode dar alguns 1e-8 de diferença do total do sumário. Nenhum
  CHECK amarra os dois, então isso só morde quem conferir uma tabela contra a outra — provavelmente
  a tela de resultados da fase 3. Consertar exigiria escolher **qual** das duas é a verdade, e essa
  escolha é de produto, não de encanamento: o total do sumário é o mais próximo do que a conta
  realmente fez, e a soma dos trades é a que o usuário consegue conferir na mão.

- [origem: uso real da coleta multi-ativo, **medido em 21/08/2026**] ⚠️ **O `job_timeout` do agente
  é inerte, e o laço de eventos fica bloqueado durante a coleta inteira.** `WorkerSettings` não
  declara `job_timeout`, então vale o default do arq 0.28 — **300 s**, com `max_tries=5` e
  `retry_jobs=True`. Nenhum desses três números foi escolhido por alguém.

  **Mas trocar o número não resolve nada**, e essa é a parte que importa: o arq aplica o limite com
  `asyncio.wait_for`, que só cancela num ponto de `await`. `run_collection` é **síncrona**
  (`collect.py:160`) e `collect_range` a chama direto (`agent.py:220`), então o job bloqueia o
  event loop do começo ao fim e o timeout nunca chega a disparar.

  **A prova está no próprio Redis** — os dois heartbeats lado a lado, mesma instância:

  ```
  collect:health-check     Aug-21 14:29:29  j_ongoing=1  queued=7   <- agente coletor, parado
  arq:queue:health-check   Aug-21 17:13:44  j_ongoing=0  queued=0   <- worker da API, atual
  ```

  O agente não consegue escrever o próprio batimento enquanto trabalha. Consequências medidas:

  1. **A chave `in-progress` expira com o job ainda rodando.** O arq a define como
     `job_timeout + 10` = 310 s (observei TTL 232 e 206 contando de 310). Uma coleta mais longa que
     isso perde o próprio cadeado.
  2. **Com `max_jobs = 1`, um job travado congela a fila inteira** — e a tela não sabe dizer isso.
     As linhas ficam `queued`, que é indistinguível de "o agente não está rodando". São dois
     estados com remédios opostos: um se resolve subindo o agente, o outro esperando ou matando o
     processo. O docstring de `Collection` diz que a linha existe justamente para separar esses
     dois casos — o modelo previu o problema, faltou o agente publicar que está vivo.
  3. **300 s é curto e longo ao mesmo tempo.** Para uma coleta é curto (um ano frio de M1 são
     ~368 mil barras); para uma conexão no terminal errado é longo. São dois números diferentes
     hoje colapsados num só que ninguém escolheu.

  **Conserto, na ordem:** (a) `await asyncio.to_thread(run_collection, ...)` em `collect_range` —
  só depois disso o timeout passa a significar alguma coisa e o heartbeat volta a bater; (b) então
  escolher `job_timeout` de propósito, e provavelmente `max_tries` também, porque recoletar é
  idempotente mas ressondar custa 207 s; (c) a tela ler `collect:health-check` para distinguir
  "enfileirado atrás de outro job" de "ninguém está drenando". ⚠️ Um heartbeat velho **não**
  distingue "morto" de "bloqueado" — mas `j_ongoing=1` ao lado diz que ele parou trabalhando, que
  já é mais do que a tela mostra hoje.

  Descoberto porque havia **dois MT5 abertos** e o agente anexou ao errado — ver o item do
  `--terminal-path`, que agora tem uma ocorrência real a favor.

- [origem: subir o agente após o upgrade de Redis 8 / Postgres 18, **medido em 24/08/2026**]
  ⚠️ **O heartbeat do agente é reescrito uma vez por hora, então não distingue "vivo" de
  "travado" dentro dessa hora.** Irmão do item do `job_timeout` acima, e o conserto cabe na
  mesma passada.

  `WorkerSettings` não declara `health_check_interval`, então vale o default do arq: **3600 s**.
  Medido logo após subir o agente — a chave `collect:health-check` ficou com o mesmo carimbo em
  três amostras a 12 s de intervalo, e o `TTL` estava em 3546 contando de 3600:

  ```
  [1] Aug-24 17:09:55 j_complete=0 j_failed=0 j_retried=0 j_ongoing=0 queued=0
  [2] Aug-24 17:09:55 ...   (12 s depois)
  [3] Aug-24 17:09:55 ...   (24 s depois)
  ```

  **Por que isso importa mais do que parece.** O docstring de `Collection` diz que a linha existe
  para separar "enfileirado atrás de outro job" de "ninguém está drenando". O heartbeat é o sinal
  que deveria responder isso, e com uma hora de granularidade ele responde tarde demais para ser
  útil: um agente que morreu às 14h05 continua com um carimbo plausível até as 15h.

  ⚠️ E ele engana ativamente. Amostrando por 24 s eu li "o horário não anda" e quase declarei
  travamento num agente perfeitamente saudável — que é o falso positivo exato que o item do
  `job_timeout` pede para a tela evitar. Um sinal de vida que só é verdadeiro na escala de horas
  é pior do que nenhum, porque parece um sinal de vida.

  **Conserto:** declarar `health_check_interval` de propósito no `WorkerSettings` — algo na
  ordem de 15-30 s, que é barato (uma escrita de chave) e torna o carimbo legível como
  liveness. Fazer junto de (a) `await asyncio.to_thread(run_collection, ...)` e (b) a escolha
  deliberada do `job_timeout`: os três são a mesma pergunta — *como alguém sabe que o agente
  está vivo e trabalhando?* — e consertar um sem os outros deixa a resposta pela metade.

  Não é regressão do upgrade: o default sempre foi 3600. Só ninguém tinha olhado.

---

## `ix_live_sessions_heartbeat_at` não é usado por consulta nenhuma

Achado ao consertar o buraco de teste do `reconcile_stale` (fix do PR-302-C-B). O comentário da
migration que cria o índice diz que a reconciliação lê essa coluna. Ela não lê — não em SQL.

`running_sessions` emite exatamente:

```sql
SELECT ... FROM live_sessions WHERE status = 'running' ORDER BY started_at
```

e o predicado de silêncio (`is_stale`) roda **em Python**, sobre as linhas já trazidas. Nenhum
plano de consulta no repo toca `heartbeat_at` num `WHERE` ou num `ORDER BY`. O índice é peso morto:
custa uma escrita a cada batida — que é o caminho mais quente do módulo, uma vez a cada
`BEAT_EVERY` por sessão viva — e não acelera leitura nenhuma.

**Duas saídas, e elas discordam de verdade:**

1. **Apagar o índice** e corrigir o comentário da migration. A reconciliação continua em Python,
   onde a decisão é testável sem banco (foi exatamente esse o argumento do `is_stale`).
2. **Descer o filtro para o SQL**: `running_sessions` passa a receber o corte e emite
   `AND COALESCE(heartbeat_at, started_at) < :cutoff`. O índice passa a ser usado e o banco
   devolve só as linhas mortas.

⚠️ O (2) tem um custo que não é óbvio: move a fronteira do off-by-one para dentro de uma string
SQL, onde o teste de milissegundos não alcança — e o portão de cobertura deselecciona integração.
Foi essa exata configuração que deixou o mutante `<` → `<=` vivo. O (2) só vale se a tabela crescer
a ponto de trazer todas as `running` para o Python ficar caro, e hoje ela tem unidades de linhas.

**Inclinação: (1).** Mas é decisão dele, não minha.

---

## A suíte de integração não precisa apagar o banco de trabalho

`packages/db/tests/conftest.py` faz `TRUNCATE ... RESTART IDENTITY CASCADE` em **11 tabelas** do
banco que o `PostgresSettings` apontar, e o default é `tradeforge` — o de trabalho. Isso já apagou
os backtests dele uma vez (04/08).

Não precisa ser assim. O `postgres_db` é override por ambiente, então:

```bash
POSTGRES_DB=tradeforge_test uv run pytest -m integration --no-cov
```

roda a suíte inteira contra um banco descartável. O fixture `migrated_engine` já chama
`upgrade("head")`, então o schema se cria sozinho; só o `CREATE DATABASE` precisa existir. Provado
em 25/08: 206 de integração verdes contra `tradeforge_test`, com os 24.001 trades do `tradeforge`
conferidos intactos antes e depois.

**Conserto:** fazer disso o caminho padrão em vez de uma coisa que se lembra na hora. Opções — um
default de `tradeforge_test` no fixture `dsn` (com o `TRUNCATE` recusando-se a rodar contra um
banco cujo nome não termine em `_test`), ou um serviço postgres separado no compose. A guarda
importa mais que o default: um default é uma sugestão, e o modo de falha aqui é perda de dados
silenciosa.

---

## Uma ordem limite que preenche depois não tem quem avise a sessão

Descoberto medindo o terminal em 26/08 (`fix/pr-304-placed-is-not-filled`). O executor trata **uma
entrada da fila = uma decisão**: recebe a ordem, manda, grava, publica o fill se houve, dá o ack.
Isso fecha certo para ordem a mercado, onde o negócio acontece dentro da chamada.

Para uma **limite**, não fecha. A colocação volta `retcode=10009` com `deal=0` — aceita e parada no
book — e agora o executor corretamente **não publica fill nenhum**. Só que o preenchimento de
verdade acontece minutos ou horas depois, e nesse momento **não há ninguém naquele laço olhando**:
a entrada já foi acked e o executor já esqueceu a ordem.

Ou seja: hoje uma sessão que arma limite (que é o que a estratégia SMC faz — ADR-0015) abre
posição no venue e **nunca fica sabendo**. O ledger do broker e a conta divergem em silêncio, que é
o pior modo de falha possível para dinheiro real.

**Conserto (candidato a PR-304-A3):** um laço próprio no executor que varre
`history_deals_get(magic=770302)` desde o último deal visto e publica em `fills.inbound` o que
apareceu, correlacionando pelo `client_id` no comment da ordem. É um segundo produtor da mesma
stream, com a mesma regra de "publica só o que tem `deal`".

⚠️ Enquanto isso não existe, **o executor só é honesto com ordem a mercado**. Uma sessão live com
limite armada não deve subir.

---

## `Broker.cancel` e `Broker.modify_stop` não têm fio

`orders.outbound` carrega `OrderRequest`, e o `__post_init__` recusa `CANCEL` e `MODIFY_STOP` — de
propósito, e certo: um cancelamento não é uma ordem. A consequência é que metade do protocolo
`Broker` não tem como chegar ao executor, e as duas metades que faltam são justamente as que a
estratégia SMC usa (desarmar a limite quando a região morre, apertar o stop na condução — ADR-0018).

**Conserto (candidato a PR-304-A3):** mensagem própria no fio, com `kind` explícito, não um
`OrderRequest` alargado. Alargar a gramática abre o buraco do outro lado.

---

## O executor não confere `terminal_info().trade_allowed` na subida

Medido em 26/08: com o botão AutoTrading do terminal desligado, **toda** ordem volta
`accepted=False`, `retcode=10027`, e nada chega ao mercado. Falha fechada — mas em silêncio: a
sessão roda o dia inteiro recusando ordens com uma mensagem plausível, e nenhum teste ponta a ponta
contra o venue prova coisa nenhuma. `account_info().trade_allowed` continua `True` e engana.

**Conserto:** o executor confere na subida e recusa subir armado com o botão desligado.

---

## O `MT5Broker` existe e ainda não está ligado em lugar nenhum

Deliberado, e a razão é o item de cima. `session.py` continua construindo dois `BacktestBroker`
(aquecimento e sessão), que é o modo paper. Ligar o `MT5Broker` numa sessão de verdade hoje seria
irresponsável enquanto:

1. **`cancel` e `modify_stop` não têm fio** — o broker levanta `EngineError` nos dois em vez de
   devolver `False`, então uma estratégia que arma limite ou conduz stop **para a sessão** em vez
   de mentir. Isso é o comportamento certo, e também significa que só uma estratégia puramente a
   mercado roda ponta a ponta.
2. **Uma limite que preenche depois não tem produtor** (item acima).
3. **Não existe reconciliação** entre o ledger do broker e a conta no venue.

Ligar é trabalho do **PR-304-B** (a trava de promoção paper→real), que é onde a decisão "esta
sessão pode usar o venue" ganha dono.

---

## O `_sent` do `MT5Broker` vive em memória

Um `dict[client_id, OrderRequest]`, e é a outra metade de todo fill. Uma sessão que morre segurando
uma ordem volta sem saber dela: o fill chega, o `_request_for` não acha o pedido e **levanta** — que
é o certo (um fill que não se consegue atribuir é uma posição real que o ledger vai errar), mas é
uma parada dura numa situação recuperável.

**Conserto (PR-304-A4, junto da reconciliação):** as ordens já estão em `order_audit`, indexadas por
`client_id`. Uma sessão subindo pode reconstruir o `_sent` de lá — e a mesma leitura serve para
comparar o que o ledger acha que tem com o que o venue diz que tem.

---

## O executor precisa conferir sozinho que um stop está apertando

Ficou de fora do conserto de `reduces_risk` (`fix/pr-303-a2`), de propósito. Um stop que **aperta**
reduz risco e deveria passar pelos portões como a saída e o cancelamento passam. Só que quem afirma
que ele aperta é a sessão, três processos longe — e um erro de sinal lá chegaria aqui com a cara
exata de um aperto, e seria passado por cima de todos os limites.

A engine já garante isso do lado dela (`Broker.modify_stop` levanta num afrouxamento), mas
**esta máquina não pode aceitar a palavra da sessão** — é o mesmo princípio que faz os limites
viverem no executor e não no core.

**Conserto (PR-304-A3, junto do fio):** antes de admitir um `MODIFY_STOP`, o executor lê a posição
no venue (`positions_get` filtrado por magic) e compara a direção ele mesmo: para um long o nível
novo tem que ser >= o atual, para um short <=. Aí `MODIFY_STOP` entra no `reduces_risk`.

⚠️ Enquanto isso, um aperto de stop está sujeito a todos os limites — inclusive ao kill switch, o
que é conservador na direção errada, mas é a direção segura enquanto não dá para verificar.
