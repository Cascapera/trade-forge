export { validateStrategy } from './validate.js'
export type { Strategy, ValidationFailure, ValidationResult } from './validate.js'
// The DSL's own sub-types, re-exported from the generated source so a consumer (the web
// strategy builder) composes a strategy against the same types the schema defines — never a
// hand-written copy that could drift from the contract.
export type {
  Comparison,
  ComparisonOp,
  Condition,
  Entry,
  Exit,
  Indicator,
  Ref,
  Risk,
  Timeframe,
} from './generated/strategy.js'
