import { asDateInput, asInstant, BAR_BUDGET, suggestedWindow } from './window'
import type { SymbolHistory } from '../api/types'

const NOW = new Date('2026-08-20T00:00:00Z')

function history(patch: Partial<SymbolHistory> = {}): SymbolHistory {
  return {
    symbol: 'EURUSD',
    timeframe: 'H1',
    oldest: '1971-01-03T21:00:00Z',
    bar_count: 178642,
    terminal_maxbars: 100000000,
    bar_count_is_a_ceiling: false,
    last_fabricated: 1972,
    first_measured_cost: 2009,
    probed_at: '2026-08-20T01:27:37Z',
    capped_by_terminal: false,
    usable_from: '2009-01-01T00:00:00Z',
    ...patch,
  }
}

describe('the window the form opens on', () => {
  it('spends the bar budget when nothing has been measured', () => {
    // ⚠️ The two numbers Guilherme picked by hand — a year of M1, five years of M5 — are the
    // same number of bars. That is the criterion, and this is it holding.
    const oneYearOfM1 = suggestedWindow('M1', undefined, NOW)
    const fiveYearsOfM5 = suggestedWindow('M5', undefined, NOW)

    expect(Math.round(years(oneYearOfM1.from, NOW))).toBe(1)
    expect(Math.round(years(fiveYearsOfM5.from, NOW))).toBe(5)
    expect(oneYearOfM1.bars).toBeCloseTo(BAR_BUDGET, -3)
    expect(fiveYearsOfM5.bars).toBeCloseTo(BAR_BUDGET, -3)
  })

  it('stops at the probe floor when the budget would reach past it', () => {
    /**
     * ⚠️ The whole point, and it runs the opposite way to intuition. An H1 budget is seventeen
     * years, which reaches back to 2009 — and the probe says EURUSD's spread was a number
     * somebody typed until then. Spending the rest of the budget would buy bars whose costs are
     * fictional, which makes a backtest look better and be less validated.
     */
    const found = suggestedWindow('H1', history(), NOW)

    expect(found.bound).toBe('probe')
    expect(asDateInput(found.from)).toBe('2009-01-01')
  })

  it('keeps the budget when the probe floor is older than it', () => {
    // A symbol with clean history back to 1990 does not get a 36-year M5 window. The budget is
    // what stops that, and this is the case where the probe has nothing to add.
    const found = suggestedWindow('M5', history({ usable_from: '1990-01-01T00:00:00Z' }), NOW)

    expect(found.bound).toBe('budget')
    expect(Math.round(years(found.from, NOW))).toBe(5)
  })

  it('reports the bar count, not just the dates', () => {
    // ⚠️ "5 years" says nothing about how much signal that is. The count is what a person sizes
    // a run from, and it is why the budget is stated in bars in the first place.
    const found = suggestedWindow('D1', history({ usable_from: '2024-08-20T00:00:00Z' }), NOW)

    expect(found.bars).toBeCloseTo(259 * 2, -1)
  })

  it('a symbol whose probe found no floor still gets a window', () => {
    /**
     * `usable_from` is null when the probe has nothing to stand on — a symbol the terminal holds
     * no bars for. The budget is then the only thing there is, and returning no window at all
     * would leave the form blank on exactly the symbol somebody is trying to collect.
     */
    const found = suggestedWindow('H1', history({ usable_from: null }), NOW)

    expect(found.bound).toBe('budget')
    expect(found.from.getTime()).toBeLessThan(NOW.getTime())
  })

  it('the quarter day is in the year, and it moves the answer by a fortnight', () => {
    /**
     * ⚠️ The assertion that separates 365.25 from 365, and it needs a *long* window to do it.
     * An H1 budget is sixty years, so the quarter day compounds into fifteen — 18 September
     * against 3 October. A one-year M1 window cannot tell them apart at all, which is why this
     * one is written against H1 with the exact date rather than a rounded span.
     */
    const found = suggestedWindow('H1', undefined, NOW)

    expect(asDateInput(found.from)).toBe('1966-09-18')
  })

  it('an unknown timeframe falls back rather than producing a NaN date', () => {
    // ⚠️ `BARS_PER_YEAR[tf]` on a typo yields `undefined`, and every arithmetic below it becomes
    // NaN — which reaches the form as an empty date input and looks like nothing happened.
    const found = suggestedWindow('M7', undefined, NOW)

    expect(Number.isNaN(found.from.getTime())).toBe(false)
  })

  it('each timeframe gets its own span, not one span reused', () => {
    /**
     * ⚠️ The assertion that actually separates. "Every timeframe yields the budget in bars" does
     * **not**: the fallback rate is used on both sides of the arithmetic, so a missing entry
     * still produces exactly 368,500 bars — over a window wrong by two orders of magnitude.
     *
     * Completeness of the map is proved by `tsc` instead (`Record<Timeframe, number>` against
     * the union generated from the DSL). What is left to prove at runtime is that the rates are
     * *distinct*, which is what makes them rates rather than one number copied eight times.
     */
    const spans = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'].map((timeframe) =>
      suggestedWindow(timeframe, undefined, NOW).from.getTime(),
    )

    expect(new Set(spans).size).toBe(spans.length)
    expect(spans).toEqual([...spans].sort((a, b) => b - a))
  })

})

describe('crossing the wire', () => {
  it('sends midnight UTC rather than midnight somewhere', () => {
    // ⚠️ The API refuses a naive instant precisely so this choice is made where it can be seen.
    // `new Date(2020, 0, 1)` is local midnight and differs from this by hours — which is a
    // whole bar on H4 and a whole day of them on M1.
    expect(asInstant('2020-01-01')).toBe('2020-01-01T00:00:00Z')
  })

  it('the end of a day is the end of it, not the start', () => {
    // The window is inclusive on both ends, so a `date_to` of midnight would drop the final
    // day's bars — every one of them.
    expect(asInstant('2020-01-01', true)).toBe('2020-01-01T23:59:59Z')
  })

  it('a date input round-trips', () => {
    expect(asDateInput(new Date('2009-01-01T00:00:00Z'))).toBe('2009-01-01')
  })
})

function years(from: Date, to: Date): number {
  return (to.getTime() - from.getTime()) / (365.25 * 24 * 60 * 60 * 1000)
}
