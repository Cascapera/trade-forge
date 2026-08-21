import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'

import { ApiError } from '../api/client'
import type { Collection, SymbolHistory } from '../api/types'
import { asDateInput, suggestedWindow } from '../collect/window'

const createMutation = {
  mutate: vi.fn(),
  reset: vi.fn(),
  isPending: false,
  error: null as unknown,
}
let historyAnswer: { data: SymbolHistory | undefined; error: unknown } = {
  data: undefined,
  error: null,
}
let listed: Collection[] = []

vi.mock('../api/hooks', () => ({
  useSymbolHistory: () => historyAnswer,
  useProbeSymbol: () => ({ mutate: vi.fn(), isPending: false, isSuccess: false }),
  useCreateCollection: () => createMutation,
  useCollections: () => ({ data: listed }),
  useSymbolSearch: () => ({ data: { symbols: [], snapshot: null } }),
  useSyncSymbols: () => ({ mutate: vi.fn(), isPending: false }),
}))

import { CollectSymbol } from './CollectSymbol'

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

function collection(patch: Partial<Collection> = {}): Collection {
  return {
    id: 'c1',
    symbol: 'EURUSD',
    timeframe: 'H1',
    date_from: '2009-01-01T00:00:00Z',
    date_to: '2026-08-20T23:59:59Z',
    asset_class: null,
    status: 'queued',
    years_done: 0,
    years_total: 18,
    candles: null,
    gaps: null,
    error: null,
    requested_at: '2026-08-20T09:00:00Z',
    started_at: null,
    finished_at: null,
    ...patch,
  }
}

function typeSymbol(value: string): void {
  fireEvent.change(screen.getByLabelText(/symbol/i), { target: { value } })
}

/**
 * The body the screen handed to `useCreateCollection`.
 *
 * ⚠️ Indexing `mock.calls[0]` directly does not compile under `noUncheckedIndexedAccess`, and
 * the honest reason is that the screen might not have called `mutate` at all — a real failure
 * this project has already shipped once. Reading it here turns that into one named error
 * instead of `Cannot read properties of undefined` four tests deep.
 */
function requestSent(): Record<string, string> {
  const [first] = createMutation.mutate.mock.calls
  if (first === undefined) throw new Error('the screen never called mutate')
  return first[0] as Record<string, string>
}

beforeEach(() => {
  historyAnswer = { data: undefined, error: null }
  listed = []
  createMutation.error = null
  createMutation.isPending = false
  createMutation.mutate.mockClear()
  createMutation.reset.mockClear()
})

it('will not collect until a symbol is chosen', () => {
  // The window is pre-filled from the start, so without this the button would be live with no
  // symbol behind it — and the request it sent would be refused by the API for an empty string.
  render(<CollectSymbol />)

  expect(screen.getByRole('button', { name: 'Collect' })).toBeDisabled()
})

it('sends the window the form is showing, with the end day included', () => {
  /**
   * ⚠️ `date_to` is the **end** of the chosen day. The collection window is inclusive on both
   * ends, so sending midnight would silently drop every bar of the final day — on exactly the
   * day somebody picked as the end of their backtest.
   */
  historyAnswer = { data: history(), error: null }
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  fireEvent.click(screen.getByRole('button', { name: 'Collect' }))

  const sent = requestSent()
  expect(sent.symbol).toBe('EURUSD')
  expect(sent.date_from).toMatch(/T00:00:00Z$/)
  expect(sent.date_to).toMatch(/T23:59:59Z$/)
})

it('opens on the floor the probe found rather than on the whole budget', () => {
  /**
   * ⚠️ The suggestion that runs against intuition. An H1 bar budget reaches back seventeen
   * years — past 2009, where this broker's spread stops being a number somebody typed. Offering
   * those years by default would hand somebody a backtest that looks better for being less
   * validated.
   */
  historyAnswer = { data: history(), error: null }
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  expect(screen.getByLabelText('From')).toHaveValue('2009-01-01')
  expect(screen.getByText(/stops being filler and typed costs/)).toBeInTheDocument()
})

it('says how many bars the suggested window is, not just its dates', () => {
  // "17 years" says nothing about how much signal that is; the count is what a run is sized by.
  historyAnswer = { data: history(), error: null }
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  expect(screen.getByText(/bars,/)).toBeInTheDocument()
})

it('stops suggesting once the dates are edited by hand', () => {
  /**
   * ⚠️ The suggestion is a default, never a limit. Re-deriving it on every render would snap the
   * field back under the cursor the moment a probe result arrived — and the operator widening a
   * window is precisely the person a probe result is arriving for.
   */
  historyAnswer = { data: history(), error: null }
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  fireEvent.change(screen.getByLabelText('From'), { target: { value: '1999-01-01' } })

  expect(screen.getByLabelText('From')).toHaveValue('1999-01-01')
  fireEvent.click(screen.getByRole('button', { name: 'Collect' }))
  const sent = requestSent()
  expect(sent.date_from).toBe('1999-01-01T00:00:00Z')
})

