import { indicatorSpec, SETUP_TYPES, validateStrategy } from '@tradeforge/schema'

import {
  buildCondition,
  buildConditionStrategy,
  buildSetupStrategy,
  buildStrategy,
  catalogueHas,
  CUSTOM_REF_SEED,
  emptyForm,
  emptyRow,
  foldParams,
  indicatorValues,
  maCrossForm,
  OP_GROUPS,
  opOf,
  OPS,
  refCatalogue,
  refOperand,
  retypedValues,
  shapeOf,
  subjectOf,
  valueOperand,
  withOp,
  rsiOversoldForm,
  SETUP_LABELS,
  setupForm,
  setupValues,
  STRATEGY_CHOICES,
  strategyChoice,
  TIMEFRAMES,
  type ConditionRow,
  type IndicatorForm,
  type SideForm,
  type StrategyForm,
} from './builder'

function side(rows: SideForm['rows'], combine: SideForm['combine'] = 'all'): SideForm {
  return { enabled: true, combine, rows }
}

/** A blank row of any shape, with every box a person would have to fill actually filled — so the
 *  only thing a validation failure can be about is the shape the row folded into. */
function fill(row: ConditionRow): ConditionRow {
  switch (row.shape) {
    case 'comparison':
      return { ...row, left: 'fast', right: refOperand('slow') }
    case 'between':
      return { ...row, value: 'fast', low: valueOperand('1'), high: valueOperand('2') }
    case 'trend':
      return { ...row, of: 'fast', bars: '3' }
  }
}

/** The instant every form in this file is "picked" at. Fixed, because a form factory that read the
 *  wall clock could only be tested for having produced something. Naming is proven in
 *  `naming.test.ts`; here the clock just has to be a value. */
const PICKED_AT = new Date(2026, 7, 7, 15, 12, 30)

/** The Ponto Contínuo with its one unanswered field filled in — what a user has after choosing the
 *  setup from the picker and saying which way they trade it. */
function pontoContinuoForm(): StrategyForm {
  const form = setupForm('ponto_continuo', PICKED_AT)
  return { ...form, setup: { ...form.setup, values: { ...form.setup.values, side: 'long' } } }
}

