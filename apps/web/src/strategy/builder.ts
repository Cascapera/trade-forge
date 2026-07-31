// The guided builder's pure core: a flat, form-shaped model and the function that folds it into
// a DSL `Strategy` document. Kept free of React so it can be unit-tested exhaustively — the part
// with the real logic (a single condition collapses to a bare comparison; a group becomes
// `all`/`any`; an empty side becomes `null`) is proven here, and the form component just edits
// the model and renders whatever `buildStrategy` produces.
//
// The DSL types come from `@tradeforge/schema` (generated from the shared JSON Schema), never
// hand-written. The runtime option lists below are checked against those types with `satisfies`,
// so an invalid value (a timeframe the schema does not know) is a compile error.

import { SETUP_TYPES, setupSpec, type SetupType } from '@tradeforge/schema'
import type { Comparison, ComparisonOp, Condition, Strategy, Timeframe } from '@tradeforge/schema'

export const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'] as const satisfies
  readonly Timeframe[]

export const OPS = [
  'gt',
  'lt',
  'gte',
  'lte',
  'crosses_above',
  'crosses_below',
  'breaks_above',
  'breaks_below',
] as const satisfies readonly ComparisonOp[]

export const SOURCES = ['open', 'high', 'low', 'close'] as const
export type Source = (typeof SOURCES)[number]

export const INDICATOR_KINDS = ['SMA', 'EMA', 'RSI'] as const
export type IndicatorKind = (typeof INDICATOR_KINDS)[number]

export type Combine = 'all' | 'any'

// The right-hand operand is either another reference (`fast`, `price.close`) or a literal number
// (the `30` in `rsi < 30`). The form keeps `right` a string in both cases and this flag decides
// how it is folded — a `{ ref }` or a `{ value }` — so RSI thresholds become expressible.
export type OperandKind = 'ref' | 'value'

export interface IndicatorForm {
  id: string
  kind: IndicatorKind
  period: number
  source: Source
}

export interface ConditionRow {
  left: string
  op: ComparisonOp
  right: string
  rightKind: OperandKind
}

export interface SideForm {
  enabled: boolean
  combine: Combine
  rows: ConditionRow[]
}

export interface StopForm {
  enabled: boolean
  lookback: number
  side: 'low' | 'high'
}

export interface TakeProfitForm {
  enabled: boolean
  rr: number
}

/**
 * A document describes its strategy in one of two ways, and the form follows it.
 *
 * `conditions` is the tree the builder has always produced. `setup` *names* a state machine the
 * DSL cannot describe — which order is resting, whether this turn already gave its trade, how many
 * breaks of structure the position has seen — and the two cannot be combined: the semantic layer
 * refuses a document carrying both, because a setup owns its own entry and its own stop, and a
 * second opinion has no arbiter.
 */
export type BuilderMode = 'conditions' | 'setup'

/**
 * The named setup and the values typed into its fields.
 *
 * Values are kept as the form holds them — strings, or booleans for flags — rather than parsed,
 * because an empty box is a meaningful state that a number cannot represent. What it means is
 * decided per parameter by the schema: an empty *nullable* field is the rule being switched off
 * (`breakeven_at_r: null` asks what the setup earns without taking winners to breakeven,
 * `max_bos: null` means uncapped), and any other empty field is simply left out, so the engine
 * class's own default applies. Neither is the same as sending a zero.
 */
export interface SetupForm {
  type: SetupType
  values: Record<string, string | boolean>
}

export interface StrategyForm {
  name: string
  timeframe: Timeframe
  mode: BuilderMode
  setup: SetupForm
  indicators: IndicatorForm[]
  long: SideForm
  short: SideForm
  stop: StopForm
  takeProfit: TakeProfitForm
  exit: SideForm
  percent: number
}

function comparison(row: ConditionRow): Comparison {
  const right = row.rightKind === 'value' ? { value: Number(row.right) } : { ref: row.right }
  return { op: row.op, left: { ref: row.left }, right }
}

/** A side's condition, in the DSL's shape: `null` if empty, a bare comparison if there is one
 *  row, an `all`/`any` group if there are several. */
export function buildCondition(side: SideForm): Condition | null {
  if (!side.enabled) return null
  const comparisons = side.rows.map(comparison)
  if (comparisons.length === 0) return null
  if (comparisons.length === 1) return comparisons[0] as Condition
  const group = comparisons as [Condition, ...Condition[]]
  return side.combine === 'all' ? { all: group } : { any: group }
}

/**
 * A document that describes its strategy as conditions.
 *
 * `entry` and `exit` are optional on `Strategy` because a document may instead *name* a setup —
 * a state machine the DSL cannot describe, only reference (ADR-0019) — and such a document
 * carries neither. This builder makes only the condition kind, so it always carries both, and
 * saying so here is what lets callers read `document.entry.long` without a null check for a
 * value that is never null. When the builder learns to emit setups, this type is where the
 * change starts.
 */
