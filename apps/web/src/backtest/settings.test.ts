import {
  costModel,
  emptyBacktestForm,
  toBacktestRequest,
  toIso,
  whyNotRunnable,
  type BacktestForm,
} from './settings'

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