describe('buildCondition', () => {
  it('is null for a disabled or empty side', () => {
    expect(buildCondition({ enabled: false, combine: 'all', rows: [] })).toBeNull()
    expect(buildCondition(side([]))).toBeNull()
  })

  it('collapses a single row to a bare comparison', () => {
    expect(
      buildCondition(side([{ shape: 'comparison', left: 'fast', op: 'gt', right: refOperand('slow') }])),
    ).toEqual({
      op: 'gt',
      left: { ref: 'fast' },
      right: { ref: 'slow' },
    })
  })

  it('folds a value operand into a literal constant', () => {
    expect(
      buildCondition(side([{ shape: 'comparison', left: 'rsi', op: 'lt', right: valueOperand('30') }])),
    ).toEqual({
      op: 'lt',
      left: { ref: 'rsi' },
      right: { value: 30 },
    })
  })

  it('wraps several rows in all or any', () => {
    const rows = [
      { shape: 'comparison' as const, left: 'a', op: 'gt' as const, right: refOperand('b') },
      { shape: 'comparison' as const, left: 'c', op: 'lt' as const, right: refOperand('d') },
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

describe('the three shapes a row can take', () => {
  it('folds a band into the between node, with an operand kind per edge', () => {
    // Mixed on purpose: a floor typed as a number and a ceiling that names an indicator. A fold
    // that hard-coded either kind for the bounds would pass a test where both edges matched.
    const row: ConditionRow = {
      shape: 'between',
      value: 'rsi',
      low: valueOperand('30'),
      high: refOperand('ceiling'),
    }
    expect(buildCondition(side([row]))).toEqual({
      op: 'between',
      value: { ref: 'rsi' },
      low: { value: 30 },
      high: { ref: 'ceiling' },
    })
  })

  it('folds a trend row into the node, carrying the window it was given', () => {
    const row: ConditionRow = { shape: 'trend', of: 'fast', op: 'rising', bars: '3' }
    expect(buildCondition(side([row]))).toEqual({ op: 'rising', of: { ref: 'fast' }, bars: 3 })
  })

  it('leaves bars off the node entirely when the box is empty', () => {
    // ⚠️ Not `bars: 1` written out, and above all not a zero. Omitting the key is how the form
    // says "whatever the engine's own answer is"; sending `0` would ask a question about a window
    // of no bars, which is a different strategy and one nobody chose. `toEqual` alone would not
    // separate the two, because it ignores an explicit `undefined` — hence the property check.
    const row: ConditionRow = { shape: 'trend', of: 'fast', op: 'falling', bars: '' }
    const node = buildCondition(side([row]))
    expect(node).toEqual({ op: 'falling', of: { ref: 'fast' } })
    expect(node).not.toHaveProperty('bars')
  })

  it('emits a schema-valid document for every operator the picker offers', () => {
    // The backlog's warning, turned into a test: adding an operator to the picker without giving
    // its row the arity it needs produces documents the API refuses. Here every offered operator
    // has to survive the same validator the screen runs.
    for (const group of OP_GROUPS) {
      for (const op of group.ops) {
        const filled = fill(withOp(emptyRow(), op))
        const document = buildConditionStrategy({
          ...maCrossForm(PICKED_AT),
          long: side([filled]),
        })
        const result = validateStrategy(document)
        expect(shapeOf(op)).toBe(group.shape)
        // The operator is named in the message, because a bare `false` here would send the reader
        // looking through eleven of them for the one that failed.
        expect(result.valid, `${op}: ${result.valid ? '' : JSON.stringify(result.errors)}`).toBe(
          true,
        )
      }
    }
  })
})

describe('the keys a document does not carry', () => {
  it('writes no description when there is none', () => {
    // ⚠️ `""` is the schema's own default, so writing it adds a key that says nothing — and a
    // key that says nothing is what made re-saving a loaded document rewrite its shape. Same
    // rule as the empty `exit` block on a setup document.
    expect(buildStrategy(maCrossForm(PICKED_AT))).not.toHaveProperty('description')
  })

  it('writes a description when there is one', () => {
    const form = { ...maCrossForm(PICKED_AT), description: 'rompimento com filtro de tendência' }
    expect(buildStrategy(form)).toHaveProperty('description', 'rompimento com filtro de tendência')
  })

  it('writes a risk cap only when the box holds one', () => {
    const blank = buildStrategy(maCrossForm(PICKED_AT))
    expect(blank.risk).not.toHaveProperty('max_open_positions')
    expect(blank.risk).not.toHaveProperty('max_daily_loss_percent')

    const capped = buildStrategy({
      ...maCrossForm(PICKED_AT),
      maxOpenPositions: '2',
      maxDailyLossPercent: '2.5',
    })
    expect(capped.risk).toMatchObject({ max_open_positions: 2, max_daily_loss_percent: 2.5 })
  })
})

describe('an indicator form driven by the schema', () => {
  it('starts every parameter at what the schema declares, and blank where it declares nothing', () => {
    // ⚠️ `period` is blank on purpose. The Pydantic model gives it no default, so a form that
    // pre-filled one would be writing a number the DSL never chose — and the reader would run an
    // SMA of that window believing it was a considered setting. `source` is the opposite case:
    // the schema does say `close`, so the box shows `close`.
    expect(indicatorValues('SMA')).toEqual({ period: '', source: 'close' })
    // The parameter this whole PR exists for. It arrives with the schema's own 2.0, which is why
    // every band built before this could only ever be a 2.0 band: the value was right, and there
    // was no way to say anything else.
    expect(indicatorValues('BOLLINGER')).toEqual({ period: '', source: 'close', deviations: '2' })
    // And an indicator that reads the whole candle has no `source` key at all — not a `source`
    // the boundary later drops.
    expect(indicatorValues('ATR')).toEqual({ period: '' })
  })

  it('folds the typed values into params, and a name the indicator does not have is not folded', () => {
    expect(foldParams(indicatorSpec('BOLLINGER').params, { period: '20', deviations: '2.5' }))
      .toEqual({ period: 20, deviations: 2.5 })
    // ⚠️ `source` is a legal value on other indicators and simply not a parameter of this one.
    // There is no branch deciding that any more — the spec is the only list consulted.
    expect(foldParams(indicatorSpec('ATR').params, { period: '14', source: 'high' })).toEqual({
      period: 14,
    })
  })

  it('carries a value across a kind change wherever the new kind has a place for it', () => {
    // The comparison this exists for: SMA(20) against EMA(20) is one question, and retyping the
    // 20 to ask it is how the question stops being asked.
    expect(retypedValues('EMA', { period: '20', source: 'high' })).toEqual({
      period: '20',
      source: 'high',
    })
  })

  it('drops what the new kind has no place for, and fills the rest from its own defaults', () => {
    // ATR has no `source`, so it goes; BOLLINGER has a `deviations` the previous kind never had,
    // so it arrives at the schema's 2.0 rather than empty.
    expect(retypedValues('ATR', { period: '14', source: 'high' })).toEqual({ period: '14' })
    expect(retypedValues('BOLLINGER', { period: '14' })).toEqual({
      period: '14',
      source: 'close',
      deviations: '2',
    })
  })

  it('refuses to carry a value that is not the kind of thing the parameter holds', () => {
    // ⚠️ No indicator has a flag today, so this guard is about the shape of the rule rather than
    // about a case on screen — and it is the difference between carrying *a value by name* and
    // carrying *a value a control can render*. A checkbox's `true` in a number box is a field
    // nobody can fix by typing.
    expect(retypedValues('SMA', { period: true, source: 'high' })).toEqual({
      period: '',
      source: 'high',
    })
  })
})

describe('the refs the picker can offer', () => {
  const sma = (id: string): IndicatorForm =>
    ({ id, kind: 'SMA', values: { period: '9', source: 'close' } })

  it('always offers the four fields of the candle being decided on', () => {
    expect(refCatalogue([])).toEqual([
      { label: 'this candle', refs: ['price.open', 'price.high', 'price.low', 'price.close'] },
    ])
  })

  it('offers a single-valued indicator by its bare id', () => {
    expect(refCatalogue([sma('fast')])[1]).toEqual({ label: 'indicators', refs: ['fast'] })
  })

  it('offers a composite by component, and never by its bare id', () => {
    // ⚠️ The whole point of the schema change behind this PR. `bb` alone is a document the
    // semantic layer refuses — "the middle band" is a tempting default and a wrong one for an
    // ADX, whose first component is not a price — so the picker must not be able to produce it.
    const refs = refCatalogue([{ id: 'bb', kind: 'BOLLINGER', values: {} }])[1]
    expect(refs).toEqual({ label: 'indicators', refs: ['bb.middle', 'bb.upper', 'bb.lower'] })
    expect(refs?.refs).not.toContain('bb')
  })

  it('leaves out an indicator that has no name yet', () => {
    // A fresh row starts blank, and `price.` prefixed nothing is not a ref anybody can pick.
    expect(refCatalogue([sma('')])).toHaveLength(1)
  })

  it('offers a name once even when two indicators claim it', () => {
    // A duplicate id is a document the semantic layer refuses — and also a state the form passes
    // through while somebody is typing. A picker with the name twice would make a React key
    // collision out of a message the API already gives properly.
    expect(refCatalogue([sma('fast'), sma('fast')])[1]?.refs).toEqual(['fast'])
  })

  it('answers whether a ref is offerable, which is what reveals the free-text box', () => {
    const groups = refCatalogue([sma('fast')])
    expect(catalogueHas(groups, 'price.close')).toBe(true)
    expect(catalogueHas(groups, 'fast')).toBe(true)
    // ⚠️ The form the catalogue cannot hold: N is unbounded, so no list enumerates it. This
    // answering `false` is what keeps the box on screen for it.
    expect(catalogueHas(groups, CUSTOM_REF_SEED)).toBe(false)
    // And a ref left dangling by an indicator that was renamed reads the same way — shown in the
    // box exactly as written, rather than silently swapped for something offerable.
    expect(catalogueHas(groups, 'slow')).toBe(false)
  })
})

describe('changing a row operator', () => {
  const threshold: ConditionRow = {
    shape: 'comparison',
    left: 'rsi',
    op: 'lt',
    right: valueOperand('30'),
  }

  it('keeps the whole row when the shape does not change', () => {
    expect(withOp(threshold, 'gte')).toEqual({ ...threshold, op: 'gte' })
  })

  it('carries the series across shapes and starts the new bounds empty', () => {
    // ⚠️ The `30` does **not** become the floor of the band. It was the far side of `rsi < 30`;
    // as the low edge of `between` it is a number the author never chose, and a form that moved
    // it there would write a strategy by itself.
    expect(withOp(threshold, 'between')).toEqual({
      shape: 'between',
      value: 'rsi',
      low: valueOperand(''),
      high: valueOperand(''),
    })
    expect(withOp(threshold, 'rising')).toEqual({
      shape: 'trend',
      of: 'rsi',
      op: 'rising',
      bars: '',
    })
  })

  it('carries the series back out of a band', () => {
    const band: ConditionRow = {
      shape: 'between',
      value: 'rsi',
      low: valueOperand('30'),
      high: valueOperand('70'),
    }
    expect(withOp(band, 'gt')).toEqual({
      shape: 'comparison',
      left: 'rsi',
      op: 'gt',
      right: refOperand(''),
    })
    expect(subjectOf(withOp(band, 'falling'))).toBe('rsi')
  })

  it('switches direction inside the trend shape without losing the window', () => {
    const row: ConditionRow = { shape: 'trend', of: 'fast', op: 'rising', bars: '4' }
    expect(withOp(row, 'falling')).toEqual({ ...row, op: 'falling' })
  })

  it('answers the band with itself, because the band has no operator field to patch', () => {
    const band: ConditionRow = {
      shape: 'between',
      value: 'rsi',
      low: valueOperand('30'),
      high: valueOperand('70'),
    }
    expect(withOp(band, 'between')).toEqual(band)
    expect(opOf(band)).toBe('between')
  })
})

describe('buildStrategy', () => {
  it('produces a valid MA-cross document from the template', () => {
    const result = validateStrategy(buildStrategy(maCrossForm(PICKED_AT)))
    expect(result.valid).toBe(true)
  })

  it('produces a valid RSI document with literal thresholds from the template', () => {
    // The narrowed builder, so `entry` reads without a null check: `buildStrategy` returns the
    // union of both document shapes now, and only this one always carries entry conditions.
    const document = buildConditionStrategy(rsiOversoldForm(PICKED_AT))
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
      indicators: [{ id: 'fast', kind: 'SMA', values: { period: '9', source: 'close' } }],
    })
    expect(withIndicators.indicators).toEqual([
      { id: 'fast', type: 'SMA', params: { period: 9, source: 'close' } },
    ])
  })

  it('leaves the price source off an indicator that reads the whole candle', () => {
    // ⚠️ ATR and the two channels are defined over high, low and the previous close together,
    // and their params model forbids extra keys. Carrying a `source` through would build a
    // document the API refuses — and refuses with a message about an unexpected field rather
    // than about the indicator that was chosen, which is a report nobody can act on.
    //
    // ⚠️ **The form is given one here anyway, and that is the point of the fixture.** It used to
    // be impossible for this to go wrong in an interesting way, because the boundary had an
    // explicit "does this one take a source?" branch. There is no branch any more: the spec says
    // which parameters exist, so a stray value under a name the indicator does not have is
    // simply not folded. Handing it one proves that, instead of proving the form never has one.
    const built = buildStrategy({
      ...emptyForm(),
      indicators: [
        { id: 'atr', kind: 'ATR', values: { period: '14', source: 'high' } },
        { id: 'canal', kind: 'HIGHEST', values: { period: '20', source: 'close' } },
      ],
    })

    expect(built.indicators).toEqual([
      { id: 'atr', type: 'ATR', params: { period: 14 } },
      { id: 'canal', type: 'HIGHEST', params: { period: 20 } },
    ])
    // And the whole document passes the same validator the API runs — the assertion that makes
    // the one above about a contract rather than about a shape this test happens to prefer.
    const checked = validateStrategy({
      ...built,
      name: 'canal com filtro de ATR',
      entry: { long: { op: 'gt', left: { ref: 'atr' }, right: { value: 0 } }, short: null },
    })
    expect(checked.valid ? [] : checked.errors).toEqual([])
  })

  it('emits the stop and target only when enabled', () => {
    const without = buildConditionStrategy(emptyForm())
    expect(without.exit.stop_loss).toBeNull()
    expect(without.exit.take_profit).toBeNull()

    const form = maCrossForm(PICKED_AT)
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
    // ⚠️ **Absent, not `null`.** These used to be written as `stop_loss: null, conditions: []`,
    // which is the schema's own default spelled out — keys that say nothing. Writing them made
    // opening a saved setup document and saving it again rewrite its shape, which is exactly the
    // loss the round-trip in `parse.test.ts` exists to forbid.
    expect(document.exit).not.toHaveProperty('stop_loss')
    expect(document.exit).not.toHaveProperty('conditions')
  })

  it('leaves the whole exit block out when there is nothing to put in it', () => {
    // A setup with no target carries no exit at all, matching `setup_mme9_breakout` in the
    // fixtures — the document says nothing rather than saying "nothing" three times.
    const form = pontoContinuoForm()
    const document = buildSetupStrategy({ ...form, takeProfit: { enabled: false, rr: 5 } })
    expect(document).not.toHaveProperty('exit')
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

describe('the strategy picker', () => {
  it('offers every setup the schema names, plus the worked condition examples', () => {
    expect(STRATEGY_CHOICES.filter((c) => c.group === 'Setups').map((c) => c.id)).toEqual([
      ...SETUP_TYPES,
    ])
    expect(STRATEGY_CHOICES.filter((c) => c.group === 'Conditions').map((c) => c.id)).toEqual([
      'ma_cross',
      'rsi_oversold',
    ])
  })

  it('names every setup, so none is offered as a raw schema key', () => {
    for (const type of SETUP_TYPES) {
      expect(SETUP_LABELS[type]).toBeTruthy()
      expect(SETUP_LABELS[type]).not.toBe(type)
    }
  })

  it('builds a runnable form for every choice, given the one field only a user can answer', () => {
    for (const choice of STRATEGY_CHOICES) {
      const form = choice.form(PICKED_AT)
      const answered: StrategyForm =
        'side' in form.setup.values && form.setup.values.side === ''
          ? { ...form, setup: { ...form.setup, values: { ...form.setup.values, side: 'long' } } }
          : form
      expect(validateStrategy(buildStrategy(answered)).valid).toBe(true)
    }
  })

  it('names an unknown choice as an error rather than silently loading nothing', () => {
    expect(() => strategyChoice('escada')).toThrow(/no strategy named escada/)
  })

  it('starts a setup on the 5R target the author trades', () => {
    expect(setupForm('mme9_breakout', PICKED_AT).takeProfit).toEqual({ enabled: true, rr: 5 })
  })

  it('leaves side unanswered, because picking a setup is not picking a direction', () => {
    // The picker says *which* setup. Pre-filling the direction here would be it quietly answering
    // the one question the schema deliberately asks.
    const form = setupForm('ponto_continuo', PICKED_AT)
    expect(form.setup.values.side).toBe('')
    expect(validateStrategy(buildStrategy(form)).valid).toBe(false)
  })
})
