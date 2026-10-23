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
import { readParams, resolverFor, type SchemaNode, type SchemaParam } from './params.js'
import schema from './tradeforge_schema/strategy.schema.json' with { type: 'json' }

/** The DSL's `indicators[].type` — the union the generated types already define. */
export type IndicatorType = Indicator['type']

export interface IndicatorSpec {
  type: IndicatorType
  /**
   * This indicator's own parameters, described for a form control — required first.
   *
   * ⚠️ **Names alone were not enough, and the gap had a name: `deviations`.** A Bollinger's
   * multiplier is a perfectly valid parameter of the DSL that no screen could reach, because the
   * form rendered hand-written fields and nobody had written that one. Read through the shared
   * `readParams`, an indicator gains a parameter in Python and the form offers it — with its own
   * bounds, its own default, and its exclusive lower bound honoured as exclusive.
   */
  params: readonly SchemaParam[]
  /**
   * The component names a reference must pick from — `middle`, `upper`, `lower` for a band —
   * or empty for an indicator referenced by its bare id.
   *
   * ⚠️ **Read from the schema, and it only became readable on purpose.** These names live in
   * `COMPOSITE_COMPONENTS` in Python, and until the classes started publishing them through
   * `json_schema_extra` the only trace of them in the schema was English prose inside a
   * `description`. A builder that wanted to offer `bb.upper` had no choice but to write the
   * list a third time, by hand, with nothing able to pin it to the two that already existed.
   *
   * ⚠️ **Ordered primary first**, like the Python map: a reader who does not know what a band
   * is uses the order to tell the average from its envelope.
   */
  components: readonly string[]
}

/**
 * The component names on an indicator's schema node, refused rather than coerced if malformed.
 *
 * Absent means single-valued, which is the common case and not an error. Present but not a list
 * of strings means the generator changed under this file — and the reading that would let that
 * pass quietly is `[]`, which is indistinguishable from "single-valued" and would turn every
 * `bb.upper` in every saved strategy into a reference the builder cannot offer.
 */
function readComponents(type: string, node: SchemaNode): readonly string[] {
  if (node.components === undefined) return []
  const names = node.components.filter((name): name is string => typeof name === 'string')
  if (names.length !== node.components.length) {
    throw new Error(`indicator ${type} publishes a component that is not a name`)
  }
  return names
}

export function readIndicators(root: unknown): readonly IndicatorSpec[] {
  const { defs, resolve } = resolverFor(root)

  // ⚠️ Reached through the discriminator mapping, never by guessing the definition's name from
  // the DSL type. They differ: `HIGHEST` maps to `#/$defs/Highest`, because the Python class is
  // named for a reader and the DSL name is shouted like the other indicators.
  const mapping = defs.Indicator?.discriminator?.mapping
  if (mapping === undefined) throw new Error('the Indicator union lost its discriminator mapping')

  return Object.entries(mapping).map(([type, ref]) => {
    const node = resolve(ref)
    const paramsRef = node.properties?.params?.$ref
    if (paramsRef === undefined) throw new Error(`indicator ${type} has no params definition`)
    return {
      type: type as IndicatorType,
      params: readParams(resolve(paramsRef), resolve),
      components: readComponents(type, node),
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
  return indicatorSpec(type).params.some((param) => param.name === 'source')
}

/**
 * Every way a declared indicator with this id can be named in a ref.
 *
 * One name for a single-valued indicator, and one per component for a composite — never the bare
 * id of a composite, because the semantic layer refuses it: "the middle band" is a tempting
 * default for `bb` and a wrong one for `adx`, so the DSL makes the choice explicit.
 */
export function refsFor(type: IndicatorType, id: string): readonly string[] {
  const { components } = indicatorSpec(type)
  return components.length === 0 ? [id] : components.map((name) => `${id}.${name}`)
}
