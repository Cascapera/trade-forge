import { describe, expect, it } from 'vitest'

import type { CatalogEntry } from '../api/types'

import {
  emptySweepForm,
  runCount,
  toPreviewRequest,
  toSweepRequest,
  whyNotLaunchable,
  type SweepForm,
} from './settings'

function anEntry(id: string, points: number): CatalogEntry {
  return {
    id,
    name: `entry ${id}`,
    description: null,
    strategy_id: `strategy-${id}`,
    strategy_name: 'MME9-20260910-172055',
    strategy_version: 1,
    setup: 'mme9_breakout',
    grid: {},
    points,
    created_at: '2026-09-01T00:00:00Z',
  }
}

/** A form that `whyNotLaunchable` approves, so each test can break exactly one thing. */
function aForm(patch: Partial<SweepForm> = {}): SweepForm {
  return {
    ...emptySweepForm,
    entryIds: ['a'],
    symbols: ['EURUSD'],
    timeframes: ['M15'],
    dateFrom: '2025-01-01',
    dateTo: '2025-06-01',
    ...patch,
  }
}

describe('runCount', () => {
  it('adds the entries and multiplies the charts and the markets', () => {
    // ⚠️ The arithmetic mistake this function exists to make once, in a fixture that **separates**
    // it: three points and one point, over two charts and three markets, is (3 + 1) x 2 x 3 = 24.
    // An implementation that multiplied the entries would give 3 x 1 x 2 x 3 = 18, and one that
    // added the axes would give something else again. Equal counts would not tell them apart.
    const entries = [anEntry('a', 3), anEntry('b', 1)]
    const form = aForm({
      entryIds: ['a', 'b'],
      symbols: ['EURUSD', 'GBPUSD', 'XAUUSD'],
      timeframes: ['M15', 'H1'],
    })

    expect(runCount(form, entries)).toBe(24)
  })

  it('counts an entry that varies nothing as one, not zero', () => {
    // The empty product. An entry with no grid is still one backtest per chart per market.
    expect(runCount(aForm(), [anEntry('a', 1)])).toBe(1)
  })

  it('is null, not zero, when a ticked entry is not on the shelf', () => {
    // ⚠️ The distinction the screen is built on: "nothing to run" and "I could not count" are
    // different facts, and `0` claims the first while meaning the second. Reachable because the
    // shelf is a live query — an entry removed in another tab leaves on the next refetch.
    const form = aForm({ entryIds: ['a', 'gone'] })

    expect(runCount(form, [anEntry('a', 3)])).toBeNull()
  })

  it('is zero only when the form really describes no runs', () => {
    // The other side of the pair. Without this, `toBeNull` above would also pass for an
    // implementation that returned null for everything.
    expect(runCount(aForm({ symbols: [] }), [anEntry('a', 3)])).toBe(0)
  })
})

