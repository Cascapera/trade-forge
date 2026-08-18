// Which parameters a chosen strategy can have varied, and how to say so on screen.
//
// ⚠️ **Nothing here is a list of parameters.** The catalogue is `@tradeforge/schema`'s
// `setupSpec`, which reads each setup's fields, bounds and defaults out of the generated JSON
// Schema — which is generated from the Pydantic models. So a parameter added to a setup in
// Python appears in this dropdown with no code changed anywhere, and a bound tightened there
// tightens the hint here. A hand-written copy would be a second version of the DSL that drifts
// from it silently, which is the one failure mode this whole file exists to avoid.

import { setupSpec, type SchemaParam, type SetupType } from '@tradeforge/schema'

export interface AxisOption {
  /** The dotted path the request carries: `setup.params.period`. */
  path: string
  /** What the reader sees in the dropdown: the parameter's own name. */
  label: string
  /** How to fill the values field for this one — the format *and* what is legal. */
  hint: string
  /** A ready-made line of values, so the first study can be launched without inventing any. */
  example: string
  /**
   * The parameter as the schema describes it — kind, bounds, default, nullability.
   *
   * Carried rather than flattened into more strings, because the *control* a value is entered
   * with is decided by the same facts as the sentence describing it: a set of names wants
   * checkboxes, a bounded number wants arrows that know its step. Two readings of one schema
   * node would be two places to disagree about what a parameter is.
   */
  param: SchemaParam
}

/**
 * Every parameter of a setup, as an option a grid can vary.
 *
 * Returns nothing for a strategy built from indicators and conditions rather than a named
 * setup: those vary at `indicators.0.params.period`, which is a shape this dropdown does not
 * describe. Saying nothing is better than offering paths that do not apply — the screen still
 * accepts a typed path, and the server refuses one that leads nowhere.
 */
export function axesFor(setup: string | null): AxisOption[] {
  if (setup === null) return []
  let spec
  try {
    spec = setupSpec(setup as SetupType)
  } catch {
    // A setup this build of the schema does not know. The API is the authority on what exists,
    // and a screen that threw here would go blank over a strategy it merely could not describe.
    return []
  }
  return spec.params.map((param) => ({
    path: `setup.params.${param.name}`,
    label: param.name,
    hint: hintFor(param),
    example: exampleFor(param),
    param,
  }))
}

/** What to type in the values field, in the parameter's own terms. */
function hintFor(param: SchemaParam): string {
  if (param.kind === 'enum') {
    return `one or more of ${param.options.join(', ')}, separated by commas`
  }
  if (param.kind === 'boolean') {
    return 'true, false — separated by commas'
  }
  // ⚠️ An exclusive bound is said as an exclusive bound. Folding `> 0` into "between 0 and 100"
  // is how this hint told a reader that zero was legal on `breakeven_at_r`, which the API
  // refuses — and the study came back 422 for doing what the screen had suggested.
  const lower =
    param.min === undefined
      ? null
      : param.minExclusive === true
        ? `greater than ${String(param.min)}`
        : `at least ${String(param.min)}`
  const upper =
    param.max === undefined
      ? null
      : param.maxExclusive === true
        ? `below ${String(param.max)}`
        : `at most ${String(param.max)}`
  const bounds = [lower, upper].filter((part) => part !== null).join(' and ')
  const whole = param.kind === 'integer' ? 'whole numbers' : 'numbers'
  return `${whole}${bounds === '' ? '' : ` ${bounds}`}, separated by commas`
}

/**
 * A line of values that will actually run.
 *
 * Built around the parameter's **default**, because that is the value the author chose and the
 * one a first study should be searching around rather than away from. Clamped to the declared
 * bounds so the example is never a request the server would refuse — an example that fails is
 * worse than none.
 */
function exampleFor(param: SchemaParam): string {
  if (param.kind === 'enum') return param.options.join(', ')
  if (param.kind === 'boolean') return 'true, false'

  const middle = param.default ?? 1
  // ⚠️ The step is proportional, and only *whole* parameters are floored at 1. Flooring
  // everything at 1 is the obvious version and it is wrong on a fraction: `stop_buffer`
  // defaults to 0.1 — a tenth of a region's width — and the floored step suggested `0.1, 1.1`,
  // an eleven-fold jump presented as a reasonable thing to search. Measured, not reasoned: the
  // probe printed it before any of this had a test.
  const raw = Math.abs(middle) / 2 || 0.5
  const step = param.kind === 'integer' ? Math.max(1, Math.round(raw)) : raw
  const candidates = [middle - step, middle, middle + step].map(
    // Floating point: 0.1 - 0.05 is 0.05000000000000001, and an example carrying that is an
    // example nobody types. Three significant digits is more than any of these parameters mean.
    (value) => Number(value.toPrecision(3)),
  )
  const within = candidates.filter(
    (value) =>
      // The same exclusivity the hint now states. Suggesting the bound itself would offer a
      // value the API refuses, which is worse than offering none.
      (param.min === undefined ||
        (param.minExclusive === true ? value > param.min : value >= param.min)) &&
      (param.max === undefined ||
        (param.maxExclusive === true ? value < param.max : value <= param.max)),
  )
  // Distinct and in order. A repeated value is two identical backtests, which the server
  // refuses — an example must not be the thing the form is there to prevent.
  return [...new Set(within)].join(', ')
}
