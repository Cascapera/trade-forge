// The setups a document may name, and the shape of each one's parameters — read out of the JSON
// Schema rather than written here.
//
// A setup parameter's default already exists twice on purpose: in the engine class, where the rule
// runs, and in the Pydantic model, so that a form can *show* "20" instead of making the user guess
// it (ADR-0019). This module is that second copy being cashed in. Writing the numbers again here
// would make a third, and the third is always the one that silently disagrees — the same reasoning
// that stops `setup_factory.py` from holding defaults of its own.
//
// So everything below is derived: the list of setups comes from the union's discriminator mapping,
// and each parameter's kind, bounds, nullability and default come from its own schema node. Add a
// setup type or a parameter in Python, regenerate, and it appears here with no edit. What is *not*
// derived is which fields the builder renders — a type-level check in `setups.test.ts` fails to
// compile when a new setup type shows up unhandled, so the gap is loud rather than invisible.

import type { Setup } from './generated/strategy.js'
import { readParams, resolverFor, type SchemaParam } from './params.js'
import schema from './tradeforge_schema/strategy.schema.json' with { type: 'json' }

/** The DSL's `setup.type` — the union the generated types already define. */
export type SetupType = Setup['type']

export interface SetupSpec {
  type: SetupType
  /** Required parameters first: the field with no default is the one that must be answered. */
  params: readonly SchemaParam[]
}

/**
 * Read the setups out of a JSON Schema document.
 *
 * The schema arrives as a parameter rather than being reached for directly, so every refusal below
 * is reachable from a test with a hand-made document. Each one guards a way the contract could
 * change underneath this file — a union that lost its discriminator, a parameter whose type no form
 * control can represent — and a guard nothing exercises is a guess about what the generator emits,
 * not a guarantee.
 */
export function readSetups(root: unknown): readonly SetupSpec[] {
  const { defs, resolve } = resolverFor(root)

  function specFor(type: string, ref: string): SetupSpec {
    const paramsRef = resolve(ref).properties?.params?.$ref
    if (paramsRef === undefined) throw new Error(`setup ${type} has no params definition`)
    return { type: type as SetupType, params: readParams(resolve(paramsRef), resolve) }
  }

  const mapping = defs.Setup?.discriminator?.mapping
  if (mapping === undefined) throw new Error('the Setup union lost its discriminator mapping')
  return Object.entries(mapping).map(([type, ref]) => specFor(type, ref))
}

/** Every setup the DSL can name, in the schema's own order. */
export const SETUPS: readonly SetupSpec[] = readSetups(schema)

export const SETUP_TYPES: readonly SetupType[] = SETUPS.map((setup) => setup.type)

export function setupSpec(type: SetupType): SetupSpec {
  const spec = SETUPS.find((candidate) => candidate.type === type)
  if (spec === undefined) throw new Error(`no setup named ${type}`)
  return spec
}
