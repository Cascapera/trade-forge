import { describe, expect, it } from 'vitest'

import {
  INDICATOR_TYPES,
  indicatorSpec,
  readIndicators,
  refsFor,
  takesSource,
  type IndicatorType,
} from './indicators.js'

/** The parameter names of an indicator, in the order a form would render them. */
function names(type: IndicatorType): readonly string[] {
  return indicatorSpec(type).params.map((param) => param.name)
}

function param(type: IndicatorType, name: string) {
  const found = indicatorSpec(type).params.find((candidate) => candidate.name === name)
  if (found === undefined) throw new Error(`${type} has no parameter ${name}`)
  return found
}

describe('the indicators a document may declare', () => {
  it('is the schema own list, not one written here', () => {
    // Sorted so the assertion is about membership rather than about the order Pydantic happened
    // to emit — the order is the schema's business and changing it is not a contract break.
    expect([...INDICATOR_TYPES].sort()).toEqual([
      'ADX',
      'ATR',
      'BOLLINGER',
      'EMA',
      'HIGHEST',
      'LOWEST',
      'RSI',
      'SMA',
    ])
  })

  it('knows which of them read one price series and which read the whole candle', () => {
    // ⚠️ The distinction this module exists for. `PeriodParams` forbids extra keys, so a form
    // that emitted `source: "close"` for an ATR would build a document the API refuses — and it
    // would refuse it with a message about an unexpected field, not about the indicator the
    // reader picked.
    //
    // ⚠️ And Bollinger is on the priced side, which is where this module earned its keep: the
    // file's own comment predicted "ADX and Bollinger are next, and both are sourceless", and it
    // was half wrong. A band is an average of one price series with an envelope around it, so it
    // takes a source; ADX is defined over high, low and the previous close together, so it cannot.
    // Nothing had to be edited for the form to get that right — it is read, not remembered.
    const priced: IndicatorType[] = ['SMA', 'EMA', 'RSI', 'BOLLINGER']
    const whole: IndicatorType[] = ['ATR', 'HIGHEST', 'LOWEST', 'ADX']

    expect(priced.map(takesSource)).toEqual([true, true, true, true])
    expect(whole.map(takesSource)).toEqual([false, false, false, false])
  })

  it('reads the parameters of each one off its own params model', () => {
    expect(names('SMA')).toEqual(['period', 'source'])
    expect(names('ATR')).toEqual(['period'])
    // The one with a parameter no other indicator has. A form driven by this list offers it
    // without anybody adding a case for it — and for a whole release it did not, because the form
    // rendered hand-written fields and `deviations` was never one of them.
    //
    // ⚠️ **`period` comes first, and the schema does not list it first.** The generator emits
    // properties alphabetically, so `deviations` sorts ahead of `period` — and rendering that
    // order puts the multiplier above the window it multiplies. `period` is the only one of the
    // three with no default, so required-first is on its own enough to pull it back to the top;
    // the two that follow keep the schema's order, which is harmless between them.
    expect(names('BOLLINGER')).toEqual(['period', 'deviations', 'source'])
  })

  it('describes a parameter with everything a control needs, not just its name', () => {
    // ⚠️ `> 0`, not `>= 0`, and the flag is what carries the difference. A multiplier of zero
    // collapses the three bands onto the average — an SMA spelled in a way that hides that it is
    // one — so the schema bounds it exclusively, and a stepper that stopped at `min` would offer
    // the one value in range the API refuses. That defect was #106, on another parameter.
    expect(param('BOLLINGER', 'deviations')).toEqual({
      name: 'deviations',
      kind: 'number',
      required: false,
      default: 2,
      nullable: false,
      min: 0,
      minExclusive: true,
      max: 10,
    })
    expect(param('SMA', 'period')).toMatchObject({ kind: 'integer', min: 1, max: 1000 })
    expect(param('SMA', 'source')).toMatchObject({
      kind: 'enum',
      default: 'close',
      options: ['open', 'high', 'low', 'close'],
    })
  })

  it('follows the discriminator mapping rather than guessing a definition name', () => {
    // ⚠️ `HIGHEST` is defined as `#/$defs/Highest`: the Python class is named for a reader and
    // the DSL type is shouted like every other indicator. Resolving `$defs.HIGHEST` finds
    // nothing — which would surface as "indicator HIGHEST has no params definition" on a
    // contract that is perfectly fine.
    expect(names('HIGHEST')).toEqual(['period'])
    expect(names('LOWEST')).toEqual(['period'])
  })

  it('reads the component names of the two composites, primary first', () => {
    // ⚠️ Order, not membership. The chart draws the first component solid and thick as the
    // subject of its family, so a set comparison here would pass while the upper band was being
    // drawn as the subject of its own average.
    expect(indicatorSpec('BOLLINGER').components).toEqual(['middle', 'upper', 'lower'])
    expect(indicatorSpec('ADX').components).toEqual(['adx', 'plus_di', 'minus_di'])
  })

  it('reports no components for a single-valued indicator', () => {
    // The negative half, and the one that decides how a ref is spelled: an SMA that reported
    // components would make `fast` alone unofferable, and that is the spelling every strategy
    // saved so far uses.
    expect(indicatorSpec('SMA').components).toEqual([])
    expect(indicatorSpec('ATR').components).toEqual([])
  })

  it('spells a ref by the bare id, or by component, according to the indicator', () => {
    expect(refsFor('SMA', 'fast')).toEqual(['fast'])
    // ⚠️ Never the bare `bb`. The semantic layer refuses it — "the middle band" is a tempting
    // default and a wrong one for `adx`, whose first component is not a price at all.
    expect(refsFor('BOLLINGER', 'bb')).toEqual(['bb.middle', 'bb.upper', 'bb.lower'])
    expect(refsFor('ADX', 'trend')).toEqual(['trend.adx', 'trend.plus_di', 'trend.minus_di'])
  })

  it('refuses an indicator that publishes a component which is not a name', () => {
    // ⚠️ The alternative reading is `[]`, which is indistinguishable from "single-valued" — so
    // the generator changing under this file would silently make every `bb.upper` unofferable
    // instead of failing.
    expect(() =>
      readIndicators({
        $defs: {
          Indicator: { discriminator: { propertyName: 'type', mapping: { WAT: '#/$defs/Wat' } } },
          Wat: { properties: { params: { $ref: '#/$defs/P' } }, components: ['upper', 3] },
          P: { properties: { period: { type: 'integer' } } },
        },
      }),
    ).toThrow(/component that is not a name/)
  })

  it('refuses a schema whose indicator union lost its discriminator', () => {
    // The guard is reachable from a test with a hand-made document, for the reason `setups.ts`
    // gives: a guard nothing exercises is a guess about what the generator emits.
    expect(() => readIndicators({ $defs: { Indicator: {} } })).toThrow(/discriminator/)
  })

  it('refuses an indicator whose params cannot be resolved', () => {
    expect(() =>
      readIndicators({
        $defs: {
          Indicator: { discriminator: { propertyName: 'type', mapping: { WAT: '#/$defs/Wat' } } },
          Wat: { properties: {} },
        },
      }),
    ).toThrow(/no params definition/)
  })

  it('refuses a mapping that points at a definition the schema does not have', () => {
    expect(() =>
      readIndicators({
        $defs: {
          Indicator: { discriminator: { propertyName: 'type', mapping: { WAT: '#/$defs/Gone' } } },
        },
      }),
    ).toThrow(/no definition for/)
  })

  it('refuses to describe an indicator that does not exist', () => {
    // @ts-expect-error — the point is the runtime guard, for a name that reached here as data.
    expect(() => indicatorSpec('MACD')).toThrow(/no indicator named MACD/)
  })
})
