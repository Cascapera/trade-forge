import { setupSpec, type SetupParam } from '@tradeforge/schema'
import { describe, expect, it } from 'vitest'

import { startingPoint, stepFor, stepped, type NumericParam } from './stepping'

/**
 * A real parameter, read from the schema rather than written here.
 *
 * ⚠️ The whole point of these tests is that the arrows move by what the *parameter* is, so a
 * hand-made parameter would be testing a fixture. `stop_buffer` really is a `number` defaulting
 * to 0.1 and `period` really is an integer defaulting to 9 — and the day either changes in
 * Python, this file should be the thing that notices.
 */
function param(setup: Parameters<typeof setupSpec>[0], name: string): NumericParam {
  const found: SetupParam | undefined = setupSpec(setup).params.find((each) => each.name === name)
  if (found === undefined) throw new Error(`${setup} has no parameter ${name}`)
  if (found.kind !== 'integer' && found.kind !== 'number') {
    throw new Error(`${name} is ${found.kind}, which no stepper drives`)
  }
  return found
}

describe('stepFor', () => {
  it('moves a fraction by a fraction and a whole number by one', () => {
    // ⚠️ The bug this function exists to prevent, and it was found by a probe rather than by
    // reasoning: a step of 1 on `stop_buffer` turns 0.1 into 1.1 — eleven times the value —
    // presented to the reader as one press of an arrow.
    expect(stepFor(param('structure_choch', 'stop_buffer'))).toBe(0.05)
    expect(stepFor(param('mme9_breakout', 'period'))).toBe(1)
    // And the opposite mistake, which the old builder made: a constant 0.1 on every fraction
    // makes a risk multiple of 2 take twenty presses to reach 4.
    expect(stepFor(param('mme9_breakout', 'breakeven_at_r'))).toBe(0.5)
  })

  it('has a step for a parameter with no default to take a scale from', () => {
    // `max_bos` defaults to null — uncapped. An arrow still has to move it, and a step derived
    // from `null` would be `NaN`, which silently empties the field on the first press.
    expect(stepFor(param('structure_continuation', 'max_bos'))).toBe(1)
  })
})

describe('stepped', () => {
  it('lands on the value the reader expects, not on the float that produced it', () => {
    // 0.1 + 0.05 is 0.15000000000000002 in binary floating point. A field carrying that is a
    // field nobody typed, and a grid axis carrying it is a strategy name carrying it too.
    expect(stepped(param('structure_choch', 'stop_buffer'), 0.1, 1)).toBe(0.15)
    expect(stepped(param('structure_choch', 'stop_buffer'), 0.15, -1)).toBe(0.1)
  })

  it('refuses to step onto an exclusive bound', () => {
    // ⚠️ `breakeven_at_r` is `> 0`: zero is not "no break-even", it is a value the API refuses.
    // The builder's `<input min={param.min}>` offered exactly this, which is #106 still live in
    // the field after being fixed in the sentence beside it.
    const breakeven = param('mme9_breakout', 'breakeven_at_r')

    expect(stepped(breakeven, 0.5, -1)).toBe(null)
    expect(stepped(breakeven, 1, -1)).toBe(0.5)
  })

  it('stops at an inclusive bound instead of one step past it', () => {
    const period = param('mme9_breakout', 'period')

    expect(stepped(period, 1, -1)).toBe(null)
    expect(stepped(period, 2, -1)).toBe(1)
    expect(stepped(period, 1000, 1)).toBe(null)
    expect(stepped(period, 999, 1)).toBe(1000)
  })
})

describe('startingPoint', () => {
  it('opens on the value the parameter author chose, not on the floor of its range', () => {
    // `period` runs from 1 to 1000. Starting an empty field at 1 opens every search at the least
    // interesting end of the range; the default is the value somebody decided was right.
    expect(startingPoint(param('mme9_breakout', 'period'))).toBe(9)
    expect(startingPoint(param('ponto_continuo', 'period'))).toBe(20)
    expect(startingPoint(param('structure_choch', 'stop_buffer'))).toBe(0.1)
  })

  it('clears an exclusive floor when there is no default to start from', () => {
    // `max_bos` has no default and runs from 1, inclusive — so 1 it is.
    expect(startingPoint(param('structure_continuation', 'max_bos'))).toBe(1)
  })
})

describe('shapes the schema permits but does not yet contain', () => {
  // ⚠️ Hand-made parameters, and deliberately so — unlike every test above.
  //
  // The JSON Schema is generated from Pydantic, so `Field(gt=0, lt=1)` on a new parameter, or a
  // nullable float, produces one of these the day somebody writes it. The question for a guard
  // is not whether it is reachable today but **how it fails when it is**: without them, an
  // arrow offers a value the API refuses, or steps by `NaN` and empties the field. Both are
  // silent, and both are the failure this module was extracted to make impossible.

  const RATIO: NumericParam = {
    name: 'ratio',
    kind: 'number',
    required: false,
    default: 0.5,
    nullable: false,
    min: 0,
    minExclusive: true,
    max: 1,
    maxExclusive: true,
  }

  const DRIFT: NumericParam = {
    name: 'drift',
    kind: 'number',
    required: false,
    default: null,
    nullable: true,
  }

  const GAIN: NumericParam = {
    name: 'gain',
    kind: 'number',
    required: true,
    default: null,
    nullable: false,
    min: 0,
    minExclusive: true,
  }

  it('refuses to step onto an exclusive ceiling, as it does onto an exclusive floor', () => {
    expect(stepped(RATIO, 0.9, 1)).toBe(0.95)
    expect(stepped(RATIO, 0.95, 1)).toBe(null)
  })

  it('steps a number with no default rather than stepping by NaN', () => {
    // `Math.log10(null)` is 0 and the arithmetic that follows silently empties the field.
    expect(stepFor(DRIFT)).toBe(0.5)
    expect(stepped(DRIFT, 0, 1)).toBe(0.5)
    expect(stepped(DRIFT, 0, -1)).toBe(-0.5)
  })

  it('starts at zero when there is neither a default nor a floor to prefer', () => {
    expect(startingPoint(DRIFT)).toBe(0)
  })

  it('starts one step inside an exclusive floor rather than on it', () => {
    // The floor itself is the one value in range the API refuses, and an empty field whose
    // first press lands there is the #106 defect with an extra step in front of it.
    expect(startingPoint(GAIN)).toBe(0.5)
  })
})
