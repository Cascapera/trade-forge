// The guided builder's pure core: a flat, form-shaped model and the function that folds it into
// a DSL `Strategy` document. Kept free of React so it can be unit-tested exhaustively — the part
// with the real logic (a single condition collapses to a bare comparison; a group becomes
// `all`/`any`; an empty side becomes `null`) is proven here, and the form component just edits
// the model and renders whatever `buildStrategy` produces.
//
// The DSL types come from `@tradeforge/schema` (generated from the shared JSON Schema), never
// hand-written. The runtime option lists below are checked against those types with `satisfies`,
// so an invalid value (a timeframe the schema does not know) is a compile error.

import {
  INDICATOR_TYPES,
  indicatorSpec,
  refsFor,
  SETUP_TYPES,
  setupSpec,
  type IndicatorType,
  type SchemaParam,
  type SetupType,
} from '@tradeforge/schema'
import type {
  BetweenOp,
  ComparisonOp,
  Condition,
  Operand,
  Strategy,
  Timeframe,
  TrendOp,
} from '@tradeforge/schema'

import { runName } from './naming'

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

export const TREND_OPS = ['rising', 'falling'] as const satisfies readonly TrendOp[]
export const BETWEEN_OP = 'between' satisfies BetweenOp

/** Every operator a row can carry — the eight that compare two operands, plus the three the DSL
 *  gives nodes of their own because they do not take two. */
export type RowOp = ComparisonOp | BetweenOp | TrendOp

/** Which DSL node an operator produces. The row's discriminator, and the reason the operator
 *  picker can stay a single question when the answers have three different arities. */
export type RowShape = ConditionRow['shape']

/**
 * The operator picker, grouped by the shape each choice produces.
 *
 * ⚠️ The grouping is not decoration. Picking `between` or `rising` **changes the row's shape**,
 * and the reader is entitled to see that coming before the fields under the cursor rearrange
 * themselves. A flat list of eleven would make the rearrangement look like a glitch.
 */
export const OP_GROUPS = [
  { shape: 'comparison', label: 'compares two', ops: OPS },
  { shape: 'between', label: 'inside a band', ops: [BETWEEN_OP] },
  { shape: 'trend', label: 'reads a window', ops: TREND_OPS },
] as const satisfies readonly { shape: RowShape; label: string; ops: readonly RowOp[] }[]

/**
 * Every operator the picker actually offers — read back off `OP_GROUPS` rather than listed again.
 *
 * ⚠️ The line below is the completeness proof, and it runs in the **compiler**, not in a test:
 * `RowOp` is built from the types generated out of the JSON Schema, so an operator added in
 * Python and left out of a group makes `Missing` non-empty and this file stop compiling. A
 * runtime test could only check the lists against each other, which is the tautology; this
 * checks them against the contract.
 */
type OfferedOp = (typeof OP_GROUPS)[number]['ops'][number]
type MissingOp = Exclude<RowOp, OfferedOp>
const _everyOperatorIsOffered: MissingOp extends never ? true : MissingOp = true
void _everyOperatorIsOffered

/** The shape an operator belongs to. Derived from `OP_GROUPS`, so the two can never disagree. */
export function shapeOf(op: RowOp): RowShape {
  for (const group of OP_GROUPS) {
    if ((group.ops as readonly RowOp[]).includes(op)) return group.shape
  }
  // Unreachable through the picker, whose options *are* `OP_GROUPS`. Loud rather than silent:
  // an operator added to the schema and not to a group would otherwise pick up `comparison`'s
  // fields and emit a document the API refuses, with a message about the wrong node.
  throw new Error(`no shape for operator ${op}`)
}

export const SOURCES = ['open', 'high', 'low', 'close'] as const
export type Source = (typeof SOURCES)[number]