export type ConditionStrategy = Strategy & {
  entry: NonNullable<Strategy['entry']>
  exit: NonNullable<Strategy['exit']>
}

/** A document that names a setup. The counterpart of `ConditionStrategy`. */
export type SetupStrategy = Strategy & { setup: NonNullable<Strategy['setup']> }

/**
 * The values a fresh form starts with: every parameter pre-filled with the number the schema
 * declares, which is the engine class's own default (ADR-0019).
 *
 * A parameter with no default starts empty on purpose. `side` is the one that matters: the engine
 * classes default it to long so a hand-built strategy in a test has somewhere to start, but the
 * schema makes it required, and a form that pre-selected "long" would turn a forgotten choice into
 * an entire long-only backtest read as the setup's result.
 */
export function setupValues(type: SetupType): Record<string, string | boolean> {
  const values: Record<string, string | boolean> = {}
  for (const param of setupSpec(type).params) {
    values[param.name] = param.kind === 'boolean' ? param.default : (param.default?.toString() ?? '')
  }
  return values
}

/** Fold the typed values back into the document's `params`, per the rules in `SetupForm`. */
function setupParams(form: SetupForm): Record<string, unknown> {
  const params: Record<string, unknown> = {}
  for (const param of setupSpec(form.type).params) {
    const raw = form.values[param.name]
    if (param.kind === 'boolean') {
      params[param.name] = raw === true
      continue
    }
    const text = typeof raw === 'string' ? raw.trim() : ''
    if (text === '') {
      if (param.kind !== 'enum' && param.nullable) params[param.name] = null
      continue
    }
    params[param.name] = param.kind === 'enum' ? text : Number(text)
  }
  return params
}

/**
 * A document that names a setup.
 *
 * It carries only what was never the strategy's to decide: the account's risk and the broker's
 * target. No indicators, no entry conditions, and no stop — the setup declares its own indicators,
 * *is* the entry, and places its stop from the bar it entered on. The semantic layer refuses a
 * setup document that carries any of them, so the form does not offer them either.
 */
export function buildSetupStrategy(form: StrategyForm): SetupStrategy {
  return {
    schema_version: '1.0',
    name: form.name,
    timeframe: form.timeframe,
    setup: {
      type: form.setup.type,
      params: setupParams(form.setup),
    } as SetupStrategy['setup'],
    exit: {
      stop_loss: null,
      take_profit: form.takeProfit.enabled
        ? { type: 'risk_multiple', params: { rr: form.takeProfit.rr } }
        : null,
      conditions: [],
    },
    risk: { sizing: { type: 'percent_risk', params: { percent: form.percent } } },
  }
}

/** Fold the form into a DSL document, in whichever of the two shapes the mode selects. Shape only
 *  — the caller validates it (schema in the browser, semantics at the API) before treating it as
 *  runnable. */
export function buildStrategy(form: StrategyForm): Strategy {
  return form.mode === 'setup' ? buildSetupStrategy(form) : buildConditionStrategy(form)
}

/** The target a setup document starts with: the author trades 5R. */
export const SETUP_TARGET_RR = 5

/** Fold the form into a DSL document that describes its strategy as conditions. */
export function buildConditionStrategy(form: StrategyForm): ConditionStrategy {
  const strategy: ConditionStrategy = {
    schema_version: '1.0',
    name: form.name,
    timeframe: form.timeframe,
    entry: { long: buildCondition(form.long), short: buildCondition(form.short) },
    exit: {
      stop_loss: form.stop.enabled
        ? { type: 'candle_extreme', params: { lookback: form.stop.lookback, side: form.stop.side } }
        : null,
      take_profit: form.takeProfit.enabled
        ? { type: 'risk_multiple', params: { rr: form.takeProfit.rr } }
        : null,
      conditions: form.exit.rows.map(comparison),
    },
    risk: { sizing: { type: 'percent_risk', params: { percent: form.percent } } },
  }
  if (form.indicators.length > 0) {
    // The generated `Indicators` type is a union of fixed-length tuples (0..20); a mapped array
    // does not match it structurally, so the assignment is asserted rather than inferred.
    strategy.indicators = form.indicators.map((indicator) => ({
      id: indicator.id,
      type: indicator.kind,
      params: { period: indicator.period, source: indicator.source },
    })) as NonNullable<Strategy['indicators']>
  }
  return strategy
}

function emptySide(): SideForm {
  return { enabled: false, combine: 'all', rows: [] }
}

/** The setup half of a fresh form. It exists even in condition mode, and vice versa, so toggling
 *  between the two to compare them never throws away what was typed in the other. */
export function emptySetup(type: SetupType = 'ponto_continuo'): SetupForm {
  return { type, values: setupValues(type) }
}

