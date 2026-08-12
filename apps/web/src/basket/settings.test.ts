import { ApiError } from '../api/client'
import type { BacktestListItem, Instrument } from '../api/types'
import {
  MAX_SYMBOLS,
  basketLabel,
  emptyBasketForm,
  launchFailure,
  measuredSpread,
  newlyComparable,
  toBasketRequest,
  toggleSymbol,
  uncostedAmong,
  whyNotLaunchable,
} from './settings'

function instrument(symbol: string, spread: string | null): Instrument {
  return {
    id: `i-${symbol}`,
    symbol,
    name: symbol,
    asset_class: 'forex',
    currency_quote: 'USD',
    currency_base: 'EUR',
    tick_size: '0.00001',
    tick_value: '1',
    contract_size: '100000',
    digits: 5,
    default_spread_points: spread,
  }
}

function listed(over: Partial<BacktestListItem>): BacktestListItem {
  return {
    id: 'b1',
    strategy_id: 's1',
    strategy_name: 'Ponto Contínuo',
    strategy_version: 2,
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-12-31T00:00:00Z',
    initial_capital: '10000',
    cost_model: { type: 'none' },
    status: 'done',
    error: null,
    created_at: '2026-08-12T12:00:00Z',
    finished_at: '2026-08-12T12:00:30Z',
    metrics: { net_profit: '100' } as BacktestListItem['metrics'],
    ...over,
  }
}

describe('toggleSymbol', () => {
  it('appends on tick and keeps the order the markets were chosen in', () => {
    let form = emptyBasketForm()
    form = toggleSymbol(form, 'GBPUSD')
    form = toggleSymbol(form, 'EURUSD')

    // Not sorted: the API returns the runs in the order they were asked for, so the confirmation
    // the reader gets back matches the list they built.
    expect(form.symbols).toEqual(['GBPUSD', 'EURUSD'])
  })

  it('removes on untick and leaves the survivors where they were', () => {
    let form = emptyBasketForm()
    for (const symbol of ['A', 'B', 'C']) form = toggleSymbol(form, symbol)

    form = toggleSymbol(form, 'A')

    // The point of the assertion is the *order*, not the membership: unticking the first market
    // must not reshuffle the rest, or a reader narrowing a basket would find it rebuilt.
    expect(form.symbols).toEqual(['B', 'C'])
  })
})

describe('measuredSpread', () => {
  it('trims the decimal column trailing zeros a person should not have to read', () => {
    expect(measuredSpread(instrument('EURUSD', '8.0000000000'))).toBe('8')
  })

  it('reports no measurement as null and never as zero', () => {
    // ⚠️ The distinction the whole cost preview rests on. Zero is the claim that an instrument is
    // free to trade; null is the truth that nobody has looked. A screen that collapsed the two
    // would present an unmeasured market as a free one.
    expect(measuredSpread(instrument('US500', null))).toBeNull()
    expect(measuredSpread(undefined)).toBeNull()
    expect(measuredSpread(instrument('FREE', '0'))).toBe('0')
  })
})

describe('uncostedAmong', () => {
  it('names the chosen markets nobody measured, and only those', () => {
    const catalogue = [
      instrument('EURUSD', '8'),
      instrument('GBPUSD', '9'),
      instrument('US500', null),
      instrument('AAPL', null),
    ]

    // AAPL is unmeasured but was not chosen: a warning about a market the reader did not pick
    // would train them to ignore the warning.
    expect(uncostedAmong(['EURUSD', 'US500'], catalogue)).toEqual(['US500'])
  })

  it('treats a symbol the catalogue has never heard of as uncosted', () => {
    expect(uncostedAmong(['NOPE'], [instrument('EURUSD', '8')])).toEqual(['NOPE'])
  })

  it('says nothing while the catalogue is still loading', () => {
    expect(uncostedAmong(['EURUSD'], undefined)).toEqual(['EURUSD'])
  })
})

describe('whyNotLaunchable', () => {
  it('refuses a basket of one, because that is a backtest', () => {
    const form = toggleSymbol(emptyBasketForm(), 'EURUSD')
    expect(whyNotLaunchable(form)).toMatch(/at least 2 markets/)
  })

  it('refuses an empty basket', () => {
    expect(whyNotLaunchable(emptyBasketForm())).toMatch(/at least 2 markets/)
  })

  it('refuses more markets than the API accepts', () => {
    let form = emptyBasketForm()
    for (let i = 0; i <= MAX_SYMBOLS; i += 1) form = toggleSymbol(form, `SYM${String(i)}`)
    expect(whyNotLaunchable(form)).toMatch(/at most 20 markets/)
  })

  it('refuses a window that ends before it starts, and one that is a single instant', () => {
    const two = toggleSymbol(toggleSymbol(emptyBasketForm(), 'A'), 'B')
    expect(whyNotLaunchable({ ...two, dateFrom: '2024-06-01', dateTo: '2024-01-01' })).toMatch(
      /ends before it starts/,
    )
    // Equal dates are refused too: the boundary is `>=`, and `>` would let through a window with
    // no candles in it that fails much later, in the worker.
    expect(whyNotLaunchable({ ...two, dateFrom: '2024-01-01', dateTo: '2024-01-01' })).toMatch(
      /ends before it starts/,
    )
  })

  it('refuses a window with an empty end, on either side', () => {
    // A cleared date input yields '', and '' compares below every real date — so the emptiness
    // has to be caught before the ordering check, or clearing "from" would read as a valid
    // window starting at the beginning of time.
    const two = toggleSymbol(toggleSymbol(emptyBasketForm(), 'A'), 'B')
    expect(whyNotLaunchable({ ...two, dateFrom: '' })).toMatch(/set the backtest window/)
    expect(whyNotLaunchable({ ...two, dateTo: '' })).toMatch(/set the backtest window/)
  })

  it('refuses capital that is zero or negative', () => {
    const two = toggleSymbol(toggleSymbol(emptyBasketForm(), 'A'), 'B')
    expect(whyNotLaunchable({ ...two, capital: '0' })).toMatch(/must be positive/)
    expect(whyNotLaunchable({ ...two, capital: '-1' })).toMatch(/must be positive/)
  })

  it('lets a well-formed basket through', () => {
    const two = toggleSymbol(toggleSymbol(emptyBasketForm(), 'EURUSD'), 'GBPUSD')
    expect(whyNotLaunchable(two)).toBeNull()
  })
})

