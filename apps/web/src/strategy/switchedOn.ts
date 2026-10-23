import type { SchemaParam } from '@tradeforge/schema'

/**
 * The value a parameter takes when switched back on: its default, else its lowest legal value —
 * one past an exclusive bound, since `rr > 0` refuses the 0 the schema names as its minimum.
 */
export function switchedOn(param: SchemaParam): number {
  if (param.kind !== 'integer' && param.kind !== 'number') return 1
  if (param.default !== null) return param.default
  if (param.min === undefined) return 1
  return param.minExclusive === true ? param.min + 1 : param.min
}