// ⚠️ Not a list. `INDICATOR_TYPES` is read from the discriminator of the JSON Schema the
// Pydantic models generate, so an indicator added in Python appears in the builder with nothing
// changed here — the same discipline `axesFor` follows for setup parameters.
export const INDICATOR_KINDS = INDICATOR_TYPES
export type IndicatorKind = IndicatorType

export type Combine = 'all' | 'any'

// An operand is either another reference (`fast`, `price.close`, `bb.upper`) or a literal number
// (the `30` in `rsi < 30`). The form keeps the text a string in both cases and this flag decides
// how it is folded — a `{ ref }` or a `{ value }` — so RSI thresholds become expressible.
export type OperandKind = 'ref' | 'value'

/** An operand as the form holds it: what was typed, and what the typing means. */
export interface OperandForm {
  text: string
  kind: OperandKind
}

export function refOperand(text = ''): OperandForm {
  return { text, kind: 'ref' }
}

export function valueOperand(text = ''): OperandForm {
  return { text, kind: 'value' }
}

function operand(form: OperandForm): Operand {
  return form.kind === 'value' ? { value: Number(form.text) } : { ref: form.text }
}

/**
 * A declared indicator, and the values typed into whatever parameters it happens to have.
 *
 * ⚠️ **`values` by name, not `period` and `source` as fields**, and the difference is not style.
 * The hand-written version could only carry the parameters somebody had remembered to add — which
 * is exactly how a Bollinger's `deviations` stayed unreachable from the screen for a release
 * while being perfectly valid in the DSL, and every band built here came out at 2.0. Driven by
 * the spec, an indicator that gains a parameter in Python gains a control here with no edit.
 *
 * Values are held as the form holds them — strings, or booleans for flags — for the reason
 * `SetupForm` gives: an empty box is a state a number cannot represent.
 */
export interface IndicatorForm {
  id: string
  kind: IndicatorKind
  values: Record<string, string | boolean>
}

/** A heading in the ref picker and the names under it. */
export interface RefGroup {
  label: string
  refs: readonly string[]
}

/** The four fields of the candle being decided on. */
export const PRICE_REFS: readonly string[] = SOURCES.map((source) => `price.${source}`)

/**
 * The option that means "none of these", and what picking it puts in the box.
 *
 * ⚠️ **The ref grammar has a form no list can hold.** `price.close` and `bb.upper` are
 * enumerable; `candle[-N].field` is not, because N is unbounded. A picker with no escape would
 * make that form unreachable from the screen — trading one unreachable corner of the grammar for
 * another. So picking `custom` seeds the shortest well-formed candle ref and reveals the box,
 * which both keeps the form reachable and shows its shape to somebody who has never typed one.
 */
export const CUSTOM_REF = '__custom__'
export const CUSTOM_REF_SEED = 'candle[-1].close'

/**
 * Every ref the screen can offer, given what this strategy declares.
 *
 * The indicator half is spelled by `refsFor`, so a composite contributes its components and never
 * its bare id — the distinction lives in the schema, not here. Ids are de-duplicated: two
 * indicators sharing a name is a document the semantic layer refuses, but it is also a state the
 * form passes through while somebody is typing, and a picker is not the place to find that out.
 */
export function refCatalogue(indicators: readonly IndicatorForm[]): readonly RefGroup[] {
  const declared = [
    ...new Set(
      indicators.filter((one) => one.id !== '').flatMap((one) => refsFor(one.kind, one.id)),
    ),
  ]
  return [
    { label: 'this candle', refs: PRICE_REFS },
    ...(declared.length === 0 ? [] : [{ label: 'indicators', refs: declared }]),
  ]
}

/** Whether the picker can offer this ref, which is what decides if the free-text box shows. */
export function catalogueHas(groups: readonly RefGroup[], ref: string): boolean {
  return groups.some((group) => group.refs.includes(ref))
}

