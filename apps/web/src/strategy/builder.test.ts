import { validateStrategy } from '@tradeforge/schema'

import {
  buildCondition,
  buildStrategy,
  emptyForm,
  maCrossForm,
  OPS,
  TIMEFRAMES,
  type SideForm,
} from './builder'

function side(rows: SideForm['rows'], combine: SideForm['combine'] = 'all'): SideForm {
  return { enabled: true, combine, rows }
}

describe('buildCondition', () => {
  it('is null for a disabled or empty side', () => {
    expect(buildCondition({ enabled: false, combine: 'all', rows: [] })).toBeNull()
    expect(buildCondition(side([]))).toBeNull()
  })

  it('collapses a single row to a bare comparison', () => {
    expect(buildCondition(side([{ left: 'fast', op: 'gt', right: 'slow' }]))).toEqual({
      op: 'gt',
      left: { ref: 'fast' },
      right: { ref: 'slow' },
    })
  })

  it('wraps several rows in all or any', () => {
    const rows = [
      { left: 'a', op: 'gt' as const, right: 'b' },
      { left: 'c', op: 'lt' as const, right: 'd' },
    ]
    expect(buildCondition(side(rows, 'all'))).toEqual({
      all: [
        { op: 'gt', left: { ref: 'a' }, right: { ref: 'b' } },
        { op: 'lt', left: { ref: 'c' }, right: { ref: 'd' } },
      ],
    })
    expect(buildCondition(side(rows, 'any'))).toHaveProperty('any')
  })
})

describe('buildStrategy', () => {
  it('produces a valid MA-cross document from the template', () => {
    const result = validateStrategy(buildStrategy(maCrossForm()))
    expect(result.valid).toBe(true)
  })

  it('omits indicators when there are none and includes them when present', () => {
    const blank = buildStrategy(emptyForm())
    expect(blank).not.toHaveProperty('indicators')

    const withIndicators = buildStrategy({
      ...emptyForm(),
      indicators: [{ id: 'fast', kind: 'SMA', period: 9, source: 'close' }],
    })
    expect(withIndicators.indicators).toEqual([
      { id: 'fast', type: 'SMA', params: { period: 9, source: 'close' } },
    ])
  })

  it('emits the stop and target only when enabled', () => {
    const without = buildStrategy(emptyForm())
    expect(without.exit.stop_loss).toBeNull()
    expect(without.exit.take_profit).toBeNull()

    const form = maCrossForm()
    const withExit = buildStrategy(form)
    expect(withExit.exit.stop_loss).toEqual({
      type: 'candle_extreme',
      params: { lookback: 2, side: 'low' },
    })
    expect(withExit.exit.take_profit).toEqual({ type: 'risk_multiple', params: { rr: 2 } })
  })

  it('always carries percent_risk sizing', () => {
    expect(buildStrategy(emptyForm()).risk.sizing).toEqual({
      type: 'percent_risk',
      params: { percent: 1 },
    })
  })
})

describe('option lists', () => {
  it('expose the DSL timeframes and operators', () => {
    expect(TIMEFRAMES).toContain('H1')
    expect(OPS).toContain('crosses_above')
  })
})
