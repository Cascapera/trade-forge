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
