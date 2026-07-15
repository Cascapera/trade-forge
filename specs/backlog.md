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