/**
 * One line of a condition, in whichever of the three shapes its operator takes.
 *
 * **A union rather than one row with optional fields**, and the reason is written down in
 * `models.py` next to `Between`: folding a third operand into the comparison row would make
 * `right` mean one thing for eight operators and another for the ninth. The DSL refused that
 * and discriminates on shape; a form that did otherwise would be a picture of a grammar the
 * engine does not have.
 *
 * **The subject travels between shapes.** `left`, `value` and `of` are the same question —
 * *which series are you asking about* — under three names the DSL chose per node. Switching the
 * operator carries it over (see `withOp`); only the bounds, which mean nothing in the new shape,
 * are dropped.
 */
export type ConditionRow =
  | { shape: 'comparison'; left: string; op: ComparisonOp; right: OperandForm }
  | { shape: 'between'; value: string; low: OperandForm; high: OperandForm }
  | { shape: 'trend'; of: string; op: TrendOp; bars: string }

/** The series the row asks about, whatever the DSL calls it in this shape. */
export function subjectOf(row: ConditionRow): string {
  switch (row.shape) {
    case 'comparison':
      return row.left
    case 'between':
      return row.value
    case 'trend':
      return row.of
  }
}

/** What the DSL calls the subject here — the name the field answers to, so the screen never
 *  labels a band's `value` "left". */
export function subjectName(shape: RowShape): 'left' | 'value' | 'of' {
  switch (shape) {
    case 'comparison':
      return 'left'
    case 'between':
      return 'value'
    case 'trend':
      return 'of'
  }
}

/** The row with a new subject, written to whichever field this shape keeps it in. */
export function withSubject(row: ConditionRow, text: string): ConditionRow {
  switch (row.shape) {
    case 'comparison':
      return { ...row, left: text }
    case 'between':
      return { ...row, value: text }
    case 'trend':
      return { ...row, of: text }
  }
}

/** The operator the picker should show. `between` carries no field of its own — the shape *is*
 *  the operator — so it is answered from the shape. */
export function opOf(row: ConditionRow): RowOp {
  return row.shape === 'between' ? BETWEEN_OP : row.op
}

/**
 * The row that answers `op`, keeping everything the new shape still has a place for.
 *
 * Staying inside a shape is a field patch and nothing is lost. Crossing shapes keeps the subject
 * and starts the bounds empty: a `30` typed as the right-hand side of `rsi lt 30` is not the
 * lower bound of a band, and pre-filling it there would be the form putting a number into a
 * strategy that nobody chose.
 */
export function withOp(row: ConditionRow, op: RowOp): ConditionRow {
  const shape = shapeOf(op)
  if (shape === row.shape) {
    // Same shape: `between` carries no operator of its own to patch, the other two do.
    if (row.shape === 'between') return row
    return { ...row, op } as ConditionRow
  }
  const subject = subjectOf(row)
  switch (shape) {
    case 'comparison':
      return { shape, left: subject, op: op as ComparisonOp, right: refOperand() }
    case 'between':
      return { shape, value: subject, low: valueOperand(), high: valueOperand() }
    case 'trend':
      return { shape, of: subject, op: op as TrendOp, bars: '' }
  }
}

/** A blank comparison — what `+ condition` adds, and the shape eight of the eleven operators take. */
export function emptyRow(): ConditionRow {
  return { shape: 'comparison', left: '', op: 'gt', right: refOperand() }
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
  /**
   * The three optional scalars a document may carry, held as text so that **empty means absent**.
   *
   * ⚠️ Not numbers with the schema's defaults pre-filled, and the reason is the round-trip. A
   * document that omits `max_open_positions` and one that writes `1` are the same strategy today
   * and would stop being so the day the default moved — so re-saving a document that omitted it
   * must not quietly write it in. This is the rule `setupParams` and the `bars` box already
   * follow: an untouched box is not a number the author chose.
   */
  description: string
  maxOpenPositions: string
  maxDailyLossPercent: string
}

