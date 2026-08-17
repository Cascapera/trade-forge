import { describe, expect, it } from 'vitest'

import {
  INDICATOR_TYPES,
  indicatorSpec,
  readIndicators,
  takesSource,
  type IndicatorType,
} from './indicators.js'

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
    expect(indicatorSpec('SMA').params).toEqual(['period', 'source'])
    expect(indicatorSpec('ATR').params).toEqual(['period'])
    // The one with a parameter no other indicator has. A form driven by this list offers it
    // without anybody adding a case for it.
    //
    // ⚠️ Alphabetical, because that is the order the generated schema lists properties in — which
    // is why `deviations` comes before `period` here. The `SMA` line above reads as declaration
    // order only because `period` happens to sort before `source`. A form rendering these in
    // sequence therefore puts the multiplier above the window; noted in the backlog rather than
    // worked around here, since the fix belongs to whoever owns the form.
    expect(indicatorSpec('BOLLINGER').params).toEqual(['deviations', 'period', 'source'])
  })

  it('follows the discriminator mapping rather than guessing a definition name', () => {
    // ⚠️ `HIGHEST` is defined as `#/$defs/Highest`: the Python class is named for a reader and
    // the DSL type is shouted like every other indicator. Resolving `$defs.HIGHEST` finds
    // nothing — which would surface as "indicator HIGHEST has no params definition" on a
    // contract that is perfectly fine.
    expect(indicatorSpec('HIGHEST').params).toEqual(['period'])
    expect(indicatorSpec('LOWEST').params).toEqual(['period'])
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