describe('whyNotLaunchable', () => {
  const entries = [anEntry('a', 2)]

  it('approves a complete form', () => {
    expect(whyNotLaunchable(aForm(), entries)).toBeNull()
  })

  it.each([
    ['entryIds', { entryIds: [] }, /entry from the shelf/],
    ['symbols', { symbols: [] }, /market/],
    ['timeframes', { timeframes: [] }, /chart/],
    ['period', { dateTo: '' }, /period/],
    ['capital', { initialCapital: '0' }, /capital/],
  ])('refuses a form missing its %s', (_what, patch, expected) => {
    expect(whyNotLaunchable(aForm(patch), entries)).toMatch(expected)
  })

  it('refuses a window of zero duration, which the table refuses too', () => {
    // ⚠️ `<=`, not `<`. `sweeps` carries `CHECK (date_to > date_from)`, so a form that let the
    // equal case through would trade this sentence for a 422 arriving after the click. A test
    // only on the inverted case would not separate the two operators.
    const sameDay = aForm({ dateFrom: '2025-01-01', dateTo: '2025-01-01' })
    const inverted = aForm({ dateFrom: '2025-06-01', dateTo: '2025-01-01' })

    expect(whyNotLaunchable(sameDay, entries)).toMatch(/after its start/)
    expect(whyNotLaunchable(inverted, entries)).toMatch(/after its start/)
  })

  it('does not refuse a sweep for its size', () => {
    // ⚠️ His decision (18/09): no cap — "this sweep expands to 3168 backtests, over the 3000" was
    // refused, and hours of queue are his to spend. 2 points x 4 charts x 400 markets = 3200.
    const markets = Array.from({ length: 400 }, (_, i) => `SYM${String(i)}`)
    const form = aForm({ symbols: markets, timeframes: ['M15', 'H1', 'H4', 'D1'] })

    expect(runCount(form, entries)).toBe(3200)
    expect(whyNotLaunchable(form, entries)).toBeNull()
  })

  it('refuses rather than waves through a sweep it cannot count', () => {
    // An entry gone from the shelf is a launch the server answers with a 404 after the click.
    // Treating "no number came back" as "nothing is wrong" is the unanswered-question-as-answer
    // defect.
    const form = aForm({ entryIds: ['a', 'gone'] })

    expect(whyNotLaunchable(form, entries)).toMatch(/no longer on the shelf/)
  })
})

describe('toPreviewRequest', () => {
  it('asks nothing until the window is filled in', () => {
    // ⚠️ Coverage is per (symbol, timeframe) inside a window, so a preview without dates would
    // be asking a question whose answer could not include the one thing it exists to report.
    expect(toPreviewRequest(aForm({ dateFrom: '' }))).toBeNull()
    expect(toPreviewRequest(aForm({ dateTo: '' }))).toBeNull()
  })

  it('asks nothing for a window that runs backwards', () => {
    expect(toPreviewRequest(aForm({ dateFrom: '2025-06-01', dateTo: '2025-01-01' }))).toBeNull()
  })

  it('asks nothing while an axis is empty', () => {
    expect(toPreviewRequest(aForm({ entryIds: [] }))).toBeNull()
    expect(toPreviewRequest(aForm({ symbols: [] }))).toBeNull()
    expect(toPreviewRequest(aForm({ timeframes: [] }))).toBeNull()
  })

  it('sends the window as an instant, not as the text that was typed', () => {
    const asked = toPreviewRequest(aForm())

    expect(asked?.date_from).toBe('2025-01-01T00:00:00.000Z')
    expect(asked?.date_to).toBe('2025-06-01T00:00:00.000Z')
  })
})

describe('toSweepRequest', () => {
  it('sends no cost model rather than a spread of zero when the field is blank', () => {
    // ⚠️ "Charge nothing" and "charge a spread of zero" are the same number and different
    // statements. The engine reads the type, and `{type: 'none'}` is the honest one.
    expect(toSweepRequest(aForm({ spreadTicks: '' })).cost_model).toEqual({ type: 'none' })
    expect(toSweepRequest(aForm({ spreadTicks: '  ' })).cost_model).toEqual({ type: 'none' })
  })

  it('sends the spread it was given, trimmed', () => {
    expect(toSweepRequest(aForm({ spreadTicks: ' 8 ' })).cost_model).toEqual({
      type: 'spread',
      spread_points: '8',
    })
  })

  it('carries capital as text, never as a number', () => {
    // Money through a float is money rounded by the wrong thing. The API takes a string and the
    // form holds one, so nothing here converts.
    const body = toSweepRequest(aForm({ initialCapital: '10000.50' }))

    expect(body.initial_capital).toBe('10000.50')
  })

  it('copies the axes instead of aliasing the form', () => {
    // Aliasing would let a later edit to the form mutate a request already in flight.
    const form = aForm({ symbols: ['EURUSD'] })
    const body = toSweepRequest(form)
    form.symbols.push('GBPUSD')

    expect(body.symbols).toEqual(['EURUSD'])
  })
})