/** A blank form: no indicators, no conditions, sensible risk. The starting point in the UI. */
export function emptyForm(): StrategyForm {
  return {
    name: '',
    timeframe: 'H1',
    mode: 'conditions',
    setup: emptySetup(),
    indicators: [],
    long: emptySide(),
    short: emptySide(),
    stop: { enabled: false, lookback: 1, side: 'low' },
    takeProfit: { enabled: false, rr: 2 },
    exit: emptySide(),
    percent: 1,
  }
}

/** A worked example the UI offers as a starting template: a two-SMA crossover, long only, with
 *  a candle-extreme stop and a 2:1 target. */
export function maCrossForm(): StrategyForm {
  return {
    name: 'MA cross',
    timeframe: 'H1',
    mode: 'conditions',
    setup: emptySetup(),
    indicators: [
      { id: 'fast', kind: 'SMA', period: 9, source: 'close' },
      { id: 'slow', kind: 'SMA', period: 21, source: 'close' },
    ],
    long: {
      enabled: true,
      combine: 'all',
      rows: [{ left: 'fast', op: 'crosses_above', right: 'slow', rightKind: 'ref' }],
    },
    short: emptySide(),
    stop: { enabled: true, lookback: 2, side: 'low' },
    takeProfit: { enabled: true, rr: 2 },
    exit: {
      enabled: true,
      combine: 'all',
      rows: [{ left: 'fast', op: 'crosses_below', right: 'slow', rightKind: 'ref' }],
    },
    percent: 1,
  }
}

/** A worked RSI example: go long when RSI(14) crosses below 30 (oversold) and close when it
 *  crosses back above 70 (overbought), with a candle-extreme stop and a 2:1 target. The `30` and
 *  `70` are literal `value` operands — the thresholds the guided builder can now express. */
export function rsiOversoldForm(): StrategyForm {
  return {
    name: 'RSI oversold',
    timeframe: 'H1',
    mode: 'conditions',
    setup: emptySetup(),
    indicators: [{ id: 'rsi', kind: 'RSI', period: 14, source: 'close' }],
    long: {
      enabled: true,
      combine: 'all',
      rows: [{ left: 'rsi', op: 'crosses_below', right: '30', rightKind: 'value' }],
    },
    short: emptySide(),
    stop: { enabled: true, lookback: 5, side: 'low' },
    takeProfit: { enabled: true, rr: 2 },
    exit: {
      enabled: true,
      combine: 'all',
      rows: [{ left: 'rsi', op: 'crosses_above', right: '70', rightKind: 'value' }],
    },
    percent: 1,
  }
}

/**
 * What each setup is called on screen.
 *
 * `Record<SetupType, string>` rather than a lookup with a fallback: a fifth setup added in Python
 * has to fail to compile here, because the alternative is a picker quietly listing a raw
 * `structure_choch` among four named ones — or worse, listing nothing for it.
 */
export const SETUP_LABELS: Record<SetupType, string> = {
  mme9_breakout: 'MME9 breakout',
  ponto_continuo: 'Ponto Contínuo',
  structure_choch: 'Structure — CHoCH',
  structure_continuation: 'Structure — Continuation',
}

/**
 * A fresh form for a named setup: its own defaults, the author's 5R target, and `side` left blank.
 *
 * Picking a setup from the list says *which* setup, not which direction — so the one field with no
 * default stays unanswered, and the screen keeps saving disabled until it is chosen. Pre-filling it
 * here would be the picker quietly answering a question the schema deliberately asks.
 */
export function setupForm(type: SetupType): StrategyForm {
  return {
    ...emptyForm(),
    name: SETUP_LABELS[type],
    mode: 'setup',
    setup: emptySetup(type),
    takeProfit: { enabled: true, rr: SETUP_TARGET_RR },
  }
}

export interface StrategyChoice {
  id: string
  label: string
  /** The heading it sits under: the two shapes a document may take. */
  group: 'Setups' | 'Conditions'
  form: () => StrategyForm
}

/**
 * Everything the builder can start from, in one list.
 *
 * The setups come from the schema, so a new one in Python appears in the picker with no edit here;
 * the condition entries are worked examples and are written out. Grouping them keeps the two
 * document shapes visible as the different things they are, while still being one question:
 * *which strategy* — instead of a mode to pick before the strategies are even visible.
 */
export const STRATEGY_CHOICES: readonly StrategyChoice[] = [
  ...SETUP_TYPES.map((type) => ({
    id: type,
    label: SETUP_LABELS[type],
    group: 'Setups' as const,
    form: () => setupForm(type),
  })),
  { id: 'ma_cross', label: 'Moving-average cross', group: 'Conditions', form: maCrossForm },
  { id: 'rsi_oversold', label: 'RSI oversold', group: 'Conditions', form: rsiOversoldForm },
]

export function strategyChoice(id: string): StrategyChoice {
  const choice = STRATEGY_CHOICES.find((candidate) => candidate.id === id)
  if (choice === undefined) throw new Error(`no strategy named ${id}`)
  return choice
}