/** The optional half of `risk`, in the shape the DSL carries it — absent where the box is empty. */
function riskCaps(form: StrategyForm): Record<string, number> {
  const caps: Record<string, number> = {}
  if (form.maxOpenPositions.trim() !== '') caps.max_open_positions = Number(form.maxOpenPositions)
  if (form.maxDailyLossPercent.trim() !== '') {
    caps.max_daily_loss_percent = Number(form.maxDailyLossPercent)
  }
  return caps
}

/** The `description`, only when there is one. An empty string is what the schema defaults to, so
 *  writing it would add a key that says nothing. */
function describedAs(form: StrategyForm): { description?: string } {
  return form.description === '' ? {} : { description: form.description }
}

/**
 * One row, as the DSL node its shape names.
 *
 * ⚠️ **An empty `bars` box leaves the key out**, rather than sending a zero or a one. The schema
 * declares `bars: 1` as the node's default, and omitting it is how the form says "whatever the
 * engine's own answer is" — the same rule `setupParams` follows one screen over, and the same
 * reason: an untouched box is not a number the author chose. Sending `0` would be worse than
 * wrong, because `rising` over zero bars asks nothing.
 */
export function conditionOf(row: ConditionRow): Condition {
  switch (row.shape) {
    case 'comparison':
      return { op: row.op, left: { ref: row.left }, right: operand(row.right) }
    case 'between':
      return {
        op: BETWEEN_OP,
        value: { ref: row.value },
        low: operand(row.low),
        high: operand(row.high),
      }
    case 'trend': {
      const bars = row.bars.trim()
      return bars === ''
        ? { op: row.op, of: { ref: row.of } }
        : { op: row.op, of: { ref: row.of }, bars: Number(bars) }
    }
  }
}

/** A side's condition, in the DSL's shape: `null` if empty, a bare comparison if there is one
 *  row, an `all`/`any` group if there are several. */
