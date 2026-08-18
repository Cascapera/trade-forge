import type { SchemaParam } from '@tradeforge/schema'

/** A parameter a stepper can drive: the two numeric kinds, with their bounds. */
export type NumericParam = Extract<SchemaParam, { kind: 'integer' | 'number' }>

/**
 * How far one press of an arrow moves this parameter.
 *
 * ⚠️ **Derived from the parameter, never a constant, and that is the whole point of this
 * function existing.** A whole number steps by one; a `number` steps by half its own order of
 * magnitude, so `stop_buffer` (0.1) moves by 0.05 and `breakeven_at_r` (2) moves by 0.5. The
 * constant version is the obvious one and it is wrong in both directions at once: a step of 1
 * turns a tenth into 1.1 — an eleven-fold jump — while a step of 0.1 makes an arrow on a
 * risk multiple something you have to click twenty times.
 *
 * This is the same mistake `axes.exampleFor` was caught making by a probe, one level up: there
 * it *suggested* `0.1, 1.1`, here it would *produce* it a click at a time.
 */
export function stepFor(param: NumericParam): number {
  if (param.kind === 'integer') return 1
  const middle = param.default
  // No default to take a scale from — `0.5` is the same fallback the example generator uses.
  if (middle === null || middle === 0) return 0.5
  return Math.pow(10, Math.floor(Math.log10(Math.abs(middle)))) / 2
}

/** How many decimals a step carries, so arithmetic on it can be rounded back to what it means. */
function decimalsOf(step: number): number {
  const text = String(step)
  const dot = text.indexOf('.')
  return dot === -1 ? 0 : text.length - dot - 1
}

/**
 * The value a press would produce, or `null` when the bound refuses it.
 *
 * ⚠️ **An exclusive bound is honoured as exclusive.** `breakeven_at_r` is `> 0`, and a stepper
 * that stopped at `param.min` would hand back the one value in range the API refuses — which is
 * exactly what the builder's `<input min={param.min}>` does today, and the same defect #106
 * fixed in the hint while leaving it live in the field.
 *
 * `null` rather than a clamp: clamping answers a press with a number nobody asked for, and the
 * caller here disables the arrow instead, which says the same thing where the reader is looking.
 */
export function stepped(param: NumericParam, from: number, direction: 1 | -1): number | null {
  const step = stepFor(param)
  // Floating point: 0.1 + 0.05 is 0.15000000000000002, and a field carrying that is a field
  // nobody typed. Rounded to the step's own precision, which is all the value ever means.
  const next = Number((from + direction * step).toFixed(decimalsOf(step)))

  if (param.min !== undefined) {
    if (param.minExclusive === true ? next <= param.min : next < param.min) return null
  }
  if (param.max !== undefined) {
    if (param.maxExclusive === true ? next >= param.max : next > param.max) return null
  }
  return next
}

/**
 * Where an arrow starts from when the field is empty.
 *
 * The parameter's **default** — the value its author chose — rather than its minimum. Starting
 * at the floor would open every search at the least interesting end of the range, and on
 * `period` that means 1.
 *
 * ⚠️ Empty is not zero. Two of these parameters are nullable and an empty box *means* something
 * on them (`breakeven_at_r` empty is "no break-even", `max_bos` empty is "uncapped"), so
 * pressing an arrow has to be the only thing that fills the box.
 */
export function startingPoint(param: NumericParam): number {
  if (param.default !== null) return param.default
  if (param.min === undefined) return 0
  return param.minExclusive === true ? (stepped(param, param.min, 1) ?? param.min) : param.min
}
