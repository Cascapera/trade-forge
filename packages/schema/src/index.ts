export { validateStrategy } from './validate.js'
export type { Strategy, ValidationFailure, ValidationResult } from './validate.js'
// The setups a document may name, with each parameter's kind, bounds and default read out of the
// JSON Schema — so the builder shows the author's own numbers without keeping a copy of them.
export { SETUP_TYPES, SETUPS, setupSpec } from './setups.js'
export type { SetupParam, SetupSpec, SetupType } from './setups.js'
// The indicators a document may declare, and which parameters each takes — read from the same
// schema, so a form cannot offer `source` to an indicator defined over the whole candle.
export { INDICATOR_TYPES, INDICATORS, indicatorSpec, takesSource } from './indicators.js'
export type { IndicatorSpec, IndicatorType } from './indicators.js'
// The DSL's own sub-types, re-exported from the generated source so a consumer (the web
// strategy builder) composes a strategy against the same types the schema defines — never a
// hand-written copy that could drift from the contract.
// ⚠️ `Op` and `Op1` are the names `json-schema-to-typescript` invents for two anonymous string
// enums (`between`, and `rising`/`falling`). They are renamed on the way out rather than in the
// generated file, which is regenerated from the schema and must never be hand-edited — so a
// consumer says `TrendOp` while the contract stays the single source it always was.
export type {
  Between,
  Comparison,
  ComparisonOp,
  Condition,
  Entry,
  Exit,
  Indicator,
  Operand,
  Ref,
  Risk,
  Timeframe,
  Trend,
  Op as BetweenOp,
  Op1 as TrendOp,
} from './generated/strategy.js'