export function buildCondition(side: SideForm): Condition | null {
  if (!side.enabled) return null
  // Destructured rather than indexed, and that is not a style choice: `all` and `any` take a
  // *non-empty* list in the schema, and `[first, ...rest]` satisfies that tuple structurally.
  // Indexing would hand back `Condition | undefined` and force an assertion to paper over it —
  // which is the compiler being told to stop asking the one question the schema is asking.
  const [first, ...rest] = side.rows.map(conditionOf)
  if (first === undefined) return null
  if (rest.length === 0) return first
  return side.combine === 'all' ? { all: [first, ...rest] } : { any: [first, ...rest] }
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
export function initialValues(params: readonly SchemaParam[]): Record<string, string | boolean> {
  const values: Record<string, string | boolean> = {}
  for (const param of params) {
    values[param.name] = param.kind === 'boolean' ? param.default : (param.default?.toString() ?? '')
  }
  return values
}

export function setupValues(type: SetupType): Record<string, string | boolean> {
  return initialValues(setupSpec(type).params)
}

/** The same, for an indicator. A fresh SMA arrives with `period` blank and `source` at `close`,
 *  because that is what the schema says — never a number written here. */
export function indicatorValues(kind: IndicatorKind): Record<string, string | boolean> {
  return initialValues(indicatorSpec(kind).params)
}

/**
 * The values after changing an indicator's kind: whatever the new kind still has a place for,
 * kept; everything else from its own defaults.
 *
 * ⚠️ **Deliberately not what the setup picker does**, which starts from scratch. The reasoning
 * there is that a setup's parameters are the knobs of one named machine, so a name carried into
 * another setup would mean something else. An indicator's are a small shared vocabulary: `period`
 * is a window on all eight of them and `source` is a price series on the four that read one. A
 * reader comparing an SMA(20) with an EMA(20) is asking one question, and making them retype the
 * 20 to ask it is how a comparison stops being made.
 *
 * The type check is the guard: a value only carries over if it is the kind of thing the new
 * parameter holds, so a flag can never land in a number's box.
 */
export function retypedValues(
  kind: IndicatorKind,
  previous: Record<string, string | boolean>,
): Record<string, string | boolean> {
  const fresh = indicatorValues(kind)
  for (const [name, value] of Object.entries(fresh)) {
    const carried = previous[name]
    if (carried !== undefined && typeof carried === typeof value) fresh[name] = carried
  }
  return fresh
}

/** Fold the typed values back into the document's `params`, per the rules in `SetupForm`. */
export function foldParams(
  params: readonly SchemaParam[],
  values: Record<string, string | boolean>,
): Record<string, unknown> {
  const folded: Record<string, unknown> = {}
  for (const param of params) {
    const raw = values[param.name]
    if (param.kind === 'boolean') {
      folded[param.name] = raw === true
      continue
    }
    const text = typeof raw === 'string' ? raw.trim() : ''
    if (text === '') {
      if (param.kind !== 'enum' && param.nullable) folded[param.name] = null
      continue
    }
    folded[param.name] = param.kind === 'enum' ? text : Number(text)
  }
  return folded
}

/** Fold the typed values back into the document's `params`, per the rules in `SetupForm`. */
function setupParams(form: SetupForm): Record<string, unknown> {
  return foldParams(setupSpec(form.type).params, form.values)
}

/**
 * A document that names a setup.
 *
 * It carries only what was never the strategy's to decide: the account's risk and the broker's
 * target. No indicators, no entry conditions, and no stop — the setup declares its own indicators,
 * *is* the entry, and places its stop from the bar it entered on. The semantic layer refuses a
 * setup document that carries any of them, so the form does not offer them either.
 */
/**
 * The `exit` block of a setup document — only the parts that say something.
 *
 * ⚠️ A setup places its own stop and conducts its own exit, so `stop_loss: null` and
 * `conditions: []` are not settings: they are the absence of settings, spelled out. Writing them
 * adds keys that carry no information, and it made re-saving a document that omitted them rewrite
 * its shape. Same rule as `description`: a key equal to the schema's own default says nothing.
 */
function setupExit(
  form: StrategyForm,
): { exit: NonNullable<Strategy['exit']> } | Record<string, never> {
  if (!form.takeProfit.enabled) return {}
  return {
    exit: { take_profit: { type: 'risk_multiple', params: { rr: form.takeProfit.rr } } },
  }
}

export function buildSetupStrategy(form: StrategyForm): SetupStrategy {
  return {
    schema_version: '1.0',
    name: form.name,
    ...describedAs(form),
    timeframe: form.timeframe,
    setup: {
      type: form.setup.type,
      params: setupParams(form.setup),
    } as SetupStrategy['setup'],
    ...setupExit(form),
    risk: {
      sizing: { type: 'percent_risk', params: { percent: form.percent } },
      ...riskCaps(form),
    },
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
    ...describedAs(form),
    timeframe: form.timeframe,
    entry: { long: buildCondition(form.long), short: buildCondition(form.short) },
    exit: {
      stop_loss: form.stop.enabled
        ? { type: 'candle_extreme', params: { lookback: form.stop.lookback, side: form.stop.side } }
        : null,
      take_profit: form.takeProfit.enabled
        ? { type: 'risk_multiple', params: { rr: form.takeProfit.rr } }
        : null,
      conditions: form.exit.rows.map(conditionOf),
    },
    risk: {
      sizing: { type: 'percent_risk', params: { percent: form.percent } },
      ...riskCaps(form),
    },
  }
  if (form.indicators.length > 0) {
    // The generated `Indicators` type is a union of fixed-length tuples (0..20); a mapped array
    // does not match it structurally, so the assignment is asserted rather than inferred.
    // ⚠️ There is no longer a case here for "the ones without a source". ATR and the two channels
    // are defined over the whole candle, and their params forbid extra keys — so emitting a source
    // for them produces a document the API refuses. That used to be a conditional; now the spec
    // decides which parameters exist at all, so the wrong key has nowhere to come from.
    strategy.indicators = form.indicators.map((indicator) => ({
      id: indicator.id,
      type: indicator.kind,
      params: foldParams(indicatorSpec(indicator.kind).params, indicator.values),
    })) as NonNullable<Strategy['indicators']>
  }
  return strategy
}

export function emptySide(): SideForm {
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
    description: '',
    maxOpenPositions: '',
    maxDailyLossPercent: '',
  }
}

