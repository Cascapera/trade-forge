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
- [origem: PR-108] **Gráfico de candles com entradas/saídas** — a tela de resultados entrega cards +
  curva de capital + tabela de trades, mas NÃO o gráfico de candles com marcadores de entrada/saída
  (lightweight-charts). Falta o dado: OHLCV mora em Parquet (ADR-05), não no Postgres, e não há endpoint
  que o sirva. Fatia seguinte: `GET /instruments/{symbol}/candles?tf&from&to` na API (lê via
  `read_candles`, pagina/decima) + a série de candlestick na UI com os trades plotados. Escopo próprio.
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
  do MT5, mas não "simplificar" o contador no futuro.
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
