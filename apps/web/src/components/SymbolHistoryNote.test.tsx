import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'

import { ApiError } from '../api/client'
import type { SymbolHistory } from '../api/types'

const probe = vi.fn()
let answer: { data: SymbolHistory | undefined; error: unknown } = { data: undefined, error: null }

vi.mock('../api/hooks', () => ({
  useSymbolHistory: () => answer,
  useProbeSymbol: () => ({ mutate: probe, isPending: false, isSuccess: false }),
}))

import { SymbolHistoryNote } from './SymbolHistoryNote'

/** EURUSD D1 as this project actually measured it. */
function history(patch: Partial<SymbolHistory> = {}): SymbolHistory {
  return {
    symbol: 'EURUSD',
    timeframe: 'D1',
    oldest: '1971-01-03T21:00:00Z',
    bar_count: 14343,
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

beforeEach(() => {
  answer = { data: undefined, error: null }
  probe.mockClear()
})

it('says nothing until both halves of the question exist', () => {
  // A symbol with no timeframe has no answer, and a note that guessed one would report a span
  // for a series nobody is running.
  const { container } = render(<SymbolHistoryNote symbol="EURUSD" timeframe={undefined} />)

  expect(container).toBeEmptyDOMElement()
})

it('offers to measure a series nobody has asked about', () => {
  /**
   * ⚠️ "Nobody has measured this" and "this symbol has no bars" are opposite invitations, and
   * the API keeps them apart with a 404. Showing an empty span for both would tell somebody
   * their broker has no history because nobody had looked.
   */
  answer = { data: undefined, error: new ApiError(404, 'not probed') }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.getByText(/nobody has measured/)).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: 'measure it' }))
  expect(probe).toHaveBeenCalledWith({ symbol: 'EURUSD', timeframe: 'D1' })
})

it('reports the count and the start together', () => {
  // Neither alone is actionable: "since 2009" says nothing about how much signal that is, and
  // "14,343 bars" says nothing about which market they are.
  answer = { data: history(), error: null }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  // ⚠️ Grouped by the environment's locale, not by en-US: this machine renders 14.343 and the
  // first version of this test pinned 14,343. The assertion is that the count is *shown*, so it
  // formats the expectation the same way the component does rather than choosing a separator.
  expect(screen.getByText(new RegExp(`${(14343).toLocaleString()} bars available`))).toBeInTheDocument()
  expect(screen.getByText(/usable from 2009-01-01/)).toBeInTheDocument()
})

it('names the terminal when the terminal is the limit', () => {
  /** Measured before this machine's setting was raised: 100000 on M1, M5, M15 and H1 alike. */
  answer = {
    data: history({ capped_by_terminal: true, terminal_maxbars: 100000, bar_count: 100000 }),
    error: null,
  }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.getByText(/limited by your terminal, not your broker/)).toBeInTheDocument()
  expect(
    screen.getByText(new RegExp(`Max bars in chart is ${(100000).toLocaleString()}`)),
  ).toBeInTheDocument()
})

it('warns that the count is where the measurement stopped', () => {
  // Seen for real at exactly 10,000,000 — the search ceiling, and the one number somebody would
  // size a window from.
  answer = { data: history({ bar_count: 10000000, bar_count_is_a_ceiling: true }), error: null }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.getByText(/where the measurement stopped/)).toBeInTheDocument()
})

it('says which bars nobody traded, and why that flatters a backtest', () => {
  answer = { data: history({ last_fabricated: 1972 }), error: null }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.getByText(/bars up to 1972 have no range and no ticks/)).toBeInTheDocument()
  expect(screen.getByText(/no stop is ever hit inside one/)).toBeInTheDocument()
})

it('admits what the measurement cannot see, next to the filler it can', () => {
  /**
   * ⚠️ The honest limit, and it is shown *beside* the filler on purpose. EURUSD's fabricated
   * bars stop in 1973 and the euro dates from 1999 — the reconstruction in between carries
   * plausible prices and volumes and is invisible to every property of a bar. A series that has
   * filler is exactly the series likely to have more of it wearing a better disguise.
   */
  answer = { data: history({ last_fabricated: 1972 }), error: null }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.getByText(/invisible to this measurement/)).toBeInTheDocument()
  expect(screen.getByText(/when the instrument was actually listed/)).toBeInTheDocument()
})

it('does not cry about a reconstruction on a series with no filler at all', () => {
  // BTCUSD measured exactly this: every year real, back to 2022. The caveat is load-bearing
  // where filler exists and noise where it does not.
  answer = {
    data: history({ symbol: 'BTCUSD', last_fabricated: null, first_measured_cost: 2022 }),
    error: null,
  }

  render(<SymbolHistoryNote symbol="BTCUSD" timeframe="H1" />)

  expect(screen.queryByText(/invisible to this measurement/)).not.toBeInTheDocument()
})

it('says when the costs were typed rather than observed', () => {
  answer = { data: history({ first_measured_cost: 2009 }), error: null }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.getByText(/before 2009 the broker stamped one spread/)).toBeInTheDocument()
})

it('a clean series carries no warnings at all', () => {
  /** ⚠️ The separating case: without it every assertion above would pass on a component that
   *  rendered all four warnings unconditionally. */
  answer = {
    data: history({
      last_fabricated: null,
      first_measured_cost: null,
      capped_by_terminal: false,
      bar_count_is_a_ceiling: false,
    }),
    error: null,
  }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="D1" />)

  expect(screen.queryByText(/⚠️/)).not.toBeInTheDocument()
})

it('a symbol the terminal has nothing for says so plainly', () => {
  answer = { data: history({ bar_count: 0, oldest: null, usable_from: null }), error: null }

  render(<SymbolHistoryNote symbol="EURUSD" timeframe="W1" />)

  expect(screen.getByText(/has no D1 bars at all/)).toBeInTheDocument()
})