/** A worked example the UI offers as a starting template: a two-SMA crossover, long only, with
 *  a candle-extreme stop and a 2:1 target. */
export function maCrossForm(now: Date): StrategyForm {
  return {
    name: runName('ma_cross', now),
    timeframe: 'H1',
    mode: 'conditions',
    setup: emptySetup(),
    indicators: [
      { id: 'fast', kind: 'SMA', values: { period: '9', source: 'close' } },
      { id: 'slow', kind: 'SMA', values: { period: '21', source: 'close' } },
    ],
    long: {
      enabled: true,
      combine: 'all',
      rows: [{ shape: 'comparison', left: 'fast', op: 'crosses_above', right: refOperand('slow') }],
    },
    short: emptySide(),
    stop: { enabled: true, lookback: 2, side: 'low' },
    takeProfit: { enabled: true, rr: 2 },
    exit: {
      enabled: true,
      combine: 'all',
      rows: [{ shape: 'comparison', left: 'fast', op: 'crosses_below', right: refOperand('slow') }],
    },
    percent: 1,
    description: '',
    maxOpenPositions: '',
    maxDailyLossPercent: '',
  }
}

/** A worked RSI example: go long when RSI(14) crosses below 30 (oversold) and close when it
 *  crosses back above 70 (overbought), with a candle-extreme stop and a 2:1 target. The `30` and
 *  `70` are literal `value` operands — the thresholds the guided builder can now express. */
export function rsiOversoldForm(now: Date): StrategyForm {
  return {
    name: runName('rsi_oversold', now),
    timeframe: 'H1',
    mode: 'conditions',
    setup: emptySetup(),
    indicators: [{ id: 'rsi', kind: 'RSI', values: { period: '14', source: 'close' } }],
    long: {
      enabled: true,
      combine: 'all',
      rows: [{ shape: 'comparison', left: 'rsi', op: 'crosses_below', right: valueOperand('30') }],
    },
    short: emptySide(),
    stop: { enabled: true, lookback: 5, side: 'low' },
    takeProfit: { enabled: true, rr: 2 },
    exit: {
      enabled: true,
      combine: 'all',
      rows: [{ shape: 'comparison', left: 'rsi', op: 'crosses_above', right: valueOperand('70') }],
    },
    percent: 1,
    description: '',
    maxOpenPositions: '',
    maxDailyLossPercent: '',
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
export function setupForm(type: SetupType, now: Date): StrategyForm {
  return {
    ...emptyForm(),
    name: runName(type, now),
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
  /**
   * A fresh form for this choice, stamped with the instant it was picked.
   *
   * The clock is a parameter and not a `new Date()` inside, for the usual reason: a function that
   * reads the wall clock cannot be tested for what it produces, only that it produced something.
   * The screen passes the real one; tests pass a fixed instant and assert the exact name.
   */
  form: (now: Date) => StrategyForm
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
    form: (now: Date) => setupForm(type, now),
  })),
  { id: 'ma_cross', label: 'Moving-average cross', group: 'Conditions', form: maCrossForm },
  { id: 'rsi_oversold', label: 'RSI oversold', group: 'Conditions', form: rsiOversoldForm },
]

export function strategyChoice(id: string): StrategyChoice {
  const choice = STRATEGY_CHOICES.find((candidate) => candidate.id === id)
  if (choice === undefined) throw new Error(`no strategy named ${id}`)
  return choice
}
