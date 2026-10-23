import { describe, expect, it } from 'vitest'

import { offIsASetting } from './params.js'
import { TAKE_PROFIT_RR, TAKE_PROFIT_RR_PATH, readTakeProfit } from './take_profit.js'

/** A schema shaped like the generator's, small enough to break one piece at a time. */
function schema(over: Record<string, unknown> = {}): unknown {
  return {
    $defs: {
      Exit: {
        properties: {
          take_profit: {
            anyOf: [{ $ref: '#/$defs/RiskMultipleTakeProfit' }, { type: 'null' }],
            default: null,
          },
        },
      },
      RiskMultipleTakeProfit: {
        properties: { params: { $ref: '#/$defs/RiskMultipleParams' } },
      },
      RiskMultipleParams: {
        properties: { rr: { type: 'number', exclusiveMinimum: 0, maximum: 100 } },
        required: ['rr'],
      },
      ...over,
    },
  }
}

describe('the target as an axis', () => {
  it('reads the multiple’s own bounds out of the schema', () => {
    // Derived, never written here: the day `rr` is capped at 50 in Python, this follows.
    expect(TAKE_PROFIT_RR).toEqual({
      name: 'rr',
      kind: 'number',
      required: false,
      default: null,
      nullable: true,
      min: 0,
      minExclusive: true,
      max: 100,
    })
  })

  it('names the path the server knows this axis by', () => {
    expect(TAKE_PROFIT_RR_PATH).toBe('exit.take_profit.params.rr')
  })

  it('says null is a value somebody chooses, because the target itself is optional', () => {
    // ⚠️ `rr` is required *inside* a target and can never be null. What may be absent is the
    // whole block — which is why `off` is legal on this axis and why it is read from `Exit`.
    // Asked through `offIsASetting`, which is the question the form actually asks — and the one
    // place that answers it for every control (`params.ts`).
    expect(offIsASetting(readTakeProfit(schema()))).toBe(true)
  })

  it('refuses a schema where the target stopped being optional', () => {
    // Were it compulsory, `off` would be a point the API refuses — one click, whole sweep 422.
    const compulsory = {
      Exit: { properties: { take_profit: { $ref: '#/$defs/RiskMultipleTakeProfit' } } },
    }
    expect(() => readTakeProfit(schema(compulsory))).toThrow(/no longer optional/)
  })

  it('refuses a schema whose exit, target, params or multiple moved', () => {
    expect(() => readTakeProfit({ $defs: {} })).toThrow(/no Exit definition/)
    expect(() => readTakeProfit(schema({ Exit: { properties: {} } }))).toThrow(/no longer has a/)
    expect(() =>
      readTakeProfit(schema({ RiskMultipleTakeProfit: { properties: {} } })),
    ).toThrow(/no params definition/)
    expect(() =>
      readTakeProfit(schema({ RiskMultipleParams: { properties: {}, required: [] } })),
    ).toThrow(/no longer has an rr/)
  })

  it('refuses a multiple that stopped being a number', () => {
    // A `rr` the form could still render — a flag, or a list of names — and which no longer
    // means "times the risk". `describeParam` is happy with both; this axis must not be.
    const flag = {
      RiskMultipleParams: { properties: { rr: { type: 'boolean' } }, required: ['rr'] },
    }
    const names = {
      RiskMultipleParams: { properties: { rr: { enum: ['near', 'far'] } }, required: ['rr'] },
    }
    expect(() => readTakeProfit(schema(flag))).toThrow(/not a multiple of anything/)
    expect(() => readTakeProfit(schema(names))).toThrow(/not a multiple of anything/)
  })
})
