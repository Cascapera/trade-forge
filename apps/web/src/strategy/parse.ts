// The other direction: a saved DSL document, read back into the form that produced it.
//
// The builder has been write-only since it existed. It could compose a strategy and save it, and
// nothing could ever open one again — there was no `/strategies/:id`, and the API's `GET` had no
// caller. That is the missing half of the phase-2 acceptance criterion, which is a *round trip*:
// JSON → UI → JSON with nothing lost.
//
// ⚠️ **The dangerous failure here is silence.** A parser that met a shape it could not show — a
// group inside a group, a `not` — and simply skipped it would hand back a form that looks right,
// and saving it would write a different strategy over the author's. So this module never returns
// a partial form: it either represents the document completely or refuses it, naming what it
// found and where. `buildStrategy(formOf(doc).form)` is meant to equal `doc`, and a function that
// can silently drop a branch cannot promise that.

import {
  indicatorSpec,
  setupSpec,
  type Between,
  type Comparison,
  type Condition,
  type Operand,
  type Strategy,
  type Trend,
} from '@tradeforge/schema'

import {
  emptySide,
  refOperand,
  type ConditionRow,
  type IndicatorForm,
  type OperandForm,
  type SideForm,
  type StrategyForm,
} from './builder'

/**
 * What reading a document produced: a complete form, or the reasons it could not be shown.
 *
 * `unsupported` is never empty on a refusal, and every entry names a path — `entry.long`, not
 * "a condition" — because the reader's next question is which rule they have to look at.
 */
export type ParseResult =
  | { ok: true; form: StrategyForm }
  | { ok: false; unsupported: readonly string[] }

/** Collected while walking, so one pass reports every shape the builder cannot show rather than
 *  the first — a document with three nested groups should not take three attempts to find out. */
class Unsupported {
  readonly found: string[] = []

  add(path: string, what: string): void {
    this.found.push(`${path}: ${what}`)
  }
}

/** A number as the form holds it. `2.0` in JSON is `2` in JavaScript, and `'2'` folds back to the
 *  same number — the text is a rendering of the value, never a second source for it. */
function text(value: number): string {
  return String(value)
}

function operandForm(operand: Operand): OperandForm {
  return 'ref' in operand ? { text: operand.ref, kind: 'ref' } : { text: text(operand.value), kind: 'value' }
}

/**
 * The subject of a row, which the form can only hold as a name.
 *
 * ⚠️ The DSL allows a literal on the left of a comparison — `5 > rsi` is well-formed. The builder
 * has never offered it, so a document using it is refused rather than quietly rewritten with the
 * constant moved to the other side, which would be a different rule the day the operator is not
 * symmetric (`crosses_above` is not).
 */
function subject(operand: Operand, path: string, bad: Unsupported): string {
  if ('ref' in operand) return operand.ref
  bad.add(path, `a literal (${text(operand.value)}) where the builder can only show a name`)
  return ''
}

/**
 * One leaf of the condition tree, as a row.
 *
 * ⚠️ **Narrowed by shape, not by the value of `op`.** That is how the DSL's own union
 * discriminates — `Between` is a distinct node precisely because it takes three operands — and it
 * is also the only narrowing TypeScript performs reliably here: excluding `rising` and `falling`
 * by value leaves `Comparison | Trend`, not `Comparison`.
 */
function rowOf(node: Comparison | Between | Trend, path: string, bad: Unsupported): ConditionRow {
  if ('low' in node) {
    return {
      shape: 'between',
      value: subject(node.value, path, bad),
      low: operandForm(node.low),
      high: operandForm(node.high),
    }
  }
  if ('of' in node) {
    return {
      shape: 'trend',
      of: subject(node.of, path, bad),
      op: node.op,
      // ⚠️ Absent stays absent. Writing the schema's `1` in here would make re-saving a document
      // that asked for the engine's own answer start insisting on today's value of it.
      bars: node.bars === undefined ? '' : text(node.bars),
    }
  }
  return {
    shape: 'comparison',
    left: subject(node.left, path, bad),
    op: node.op,
    right: operandForm(node.right),
  }
}

/**
 * One child of a group: a row, or a named refusal and a placeholder that never survives.
 *
 * ⚠️ **`not` is checked here and not only at the top of a side**, and a mutation is what found
 * that: dropping the top-level guard changed nothing, because the only `not` in the corpus is
 * nested and was being reported as "a group inside a group" — a true statement about the wrong
 * thing. A reader told to look for a nested group would not find one.
 */
function childRow(node: Condition, path: string, bad: Unsupported): ConditionRow {
  if (isLeaf(node)) return rowOf(node, path, bad)
  bad.add(
    path,
    'not' in node
      ? 'a `not`, which the builder has no control for yet'
      : 'a group inside a group, which the builder shows one level of',
  )
  return { shape: 'comparison', left: '', op: 'gt', right: refOperand() }
}

/**
 * Whether this node is one the builder can render as a row rather than a group.
 *
 * A type predicate, not a boolean: without it the compiler keeps the leaf shapes in the union
 * after the check and `condition.any` stops type-checking — the narrowing has to be told, because
 * the DSL's union is discriminated by shape and nothing tags it.
 */
function isLeaf(node: Condition): node is Comparison | Between | Trend {
  return 'left' in node || 'low' in node || 'of' in node
}

