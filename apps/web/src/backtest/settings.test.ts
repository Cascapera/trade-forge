import type { Instrument } from '../api/types'
import {
  costModel,
  costlessReason,
  emptyBacktestForm,
  toBacktestRequest,
  toIso,
  whyNotRunnable,
  withInstrumentCosts,
  type BacktestForm,
} from './settings'

function instrument(patch: Partial<Instrument> = {}): Instrument {
  return {
    id: 'i1',
    symbol: 'AAPL',
    name: 'Apple Inc.',
    asset_class: 'stock',
    currency_quote: 'USD',
    currency_base: null,
    tick_size: '0.01',
    tick_value: '0.01',
    contract_size: '1',
    digits: 2,
    default_spread_points: '1.0000000000',
    ...patch,
  }
}

function form(patch: Partial<BacktestForm> = {}): BacktestForm {
  return { ...emptyBacktestForm(), symbol: 'AAPL', ...patch }
}

describe('folding the settings into a request', () => {
  it('reads a date input as midnight UTC', () => {
    expect(toIso('2024-01-01')).toBe('2024-01-01T00:00:00Z')
  })

  it('carries the strategy and the window', () => {
    expect(toBacktestRequest(form(), 's1', 'H1')).toEqual({
      strategy_id: 's1',
      symbol: 'AAPL',
      timeframe: 'H1',
      date_from: '2024-01-01T00:00:00Z',
      date_to: '2024-12-31T00:00:00Z',
      initial_capital: '10000',
      cost_model: { type: 'none' },
    })
  })

  it('keeps the capital a string all the way to the wire', () => {
    // It is money: the API parses it into a `Decimal`. Routing it through a JavaScript number
    // first is how a balance picks up binary dust that then compounds through every trade.
    const request = toBacktestRequest(form({ capital: '10000.10' }), 's1', 'H1')
    expect(request.initial_capital).toBe('10000.10')
    expect(typeof request.initial_capital).toBe('string')
  })

  it('takes the timeframe from the strategy rather than from these settings', () => {
    // Asking twice is how an H1 strategy gets run over M5 candles and reports it without a word.
    expect(toBacktestRequest(form(), 's1', 'M15').timeframe).toBe('M15')
    expect(emptyBacktestForm()).not.toHaveProperty('timeframe')
  })

  it('builds the cost model the choice selects', () => {
    expect(costModel(form())).toEqual({ type: 'none' })
    expect(costModel(form({ cost: 'spread', spreadPoints: '25' }))).toEqual({
      type: 'spread',
      spread_points: 25,
    })
  })

  it('leaves the spread out entirely when costs are off, rather than sending a zero', () => {
    expect(costModel(form({ cost: 'none', spreadPoints: '25' }))).not.toHaveProperty('spread_points')
  })
})

describe('why a run cannot start', () => {
  it('is silent when everything is answered', () => {
    expect(whyNotRunnable(form())).toBeNull()
  })

  it.each([
    ['no instrument chosen', { symbol: '' }, /choose an instrument/],
    ['no window', { dateFrom: '' }, /set the backtest window/],
    ['a backwards window', { dateFrom: '2024-12-31', dateTo: '2024-01-01' }, /ends before it starts/],
    ['a same-day window', { dateFrom: '2024-05-01', dateTo: '2024-05-01' }, /ends before it starts/],
    ['no capital', { capital: '0' }, /capital must be positive/],
    ['negative capital', { capital: '-1' }, /capital must be positive/],
    ['a negative spread', { cost: 'spread' as const, spreadPoints: '-5' }, /a magnitude/],
  ])('names %s', (_label, patch, expected) => {
    expect(whyNotRunnable(form(patch))).toMatch(expected)
  })

  it('ignores the spread when costs are off', () => {
    expect(whyNotRunnable(form({ cost: 'none', spreadPoints: '-5' }))).toBeNull()
  })
})

describe('costs that come from the instrument', () => {
  it('switches costs on with the number the catalogue measured', () => {
    // Switched *on*, not merely pre-filled. The default that produced this project's first
    // thirty-six runs was `none`, and the forex ones are overstated by 5–10% of R per trade
    // as a result. The honest run has to be the one that happens without thinking about it.
    const next = withInstrumentCosts(form({ cost: 'none' }), instrument())

    expect(next.cost).toBe('spread')
    expect(next.spreadPoints).toBe('1')
  })

  it('strips the decimal tail a numeric column comes back with', () => {
    // `12.0000000000` and `12` are the same number, and only one of them is readable in a
    // field a person edits.
    const next = withInstrumentCosts(form(), instrument({ default_spread_points: '12.0000000000' }))
    expect(next.spreadPoints).toBe('12')
  })

  it('falls back to charging nothing when nobody has measured the instrument', () => {
    // Never to zero dressed up as a measurement: a spread of 0 would say the instrument is
    // free to trade, which is a claim. `none` plus a stated reason is the honest pair.
    const next = withInstrumentCosts(form(), instrument({ default_spread_points: null }))

    expect(next.cost).toBe('none')
    expect(next.spreadPoints).toBe('')
  })

  it('falls back the same way when the instrument is not loaded yet', () => {
    const next = withInstrumentCosts(form(), undefined)
    expect(next.cost).toBe('none')
  })

  it('replaces the previous instrument’s spread rather than keeping it', () => {
    // The spread belongs to the symbol, not to the run. Carrying EURUSD's 12 over to AAPL
    // would charge twelve times the real cost with nothing on screen saying so.
    const eurusd = form({ cost: 'spread', spreadPoints: '12' })
    const next = withInstrumentCosts({ ...eurusd, symbol: 'AAPL' }, instrument())

    expect(next.spreadPoints).toBe('1')
  })
})

describe('costlessReason', () => {
  it('says nothing while a run does charge costs', () => {
    expect(costlessReason(form({ cost: 'spread', spreadPoints: '12' }), instrument())).toBeNull()
  })

  it('says nothing before an instrument is chosen', () => {
    // There is no honest thing to charge for a symbol nobody named, and a warning here would
    // fire on every fresh form — which is how a warning stops being read.
    expect(costlessReason(form({ symbol: '', cost: 'none' }), undefined)).toBeNull()
  })

  it('distinguishes a deliberate choice from a gap in the catalogue', () => {
    // The two costless runs are not the same thing: one is "you turned costs off", the other
    // is "nobody ever measured this symbol". Collapsing them would hide which one you have.
    const chosen = costlessReason(form({ cost: 'none' }), instrument())
    const unmeasured = costlessReason(form({ cost: 'none' }), instrument({ default_spread_points: null }))

    expect(chosen).toMatch(/switched off/)
    expect(unmeasured).toMatch(/no spread has been catalogued/)
    expect(chosen).not.toBe(unmeasured)
  })
})