it('changing the timeframe re-suggests, because the budget is per timeframe', () => {
  /**
   * A year on M1 and seventeen on H1 are the same number of bars, so a window kept across the
   * change would be wrong by two orders of magnitude while looking deliberate.
   *
   * ⚠️ Asserted against the **exact** date the rule produces, not merely "it changed". The first
   * version of this test said `not.toBe(before)` and passed against the mutant that marks the
   * form as hand-edited on a timeframe change — because that reads the untouched `window` state,
   * which is the empty string, and an empty field is certainly not the old one. A scenario that
   * only rules out equality rules out almost nothing.
   */
  historyAnswer = { data: history(), error: null }
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  fireEvent.change(screen.getByLabelText('Timeframe'), { target: { value: 'M1' } })

  const expected = asDateInput(suggestedWindow('M1', history(), new Date()).from)
  expect(screen.getByLabelText('From').getAttribute('value')).toBe(expected)
})

it('turns the API refusal into a field instead of a wall', () => {
  /**
   * ⚠️ 24 of this broker's 84 symbols. `instruments.asset_class` is NOT NULL with five legal
   * values and `CFDs` names none of them, so the API refuses — and the refusal is only useful
   * if the person looking at the form can answer it.
   */
  createMutation.error = new ApiError(409, 'cannot tell what kind of instrument XAUUSD is')
  render(<CollectSymbol />)
  typeSymbol('XAUUSD')

  expect(screen.getByText(/cannot tell what kind of instrument/)).toBeInTheDocument()
  expect(screen.getByLabelText(/asset class/i)).toBeInTheDocument()
})

it('sends the class once it is answered', () => {
  createMutation.error = new ApiError(409, 'cannot tell what kind of instrument XAUUSD is')
  render(<CollectSymbol />)
  typeSymbol('XAUUSD')

  fireEvent.change(screen.getByLabelText(/asset class/i), { target: { value: 'future' } })
  fireEvent.click(screen.getByRole('button', { name: 'Collect' }))

  const sent = requestSent()
  expect(sent.asset_class).toBe('future')
})

it('does not send an asset class the API never asked for', () => {
  /**
   * ⚠️ The separating case. Absence means "the path already decided", and sending a value
   * anyway would overwrite a derived class with a form default — quietly filing every currency
   * pair as whatever the select happened to open on.
   */
  historyAnswer = { data: history(), error: null }
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  fireEvent.click(screen.getByRole('button', { name: 'Collect' }))

  const sent = requestSent()
  expect('asset_class' in sent).toBe(false)
})

it('shows the reason a request was refused, not the status code', () => {
  // ⚠️ `ApiError.message` is only "API error 422". The sentence naming the bad field is in
  // `detail`, and this project has already shipped a warning box that said nothing else.
  createMutation.error = new ApiError(422, 'date_to is before date_from')
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  expect(screen.getByText(/date_to is before date_from/)).toBeInTheDocument()
  expect(screen.queryByLabelText(/asset class/i)).not.toBeInTheDocument()
})

it('a 422 whose detail is a list of field errors still says something', () => {
  // FastAPI sends an array for a validation error. Stringifying it yields `[object Object]`.
  createMutation.error = new ApiError(422, [{ loc: ['body', 'symbol'], msg: 'bad' }])
  render(<CollectSymbol />)
  typeSymbol('EURUSD')

  expect(screen.getByText(/refused \(422\)/)).toBeInTheDocument()
})

describe('the list of collections', () => {
  it('reports progress in years while one runs', () => {
    listed = [collection({ status: 'running', years_done: 3, years_total: 18 })]
    render(<CollectSymbol />)

    expect(screen.getByText('3 of 18 years')).toBeInTheDocument()
  })

  it('a queued request shows no result rather than zero bars', () => {
    /** ⚠️ `null` and `0` are different claims: nothing collected *yet* against the broker
     * having nothing. Rendering both as `0 bars` would report a finished answer for a request
     * that has not started. */
    listed = [collection({ status: 'queued' })]
    render(<CollectSymbol />)

    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('a finished request shows the bars and the gaps', () => {
    listed = [collection({ status: 'done', candles: 26366, gaps: 234, years_done: 18 })]
    render(<CollectSymbol />)

    // ⚠️ Grouped by the environment's locale, not by en-US: this machine renders 26.366 and
    // pinning a comma would fail here while passing on CI. The assertion is that the count is
    // *shown*, so it formats the expectation the same way the component does.
    const shown = `${(26366).toLocaleString()} bars · ${(234).toLocaleString()} gaps`
    expect(screen.getByText(shown)).toBeInTheDocument()
  })

  it('a failed request shows its reason and not a stale count', () => {
    /** ⚠️ Reason first. A row that failed can still carry counts from an earlier attempt, and
     * reading a failure's count as a result is how somebody concludes they have data they do
     * not have. */
    listed = [
      collection({ status: 'failed', error: 'the broker returned no H1 bars', candles: 900 }),
    ]
    render(<CollectSymbol />)

    expect(screen.getByText('the broker returned no H1 bars')).toBeInTheDocument()
    expect(screen.queryByText(/900 bars/)).not.toBeInTheDocument()
  })

  it('says nothing at all when nothing has been collected', () => {
    render(<CollectSymbol />)

    expect(screen.queryByText(/Recent collections/)).not.toBeInTheDocument()
  })
})
