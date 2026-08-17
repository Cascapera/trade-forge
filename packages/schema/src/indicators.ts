// The indicators the DSL can name, and which parameters each of them takes — read out of the
// JSON Schema rather than written here, for the reason `setups.ts` gives at length.
//
// The list that made this necessary is a small one, and that is what makes it a good example:
// SMA, EMA, RSI and BOLLINGER take a `period` and a price `source`; ATR, HIGHEST, LOWEST and ADX
// take a period and **no source**, because they are defined over the whole candle. A form that
// emitted `source` for all of them would produce documents the API refuses — `extra="forbid"` on
// the params model — and a hand-written set of "the ones without a source" is a copy that goes
// stale the first time an indicator is added in Python.
//
// ⚠️ Which is not a hypothetical: this comment used to end "ADX and Bollinger are next, and both
// are sourceless", and it was **half wrong**. A band is an average of one price series with an
// envelope around it, so it takes a source. The prediction was written by hand and the list is
// read from the schema — so the form was right about Bollinger before anybody noticed the prose
// was not, and the only thing that needed fixing was this paragraph. Bollinger also brings
// `deviations`, a parameter no other indicator has, and it appears in the form for free.

import type { Indicator } from './generated/strategy.js'
import schema from './tradeforge_schema/strategy.schema.json' with { type: 'json' }

/** The DSL's `indicators[].type` — the union the generated types already define. */
export type IndicatorType = Indicator['type']

export interface IndicatorSpec {
  type: IndicatorType
  /** The parameter names this indicator's own params model declares, in schema order. */
  params: readonly string[]
}

interface Node {
  $ref?: string
  properties?: Record<string, Node>
  discriminator?: { propertyName: string; mapping: Record<string, string> }
}

export function readIndicators(root: unknown): readonly IndicatorSpec[] {
  const defs = (root as { $defs?: Record<string, Node> }).$defs ?? {}

  function resolve(ref: string): Node {
    const node = defs[ref.replace('#/$defs/', '')]
    if (node === undefined) throw new Error(`strategy schema has no definition for ${ref}`)
    return node
  }

  // ⚠️ Reached through the discriminator mapping, never by guessing the definition's name from
  // the DSL type. They differ: `HIGHEST` maps to `#/$defs/Highest`, because the Python class is
  // named for a reader and the DSL name is shouted like the other indicators.
  const mapping = defs.Indicator?.discriminator?.mapping
  if (mapping === undefined) throw new Error('the Indicator union lost its discriminator mapping')

  return Object.entries(mapping).map(([type, ref]) => {
    const paramsRef = resolve(ref).properties?.params?.$ref
    if (paramsRef === undefined) throw new Error(`indicator ${type} has no params definition`)
    return {
      type: type as IndicatorType,
      params: Object.keys(resolve(paramsRef).properties ?? {}),
    }
  })
}

/** Every indicator the DSL can name, in the schema's own order. */
export const INDICATORS: readonly IndicatorSpec[] = readIndicators(schema)

export const INDICATOR_TYPES: readonly IndicatorType[] = INDICATORS.map(
  (indicator) => indicator.type,
)

export function indicatorSpec(type: IndicatorType): IndicatorSpec {
  const spec = INDICATORS.find((candidate) => candidate.type === type)
  if (spec === undefined) throw new Error(`no indicator named ${type}`)
  return spec
}

/** Whether this indicator reads one price series, or the whole candle. */
export function takesSource(type: IndicatorType): boolean {
  return indicatorSpec(type).params.includes('source')
}
