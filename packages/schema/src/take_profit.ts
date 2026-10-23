// The target's axis: the one grid axis that names a rule which may be absent.
//
// Every other axis varies a parameter of something the document already has. A take profit is a
// whole block — `{type: 'risk_multiple', params: {rr: 3}}` — or `null`, which is a strategy that
// conducts its own stop and wants no target at all. His ask (22/09): search both in one sweep.
//
// The axis names the **leaf** (`exit.take_profit.params.rr`) rather than the block, so its values
// stay numbers and `null` — printable in a label, plottable on a heatmap, sortable in a column.
// Building or removing the block from that leaf is the server's rule (`grid.TAKE_PROFIT_RR`).
//
// Nothing here writes a bound: `rr`'s own node carries them, and the nullability comes from the
// block being optional in `Exit`. Both are read out of the JSON Schema, for the reason `setups.ts`
// gives — a copy kept beside the schema is the copy that silently disagrees with it.

import { describeParam, resolverFor, type SchemaNode, type SchemaParam } from './params.js'
import schema from './tradeforge_schema/strategy.schema.json' with { type: 'json' }

/** The dotted path a grid carries for the target. The server knows this one by name. */
export const TAKE_PROFIT_RR_PATH = 'exit.take_profit.params.rr'

/**
 * The target as a grid axis, read out of a JSON Schema document.
 *
 * The schema arrives as a parameter, like `readSetups`, so every refusal below is reachable from
 * a test with a hand-made document: each one guards a way the DSL could move underneath this file
 * — a target that stopped being optional, a multiple that stopped being a number — and a guard
 * nothing exercises is a guess about the generator rather than a fact about it.
 */
export function readTakeProfit(root: unknown): SchemaParam {
  const { defs, resolve } = resolverFor(root)
  const exit = defs.Exit
  if (exit === undefined) throw new Error('the strategy schema has no Exit definition')

  const target = exit.properties?.take_profit
  if (target === undefined) throw new Error('Exit no longer has a take_profit')
  if (!optional(target)) {
    // Were a target ever compulsory, `off` would be a value the API refuses — the axis would
    // offer a point that fails the whole sweep, which is the defect `offIsASetting` exists for.
    throw new Error('take_profit is no longer optional, so `off` would not be a legal value')
  }

  const params = resolve(refOf(target)).properties?.params?.$ref
  if (params === undefined) throw new Error('a take_profit has no params definition')
  const rr = resolve(params).properties?.rr
  if (rr === undefined) throw new Error('a take_profit no longer has an rr')

  const described = describeParam('rr', rr, true, resolve)
  if (described.kind !== 'number' && described.kind !== 'integer') {
    throw new Error(`rr is ${described.kind}, which is not a multiple of anything`)
  }
  // ⚠️ **`nullable` is the block's, not the leaf's.** `rr` itself is required inside a target and
  // can never be null; what may be absent is the target. Saying so here is what lets the one
  // question about `off` (`offIsASetting`) answer for this axis too, instead of the screen
  // learning a second rule. `required` follows for the same reason: an axis is a question a
  // person opted into, never a field a form must make them answer.
  return { ...described, nullable: true, required: false }
}

function optional(node: SchemaNode): boolean {
  return (node.anyOf ?? []).some((one) => one.type === 'null')
}

function refOf(node: SchemaNode): string {
  const ref = node.$ref ?? (node.anyOf ?? []).find((one) => one.$ref !== undefined)?.$ref
  if (ref === undefined) throw new Error('take_profit names no target definition')
  return ref
}

/** The target's axis for this build of the DSL. */
export const TAKE_PROFIT_RR: SchemaParam = readTakeProfit(schema)
