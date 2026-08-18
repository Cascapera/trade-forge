// Reading a parameter out of a JSON Schema node, in the vocabulary a form control needs.
//
// This used to live inside `setups.ts`, where it was written for setup parameters and was never
// setup-specific: `deviations` on a Bollinger band is the same question as `breakeven_at_r` on a
// setup — what kind of control, what bounds, what to put in it before anybody types. Leaving it
// there meant the indicator form could only ever offer the parameters somebody had remembered to
// hand-write, which is exactly how `deviations` stayed unreachable from the screen for a whole
// release while being perfectly valid in the DSL.
//
// The schema arrives as a parameter rather than being reached for directly, so every refusal here
// is reachable from a test with a hand-made document — a guard nothing exercises is a guess about
// what the generator emits, not a guarantee.

/** A parameter a form must render, in the vocabulary a form cares about. */
export type SchemaParam =
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
      /** The bound is `> min`, not `>= min`. A form that ignores this offers a refused value. */
      minExclusive?: boolean
      max?: number
      /** The bound is `< max`, not `<= max`. */
      maxExclusive?: boolean
    }
  | { name: string; kind: 'boolean'; required: boolean; default: boolean }

// A JSON Schema node, in the narrow shape this file reads. Deliberately not exhaustive — anything
// unrecognised makes `describe` throw rather than produce a field the form would render wrong.
export interface SchemaNode {
  $ref?: string
  type?: string
  const?: string
  enum?: readonly string[]
  default?: unknown
  anyOf?: readonly SchemaNode[]
  oneOf?: readonly SchemaNode[]
  minimum?: number
  maximum?: number
  exclusiveMinimum?: number
  exclusiveMaximum?: number
  properties?: Record<string, SchemaNode>
  required?: readonly string[]
  components?: readonly unknown[]
  discriminator?: { propertyName: string; mapping: Record<string, string> }
}

/** Resolve a `#/$defs/...` pointer against the document being read. */
export type Resolve = (ref: string) => SchemaNode

/** Build a resolver over a schema document's `$defs`, refusing a pointer it cannot follow. */
export function resolverFor(root: unknown): { defs: Record<string, SchemaNode>; resolve: Resolve } {
  const defs = (root as { $defs?: Record<string, SchemaNode> }).$defs ?? {}
  return {
    defs,
    resolve(ref: string): SchemaNode {
      const node = defs[ref.replace('#/$defs/', '')]
      if (node === undefined) throw new Error(`strategy schema has no definition for ${ref}`)
      return node
    },
  }
}

/** Follow a `$ref` if there is one, keeping the referring node's own keywords — a `$ref` sibling
 *  carries the `default`, which is where `average: "EMA"` lives. */
function deref(node: SchemaNode, resolve: Resolve): SchemaNode {
  return node.$ref === undefined ? node : { ...resolve(node.$ref), ...node }
}

/** The branch of an `anyOf` that is not `null`. Pydantic emits `T | None` that way, and it is how
 *  a parameter says that an explicit null is a setting rather than an absence. */
function nonNull(node: SchemaNode, resolve: Resolve): { branch: SchemaNode; nullable: boolean } {
  if (node.anyOf === undefined) return { branch: node, nullable: false }
  const branches = node.anyOf.map((one) => deref(one, resolve))
  const real = branches.find((branch) => branch.type !== 'null')
  if (real === undefined) throw new Error('a parameter whose only type is null cannot be rendered')
  return { branch: real, nullable: branches.some((branch) => branch.type === 'null') }
}

/** One parameter, described for a form. */
export function describeParam(
  name: string,
  raw: SchemaNode,
  required: boolean,
  resolve: Resolve,
): SchemaParam {
  const node = deref(raw, resolve)
  const { branch, nullable } = nonNull(node, resolve)
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
    // ⚠️ `exclusiveMinimum` is carried as its own flag, never folded into `minimum`. It used
    // to be `minimum ?? exclusiveMinimum`, which turned "greater than 0" into "at least 0" —
    // and every form built on this then offered a value the API refuses. Reported from the
    // screen as a 422 on a study whose `breakeven_at_r` axis started at 0, which is exactly
    // what the hint had said was legal.
    const min = branch.minimum ?? branch.exclusiveMinimum
    const max = branch.maximum ?? branch.exclusiveMaximum
    return {
      name,
      kind: branch.type,
      required,
      default: typeof fallback === 'number' ? fallback : null,
      nullable,
      ...(min === undefined ? {} : { min }),
      ...(branch.minimum === undefined && branch.exclusiveMinimum !== undefined
        ? { minExclusive: true }
        : {}),
      ...(max === undefined ? {} : { max }),
      ...(branch.maximum === undefined && branch.exclusiveMaximum !== undefined
        ? { maxExclusive: true }
        : {}),
    }
  }
  throw new Error(`parameter ${name} has a kind no form control can hold: ${String(branch.type)}`)
}

/**
 * Every parameter of a `params` model, required first and then in the schema's own order.
 *
 * ⚠️ **Required first is not cosmetic, and it is not the schema's order either.** The generator
 * lists properties alphabetically, so a Bollinger's `deviations` comes before its `period` — a
 * form rendering them in sequence puts the multiplier above the window. And on the setup side the
 * parameter with no default is the question the form has to ask; burying it under four pre-filled
 * ones is how it gets skipped, which turns a forgotten choice into a whole backtest read as the
 * setup's result.
 */
export function readParams(paramsNode: SchemaNode, resolve: Resolve): readonly SchemaParam[] {
  const required = new Set(paramsNode.required ?? [])
  const described = Object.entries(paramsNode.properties ?? {}).map(([name, node]) =>
    describeParam(name, node, required.has(name), resolve),
  )
  return [...described.filter((one) => one.required), ...described.filter((one) => !one.required)]
}
