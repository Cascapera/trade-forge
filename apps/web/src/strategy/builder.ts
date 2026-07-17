// The guided builder's pure core: a flat, form-shaped model and the function that folds it into
// a DSL `Strategy` document. Kept free of React so it can be unit-tested exhaustively — the part
// with the real logic (a single condition collapses to a bare comparison; a group becomes
// `all`/`any`; an empty side becomes `null`) is proven here, and the form component just edits
// the model and renders whatever `buildStrategy` produces.
//
// The DSL types come from `@tradeforge/schema` (generated from the shared JSON Schema), never
// hand-written. The runtime option lists below are checked against those types with `satisfies`,
// so an invalid value (a timeframe the schema does not know) is a compile error.

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

export const INDICATOR_KINDS = ['SMA', 'EMA'] as const
export type IndicatorKind = (typeof INDICATOR_KINDS)[number]

export type Combine = 'all' | 'any'

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

export interface StrategyForm {
  name: string
  timeframe: Timeframe
  indicators: IndicatorForm[]
  long: SideForm
  short: SideForm
  stop: StopForm
  takeProfit: TakeProfitForm
  exit: SideForm
  percent: number
}

function comparison(row: ConditionRow): Comparison {
  return { op: row.op, left: { ref: row.left }, right: { ref: row.right } }
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

/** Fold the form into a DSL document. Shape only — the caller validates it (schema in the
 *  browser, semantics at the API) before treating it as runnable. */
export function buildStrategy(form: StrategyForm): Strategy {
  const strategy: Strategy = {
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

/** A blank form: no indicators, no conditions, sensible risk. The starting point in the UI. */
export function emptyForm(): StrategyForm {
  return {
    name: '',
    timeframe: 'H1',
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
    indicators: [
      { id: 'fast', kind: 'SMA', period: 9, source: 'close' },
      { id: 'slow', kind: 'SMA', period: 21, source: 'close' },
    ],
    long: {
      enabled: true,
      combine: 'all',
      rows: [{ left: 'fast', op: 'crosses_above', right: 'slow' }],
    },
    short: emptySide(),
    stop: { enabled: true, lookback: 2, side: 'low' },
    takeProfit: { enabled: true, rr: 2 },
    exit: {
      enabled: true,
      combine: 'all',
      rows: [{ left: 'fast', op: 'crosses_below', right: 'slow' }],
    },
    percent: 1,
  }
}