/**
 * A side of the entry, as the flat list of rows the builder can show.
 *
 * ⚠️ **Flat is the limit of this PR, and it is enforced rather than assumed.** `all` and `any`
 * may nest, and `not` exists; the form has one level and no negation. Both are refused here, by
 * name, so that the tree the builder grows in the next slice arrives with this test already
 * written against it.
 */
function sideOf(condition: Condition | null | undefined, path: string, bad: Unsupported): SideForm {
  if (condition === null || condition === undefined) return emptySide()
  if (isLeaf(condition)) {
    return { enabled: true, combine: 'all', rows: [rowOf(condition, path, bad)] }
  }
  if ('not' in condition) {
    bad.add(path, 'a `not`, which the builder has no control for yet')
    return emptySide()
  }
  const combine = 'all' in condition ? 'all' : 'any'
  const children: readonly Condition[] = 'all' in condition ? condition.all : condition.any
  const rows = children.map((child, index) =>
    childRow(child, `${path}.${combine}[${String(index)}]`, bad),
  )
  return { enabled: true, combine, rows }
}

/**
 * The values of a params object, as the form holds them: what the document says, and empty where
 * it says nothing.
 *
 * ⚠️ **Deliberately not `initialValues`.** A fresh form pre-fills the schema's defaults so the
 * author sees the numbers instead of guessing them; a *loaded* form must show what this document
 * chose, and a parameter it left out was not chosen. Pre-filling here would turn "unset" into
 * "chosen" the moment anybody re-saved, which is exactly the loss this module exists to prevent.
 */
function valuesOf(
  params: readonly { name: string; kind: string }[],
  declared: Record<string, unknown>,
  path: string,
  bad: Unsupported,
): Record<string, string | boolean> {
  const values: Record<string, string | boolean> = {}
  for (const param of params) {
    const given = declared[param.name]
    if (given === undefined || given === null) {
      values[param.name] = param.kind === 'boolean' ? false : ''
      continue
    }
    if (typeof given === 'boolean' || typeof given === 'number' || typeof given === 'string') {
      values[param.name] = typeof given === 'boolean' ? given : String(given)
      continue
    }
    // ⚠️ Refused rather than stringified. `String({})` is `"[object Object]"`, which would sit in
    // the box looking like something somebody typed and fold back into a document that no longer
    // says what it said. A parameter of a shape no control can hold is the module's own promise
    // being broken, and it says so.
    bad.add(`${path}.${param.name}`, 'a value of a shape no form control can hold')
  }
  return values
}

function indicatorOf(
  indicator: NonNullable<Strategy['indicators']>[number],
  bad: Unsupported,
): IndicatorForm {
  const spec = indicatorSpec(indicator.type)
  return {
    id: indicator.id,
    kind: indicator.type,
    values: valuesOf(
      spec.params,
      indicator.params as unknown as Record<string, unknown>,
      `indicators[${indicator.id}].params`,
      bad,
    ),
  }
}

/**
 * Read a saved document back into the form that would produce it.
 *
 * Returns the reasons instead of a form when the document uses a shape the builder cannot show.
 * There is no third answer, and that is the point: a caller cannot accidentally treat a partial
 * reading as a complete one.
 */
export function formOf(strategy: Strategy): ParseResult {
  const bad = new Unsupported()
  const setup = strategy.setup ?? null

  const common = {
    name: strategy.name,
    description: strategy.description ?? '',
    timeframe: strategy.timeframe,
    percent: strategy.risk.sizing.params.percent,
    maxOpenPositions:
      strategy.risk.max_open_positions === undefined ? '' : text(strategy.risk.max_open_positions),
    maxDailyLossPercent:
      strategy.risk.max_daily_loss_percent === undefined
        ? ''
        : text(strategy.risk.max_daily_loss_percent),
    takeProfit:
      strategy.exit?.take_profit == null
        ? { enabled: false, rr: 2 }
        : { enabled: true, rr: strategy.exit.take_profit.params.rr },
  }

  if (setup !== null) {
    const spec = setupSpec(setup.type)
    const form: StrategyForm = {
      ...common,
      mode: 'setup',
      setup: {
        type: setup.type,
        values: valuesOf(spec.params, (setup.params ?? {}) as Record<string, unknown>, 'setup.params', bad),
      },
      indicators: [],
      long: emptySide(),
      short: emptySide(),
      exit: emptySide(),
      stop: { enabled: false, lookback: 1, side: 'low' },
    }
    return bad.found.length === 0 ? { ok: true, form } : { ok: false, unsupported: bad.found }
  }

  const stop = strategy.exit?.stop_loss ?? null
  const exitConditions = strategy.exit?.conditions ?? []
  const exitRows = exitConditions.map((condition, index) =>
    childRow(condition, `exit.conditions[${String(index)}]`, bad),
  )

  const form: StrategyForm = {
    ...common,
    mode: 'conditions',
    setup: { type: 'ponto_continuo', values: {} },
    indicators: (strategy.indicators ?? []).map((one) => indicatorOf(one, bad)),
    long: sideOf(strategy.entry?.long, 'entry.long', bad),
    short: sideOf(strategy.entry?.short, 'entry.short', bad),
    exit: { enabled: exitRows.length > 0, combine: 'all', rows: exitRows },
    stop:
      stop === null
        ? { enabled: false, lookback: 1, side: 'low' }
        : { enabled: true, lookback: stop.params.lookback, side: stop.params.side },
  }
  return bad.found.length === 0 ? { ok: true, form } : { ok: false, unsupported: bad.found }
}
