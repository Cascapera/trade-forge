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
import schema from './tradeforge_schema/strategy.schema.json' with { type: 'json' }

/** The DSL's `setup.type` — the union the generated types already define. */
export type SetupType = Setup['type']

/** A parameter the form must render, in the vocabulary a form cares about. */
export type SetupParam =
  | {
      name: string
      kind: 'enum'
      required: boolean
      /** `null` when the schema gives none, which means the user has to choose. */
      default: string | null
      options: readonly string[]
    }
  | {
      name: string
      kind: 'integer' | 'number'
      required: boolean
      default: number | null
      /** Whether an explicit `null` is a legal value — "off", not "unset". */
      nullable: boolean
      min?: number
      max?: number
    }
  | { name: string; kind: 'boolean'; required: boolean; default: boolean }

export interface SetupSpec {
  type: SetupType
  /** Required parameters first: the field with no default is the one that must be answered. */
  params: readonly SetupParam[]
}

// A JSON Schema node, in the narrow shape this file reads. Deliberately not exhaustive — anything
// unrecognised makes `describe` throw rather than produce a field the form would render wrong.
interface Node {
  $ref?: string
  type?: string
  const?: string
  enum?: readonly string[]
  default?: unknown
  anyOf?: readonly Node[]
  oneOf?: readonly Node[]
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  properties?: Record<string, Node>
  required?: readonly string[]
  discriminator?: { propertyName: string; mapping: Record<string, string> }
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
  const defs = (root as { $defs?: Record<string, Node> }).$defs ?? {}

  function resolve(ref: string): Node {
    const node = defs[ref.replace('#/$defs/', '')]
    if (node === undefined) throw new Error(`strategy schema has no definition for ${ref}`)
    return node
  }

  /** Follow a `$ref` if there is one, keeping the referring node's own keywords — a `$ref` sibling
   *  carries the `default`, which is where `average: "EMA"` lives. */
  function deref(node: Node): Node {
    return node.$ref === undefined ? node : { ...resolve(node.$ref), ...node }
  }

  /** The branch of an `anyOf` that is not `null`. Pydantic emits `T | None` that way, and it is how
   *  a parameter says that an explicit null is a setting rather than an absence. */
  function nonNull(node: Node): { branch: Node; nullable: boolean } {
    if (node.anyOf === undefined) return { branch: node, nullable: false }
    const branches = node.anyOf.map(deref)
    const real = branches.find((branch) => branch.type !== 'null')
    if (real === undefined) throw new Error('a parameter whose only type is null cannot be rendered')
    return { branch: real, nullable: branches.some((branch) => branch.type === 'null') }
  }

  function describe(name: string, raw: Node, required: boolean): SetupParam {
    const node = deref(raw)
    const { branch, nullable } = nonNull(node)
    const fallback = node.default

    if (branch.enum !== undefined) {
      return {
        name,
        kind: 'enum',
        required,
        default: typeof fallback === 'string' ? fallback : null,
        options: branch.enum,
      }
    }
    if (branch.type === 'boolean') {
      return { name, kind: 'boolean', required, default: fallback === true }
    }
    if (branch.type === 'integer' || branch.type === 'number') {
      const min = branch.minimum ?? branch.exclusiveMinimum
      return {
        name,
        kind: branch.type,
        required,
        default: typeof fallback === 'number' ? fallback : null,
        nullable,
        ...(min === undefined ? {} : { min }),
        ...(branch.maximum === undefined ? {} : { max: branch.maximum }),
      }
    }
    throw new Error(`setup parameter ${name} has a kind no form control can hold: ${String(branch.type)}`)
  }

  function specFor(type: string, ref: string): SetupSpec {
    const paramsRef = resolve(ref).properties?.params?.$ref
    if (paramsRef === undefined) throw new Error(`setup ${type} has no params definition`)
    const params = resolve(paramsRef)
    const required = new Set(params.required ?? [])
    const described = Object.entries(params.properties ?? {}).map(([name, node]) =>
      describe(name, node, required.has(name)),
    )
    // Required first, then schema order. The parameter with no default is the question the form has
    // to ask, and burying it under four pre-filled ones is how it gets skipped.
    return {
      type: type as SetupType,
      params: [...described.filter((p) => p.required), ...described.filter((p) => !p.required)],
    }
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
