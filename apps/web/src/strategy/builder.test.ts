import { SETUP_TYPES, validateStrategy } from '@tradeforge/schema'

import {
  buildCondition,
  buildConditionStrategy,
  buildSetupStrategy,
  buildStrategy,
  emptyForm,
  maCrossForm,
  OPS,
  pontoContinuoForm,
  rsiOversoldForm,
  setupValues,
  TIMEFRAMES,
  withMode,
  type SideForm,
  type StrategyForm,
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
    expect(
      buildCondition(side([{ left: 'fast', op: 'gt', right: 'slow', rightKind: 'ref' }])),
    ).toEqual({
      op: 'gt',
      left: { ref: 'fast' },
      right: { ref: 'slow' },
    })
  })

  it('folds a value operand into a literal constant', () => {
    expect(
      buildCondition(side([{ left: 'rsi', op: 'lt', right: '30', rightKind: 'value' }])),
    ).toEqual({
      op: 'lt',
      left: { ref: 'rsi' },
      right: { value: 30 },
    })
  })

  it('wraps several rows in all or any', () => {
    const rows = [
      { left: 'a', op: 'gt' as const, right: 'b', rightKind: 'ref' as const },
      { left: 'c', op: 'lt' as const, right: 'd', rightKind: 'ref' as const },
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

  it('produces a valid RSI document with literal thresholds from the template', () => {
    // The narrowed builder, so `entry` reads without a null check: `buildStrategy` returns the
    // union of both document shapes now, and only this one always carries entry conditions.
    const document = buildConditionStrategy(rsiOversoldForm())
    expect(validateStrategy(document).valid).toBe(true)
    expect(document.entry.long).toEqual({
      op: 'crosses_below',
      left: { ref: 'rsi' },
      right: { value: 30 },
    })
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
    const without = buildConditionStrategy(emptyForm())
    expect(without.exit.stop_loss).toBeNull()
    expect(without.exit.take_profit).toBeNull()

    const form = maCrossForm()
    const withExit = buildConditionStrategy(form)
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

describe('buildSetupStrategy', () => {
  it('produces a valid Ponto Contínuo document from the template', () => {
    const document = buildSetupStrategy(pontoContinuoForm())
    expect(validateStrategy(document).valid).toBe(true)
    expect(document.setup).toEqual({
      type: 'ponto_continuo',
      params: {
        side: 'long',
        period: 20,
        average: 'EMA',
        stop_buffer_ticks: 0,
        breakeven_at_r: 2,
      },
    })
  })

  it("carries the author's 5R target and nothing the setup owns itself", () => {
    const document = buildSetupStrategy(pontoContinuoForm())
    expect(document.exit?.take_profit).toEqual({ type: 'risk_multiple', params: { rr: 5 } })
    // A setup declares its own indicators, is the entry, and places its own stop. The semantic
    // layer refuses a setup document that carries any of them, so the builder must not emit them.
    expect(document).not.toHaveProperty('indicators')
    expect(document).not.toHaveProperty('entry')
    expect(document.exit?.stop_loss).toBeNull()
    expect(document.exit?.conditions).toEqual([])
  })

  it('fills every parameter with the number the schema declares', () => {
    // The defaults shown in the form are the engine's own (ADR-0019). This is the assertion that
    // the form reads them rather than falling back to an empty box or a zero.
    expect(buildSetupStrategy({ ...emptyForm(), mode: 'setup' }).setup.params).toMatchObject({
      period: 20,
      average: 'EMA',
      breakeven_at_r: 2,
    })
  })

  it('leaves side out when it was never answered, so the schema refuses the document', () => {
    // Rather than defaulting to long, which would silently produce a one-directional backtest read
    // as the setup's result.
    const blank = buildSetupStrategy({ ...emptyForm(), mode: 'setup' })
    expect(blank.setup.params).not.toHaveProperty('side')
    expect(validateStrategy(blank).valid).toBe(false)
  })

  it('reads a cleared nullable field as null — the rule switched off, not left unset', () => {
    const form = pontoContinuoForm()
    const off = buildSetupStrategy({
      ...form,
      setup: { ...form.setup, values: { ...form.setup.values, breakeven_at_r: '' } },
    })
    expect(off.setup.params).toMatchObject({ breakeven_at_r: null })
    expect(validateStrategy(off).valid).toBe(true)
  })

  it('drops a cleared non-nullable field so the engine default applies', () => {
    const form = pontoContinuoForm()
    const document = buildSetupStrategy({
      ...form,
      setup: { ...form.setup, values: { ...form.setup.values, period: '' } },
    })
    // Not a zero, and not a NaN: absent, which is how the document says "whatever the class uses".
    expect(document.setup.params).not.toHaveProperty('period')
    expect(validateStrategy(document).valid).toBe(true)
  })

  it('builds every setup the schema names', () => {
    // The drift guard: a setup type added in Python must be buildable here without an edit. If the
    // form cannot produce a valid document for one of them, this is what says so.
    for (const type of SETUP_TYPES) {
      const values = setupValues(type)
      const form: StrategyForm = {
        ...pontoContinuoForm(),
        setup: { type, values: 'side' in values ? { ...values, side: 'long' } : values },
      }
      expect(validateStrategy(buildSetupStrategy(form)).valid).toBe(true)
    }
  })
})

describe('switching between the two shapes', () => {
  it('brings the 5R target along when a setup is chosen', () => {
    expect(withMode(emptyForm(), 'setup').takeProfit).toEqual({ enabled: true, rr: 5 })
  })

  it("overrides the condition half's target rather than inheriting it", () => {
    // The templates ship a 2R target. Carrying it into a setup would run the setup against a
    // target its author never chose — a plausible backtest answering the wrong question.
    const form = { ...maCrossForm(), takeProfit: { enabled: true, rr: 2 } }
    expect(withMode(form, 'setup').takeProfit).toEqual({ enabled: true, rr: 5 })
  })

  it('leaves the target alone once already in setup mode', () => {
    const chosen: StrategyForm = {
      ...pontoContinuoForm(),
      takeProfit: { enabled: true, rr: 3 },
    }
    expect(withMode(chosen, 'setup').takeProfit).toEqual({ enabled: true, rr: 3 })
  })

  it('keeps both halves of the form, so toggling to compare costs nothing', () => {
    const conditions = maCrossForm()
    const there = withMode(conditions, 'setup')
    const back = withMode(there, 'conditions')
    expect(back.long).toEqual(conditions.long)
    expect(back.indicators).toEqual(conditions.indicators)
    expect(there.setup).toEqual(conditions.setup)
  })

  it('routes buildStrategy to the shape the mode selects', () => {
    expect(buildStrategy(maCrossForm())).toHaveProperty('entry')
    expect(buildStrategy(pontoContinuoForm())).toHaveProperty('setup')
  })
})