describe('toBasketRequest', () => {
  it('folds the form into the body, with no cost model in it', () => {
    let form = emptyBasketForm()
    form = toggleSymbol(form, 'EURUSD')
    form = toggleSymbol(form, 'GBPUSD')

    const body = toBasketRequest(form, 's1', 'H4')

    expect(body).toEqual({
      strategy_id: 's1',
      symbols: ['EURUSD', 'GBPUSD'],
      timeframe: 'H4',
      date_from: '2024-01-01T00:00:00Z',
      date_to: '2024-12-31T00:00:00Z',
      initial_capital: '10000',
    })
    // ⚠️ Asserted as an absence, not merely omitted from the expectation above: the server
    // charges each market its own measured spread, and a `cost_model` sent from here would be a
    // single figure applied across instruments whose tick sizes differ by a thousandfold.
    expect(body).not.toHaveProperty('cost_model')
  })

  it('keeps capital a string all the way to the wire', () => {
    const form = { ...emptyBasketForm(), symbols: ['A', 'B'], capital: '10000.50' }
    // Money that goes through a JavaScript number picks up binary dust that then compounds
    // through every trade of every run in the basket.
    expect(toBasketRequest(form, 's1', 'H1').initial_capital).toBe('10000.50')
  })

  it('copies the symbols rather than handing over the array the form holds', () => {
    let form = emptyBasketForm()
    form = toggleSymbol(form, 'EURUSD')
    form = toggleSymbol(form, 'GBPUSD')

    const body = toBasketRequest(form, 's1', 'H1')
    const after = toggleSymbol(form, 'US500')

    // The body already sent must not follow the form the user keeps editing.
    expect(after.symbols).toEqual(['EURUSD', 'GBPUSD', 'US500'])
    expect(body.symbols).toEqual(['EURUSD', 'GBPUSD'])
  })
})

describe('newlyComparable', () => {
  const done = listed({ id: 'a' })
  const queued = listed({ id: 'b', status: 'queued', metrics: null })
  const failed = listed({ id: 'c', status: 'failed', metrics: null })

  it('offers a finished run a seat', () => {
    expect(newlyComparable([done, queued, failed], new Set()).map((r) => r.id)).toEqual(['a'])
  })

  it('does not offer a run marked done that has no metrics yet', () => {
    // `status` and the metrics row are written in that order, so there is a gap where a run is
    // done and has nothing to draw. Seating it would spend a colour slot on an empty line.
    const gap = listed({ id: 'd', status: 'done', metrics: null })
    expect(newlyComparable([gap], new Set())).toEqual([])
  })

  it('never offers the same run twice, which is what lets a reader untick one', () => {
    // ⚠️ The regression this function exists for. Without the `offered` set, "every finished run
    // that is not currently seated" would put a run straight back on the chart the moment it was
    // unticked — the next poll finds it finished and unseated. The checkbox would refuse to stay
    // off and nothing would look broken.
    expect(newlyComparable([done], new Set(['a']))).toEqual([])
  })
})

describe('launchFailure', () => {
  it('shows the API detail verbatim, so every bad symbol reaches the reader', () => {
    const error = new ApiError(422, 'unknown symbols: NOPE, ALSONOPE')
    // The backend goes out of its way to name them all; replacing that with a house message
    // would send the reader back to fixing a typo list one round trip at a time.
    expect(launchFailure(error)).toBe('unknown symbols: NOPE, ALSONOPE')
  })

  it('falls back when the failure carries no readable detail', () => {
    expect(launchFailure(new ApiError(500, { loc: ['body'] }))).toMatch(/check the fields/i)
    expect(launchFailure(new Error('network down'))).toMatch(/check the fields/i)
  })
})

describe('basketLabel', () => {
  it('names a basket by its strategy and how many markets it tried', () => {
    expect(basketLabel('Ponto Contínuo', 3)).toBe('Ponto Contínuo · 3 markets')
  })
})
