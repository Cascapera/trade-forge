import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'

import { ApiError } from '../api/client'
import type { BrokerSymbol, Collection, SymbolHistory, SymbolSearch } from '../api/types'
import { asDateInput, suggestedWindow } from '../collect/window'

const createMutation = {
  mutate: vi.fn(),
  reset: vi.fn(),
  isPending: false,
  error: null as unknown,
}
let known = new Map<string, SymbolHistory>()
/** Symbols whose measurement came back empty — what the auto-probe acts on. */
let missing: string[] = []
let searchAnswer: SymbolSearch = { symbols: [], snapshot: null }
let listed: Collection[] = []

vi.mock('../api/hooks', () => ({
  useSymbolHistories: () => ({ known, missing }),
  // The real hook fires one probe per unmeasured pair; the screen only reads the count, so the
  // fake reports what the real one would. `useAutoProbe`'s own behaviour is tested next door.
  useAutoProbe: (args: { missing: readonly string[] }) => ({ queued: args.missing.length }),
  useCreateCollection: () => createMutation,
  useCollections: () => ({ data: listed }),
  useSymbolSearch: () => ({ data: searchAnswer }),
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

function broker(patch: Partial<BrokerSymbol> = {}): BrokerSymbol {
  return {
    symbol: 'EURUSD',
    description: 'Euro vs US Dollar',
    path: 'Forex\\Majors\\EURUSD',
    asset_class_from_path: 'forex',
    digits: 5,
    visible: true,
    catalogued: true,
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

/**
 * Search for a ticker and click it out of the results — how a symbol joins the batch.
 *
 * ⚠️ Queried by **name**, because this screen has two comboboxes: the symbol search and the
 * timeframe `<select>`, which carries the role implicitly. They are already named distinctly,
 * so this is the query being specific rather than the markup being fixed around a test.
 */
function pick(symbol: string): void {
  fireEvent.change(screen.getByRole('combobox', { name: 'Symbols' }), {
    target: { value: symbol },
  })
  fireEvent.mouseDown(screen.getByRole('option', { name: new RegExp(symbol) }))
}

function collectButton(): HTMLElement {
  return screen.getByRole('button', { name: /^Collect / })
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

/** The timeframe rows the screen sent. */
function rowsSent(): Record<string, string>[] {
  const rows = requestSent().rows as unknown
  if (!Array.isArray(rows)) throw new Error(`rows is not a list: ${JSON.stringify(rows)}`)
  return rows as Record<string, string>[]
}

/** The batch's items, as objects. */
function itemsSent(): Record<string, string>[] {
  const items = requestSent().items as unknown
  if (!Array.isArray(items)) throw new Error(`items is not a list: ${JSON.stringify(items)}`)
  return items as Record<string, string>[]
}

beforeEach(() => {
  known = new Map()
  missing = []
  searchAnswer = { symbols: [], snapshot: null }
  listed = []
  createMutation.error = null
  createMutation.isPending = false
  createMutation.mutate.mockClear()
  createMutation.reset.mockClear()
})

it('will not collect until a symbol is chosen, and says so', () => {
  // The window is pre-filled from the start, so without this the button would be live with no
  // symbols behind it — and the request it sent would be refused by the API for an empty list.
  render(<CollectSymbol />)

  expect(collectButton()).toBeDisabled()
  expect(screen.getByText(/choose at least one symbol/i)).toBeInTheDocument()
})

it('sends one item per chosen symbol, in the order they were picked', () => {
  searchAnswer = { symbols: [broker(), broker({ symbol: 'GBPUSD' })], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)

  pick('EURUSD')
  pick('GBPUSD')
  fireEvent.click(collectButton())

  expect(itemsSent().map((item) => item.symbol)).toEqual(['EURUSD', 'GBPUSD'])
})

it('sends the window the form is showing, with the end day included', () => {
  /**
   * ⚠️ `date_to` is the **end** of the chosen day. The collection window is inclusive on both
   * ends, so sending midnight would silently drop every bar of the final day — on exactly the
   * day somebody picked as the end of their backtest.
   */
  searchAnswer = { symbols: [broker()], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)
  pick('EURUSD')

  fireEvent.click(collectButton())

  const sent = rowsSent()[0]
  expect(sent?.date_from).toMatch(/T00:00:00Z$/)
  expect(sent?.date_to).toMatch(/T23:59:59Z$/)
})

it('opens on the floor the probe found rather than on the whole budget', () => {
  /**
   * ⚠️ The suggestion that runs against intuition. An H1 bar budget reaches back seventeen
   * years — past 2009, where this broker's spread stops being a number somebody typed. Offering
   * those years by default would hand somebody a backtest that looks better for being less
   * validated.
   */
  searchAnswer = { symbols: [broker()], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)
  pick('EURUSD')

  expect(screen.getByLabelText('H1 from')).toHaveValue('2009-01-01')
  expect(screen.getByText(/stops being filler and typed costs/)).toBeInTheDocument()
})

it('the latest floor among the chosen symbols is the one that binds', () => {
  /**
   * ⚠️ The multi-symbol case, and the separating one. EURUSD is usable from 2009 and BTCUSD only
   * from 2022; one window covers both, so opening on 2009 would buy BTCUSD thirteen empty years
   * and — for anything that does answer that early — years of spread the broker typed.
   *
   * The message names the symbol doing the binding, because "2022" with no explanation reads
   * like a bug to somebody who picked EURUSD expecting 2009.
   */
  searchAnswer = { symbols: [broker(), broker({ symbol: 'BTCUSD' })], snapshot: null }
  known = new Map([
    ['EURUSD', history()],
    ['BTCUSD', history({ symbol: 'BTCUSD', usable_from: '2022-05-10T00:00:00Z' })],
  ])
  render(<CollectSymbol />)
  pick('EURUSD')
  pick('BTCUSD')

  expect(screen.getByLabelText('H1 from')).toHaveValue('2022-05-10')
  expect(screen.getByText(/BTCUSD.s measurement/)).toBeInTheDocument()
})

it('says how many collections the batch is, and how many years that means', () => {
  // Two numbers for two questions: the count is what the ceiling is about, the slices are
  // what the wait is about. Forty collections of H1 and forty of M1 are the same count.
  searchAnswer = { symbols: [broker()], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)
  pick('EURUSD')

  expect(screen.getByText(/collection/)).toBeInTheDocument()
  expect(screen.getByText(/of history to fetch/)).toBeInTheDocument()
})

it('says how many calendar years the batch will fetch', () => {
  /**
   * ⚠️ The number that makes the wait legible before it starts. Two symbols over a window that
   * spans 2009..2026 is eighteen years each — thirty-six downloads, one after another, because
   * the agent runs one job at a time.
   */
  searchAnswer = { symbols: [broker(), broker({ symbol: 'GBPUSD' })], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)
  pick('EURUSD')
  pick('GBPUSD')

  fireEvent.change(screen.getByLabelText('H1 from'), { target: { value: '2024-01-01' } })
  fireEvent.change(screen.getByLabelText('H1 to'), { target: { value: '2025-12-31' } })

  // Two symbols × two calendar years.
  expect(screen.getByText('4')).toBeInTheDocument()
})

it('stops suggesting once the dates are edited by hand', () => {
  /**
   * ⚠️ The suggestion is a default, never a limit. Re-deriving it on every render would snap the
   * field back under the cursor the moment a measurement arrived — and the operator widening a
   * window is precisely the person a measurement is arriving for.
   */
  searchAnswer = { symbols: [broker()], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)
  pick('EURUSD')

  fireEvent.change(screen.getByLabelText('H1 from'), { target: { value: '1999-01-01' } })

  expect(screen.getByLabelText('H1 from')).toHaveValue('1999-01-01')
  fireEvent.click(collectButton())
  expect(rowsSent()[0]?.date_from).toBe('1999-01-01T00:00:00Z')
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
  searchAnswer = { symbols: [broker()], snapshot: null }
  known = new Map([['EURUSD', history()]])
  render(<CollectSymbol />)
  pick('EURUSD')

  fireEvent.change(screen.getByLabelText('Timeframe of row r0'), { target: { value: 'M1' } })

  const expected = asDateInput(suggestedWindow('M1', history(), new Date()).from)
  expect(screen.getByLabelText('M1 from').getAttribute('value')).toBe(expected)
})

it('asks the class before sending, not after the API refuses', () => {
  /**
   * ⚠️ 24 of this broker's 84 symbols. `instruments.asset_class` is NOT NULL with five legal
   * values and `CFDs` names none of them. With one symbol the API's 409 could be turned into a
   * field; with twenty it would name a list, so the screen asks up front — it already knows
   * which ones, because the search says so.
   */
  searchAnswer = {
    symbols: [broker({ symbol: 'XAUUSD', path: 'CFDs\\Metals\\XAUUSD', asset_class_from_path: null })],
    snapshot: null,
  }
  render(<CollectSymbol />)
  pick('XAUUSD')

  expect(collectButton()).toBeDisabled()
  expect(screen.getByText(/Say what XAUUSD is/i)).toBeInTheDocument()
  expect(screen.getByLabelText('Asset class for XAUUSD')).toBeInTheDocument()
})

it('sends the class once it is answered, against its own symbol', () => {
  searchAnswer = {
    symbols: [broker({ symbol: 'XAUUSD', path: 'CFDs\\Metals\\XAUUSD', asset_class_from_path: null })],
    snapshot: null,
  }
  render(<CollectSymbol />)
  pick('XAUUSD')

  fireEvent.change(screen.getByLabelText('Asset class for XAUUSD'), { target: { value: 'future' } })
  fireEvent.click(collectButton())

  expect(itemsSent()).toEqual([{ symbol: 'XAUUSD', asset_class: 'future' }])
})

it('one symbol needing an answer does not make the others carry a class', () => {
  /**
   * ⚠️ The failure a batch-wide class field would have caused. XAUUSD is a future and EURUSD is
   * filed as forex by its own path; sending `future` for both would catalogue a currency pair as
   * a contract, and nothing would raise.
   */
  searchAnswer = {
    symbols: [
      broker(),
      broker({ symbol: 'XAUUSD', path: 'CFDs\\Metals\\XAUUSD', asset_class_from_path: null }),
    ],
    snapshot: null,
  }
  render(<CollectSymbol />)
  pick('EURUSD')
  pick('XAUUSD')

  fireEvent.change(screen.getByLabelText('Asset class for XAUUSD'), { target: { value: 'future' } })
  fireEvent.click(collectButton())

  expect(itemsSent()).toEqual([
    { symbol: 'EURUSD' },
    { symbol: 'XAUUSD', asset_class: 'future' },
  ])
})

it('does not send an asset class the path already decided', () => {
  /**
   * ⚠️ Absence means "the path already decided", and sending a value anyway would overwrite a
   * derived class with a form default — quietly filing every currency pair as whatever the
   * select happened to open on.
   */
  searchAnswer = { symbols: [broker()], snapshot: null }
  render(<CollectSymbol />)
  pick('EURUSD')

  fireEvent.click(collectButton())

  expect('asset_class' in (itemsSent()[0] ?? {})).toBe(false)
})

it('removing a symbol takes its answered class with it', () => {
  /**
   * ⚠️ Otherwise the answer outlives the question: pick XAUUSD, say `future`, remove it, pick
   * something else — and a class chosen for a metal would still be sitting in state.
   */
  searchAnswer = {
    symbols: [
      broker({ symbol: 'XAUUSD', path: 'CFDs\\Metals\\XAUUSD', asset_class_from_path: null }),
    ],
    snapshot: null,
  }
  render(<CollectSymbol />)
  pick('XAUUSD')
  fireEvent.change(screen.getByLabelText('Asset class for XAUUSD'), { target: { value: 'future' } })

  fireEvent.click(screen.getByRole('button', { name: 'Remove XAUUSD' }))
  pick('XAUUSD')

  expect(collectButton()).toBeDisabled()
  expect(screen.getByText(/Say what XAUUSD is/i)).toBeInTheDocument()
})

it('shows the reason a request was refused, not the status code', () => {
  searchAnswer = { symbols: [broker()], snapshot: null }
  createMutation.error = new ApiError(422, 'date_to precedes date_from')
  render(<CollectSymbol />)
  pick('EURUSD')

  expect(screen.getByText(/date_to precedes date_from/)).toBeInTheDocument()
})

it('a 422 whose detail is a list of field errors still says something', () => {
  // ⚠️ Stringifying a list of field errors yields `[object Object]`, which is worse than the
  // status code. The guard is on the *shape* of `detail`, not on its presence.
  searchAnswer = { symbols: [broker()], snapshot: null }
  createMutation.error = new ApiError(422, [{ loc: ['body', 'items'], msg: 'too short' }])
  render(<CollectSymbol />)
  pick('EURUSD')

  expect(screen.getByText(/refused \(422\)/)).toBeInTheDocument()
})

it('lists the collections already requested', () => {
  listed = [collection(), collection({ id: 'c2', symbol: 'GBPUSD' })]
  render(<CollectSymbol />)

  expect(screen.getByText('EURUSD')).toBeInTheDocument()
  expect(screen.getByText('GBPUSD')).toBeInTheDocument()
})

describe('what is still being measured, and what will come back short', () => {
  it('says how many symbols are being measured ahead of the collection', () => {
    /**
     * ⚠️ Measuring shares the collection's single-job queue and a cold H4 took 207 seconds on
     * this broker, so these run *before* the first candle. Saying how many turns a silent
     * minute into a queue somebody can reason about.
     */
    searchAnswer = { symbols: [broker(), broker({ symbol: 'GBPUSD' })], snapshot: null }
    missing = ['EURUSD', 'GBPUSD']
    render(<CollectSymbol />)
    pick('EURUSD')

    expect(screen.getByText(/2 symbols/)).toBeInTheDocument()
    expect(screen.getByText(/measured once and never again/i)).toBeInTheDocument()
  })

  it('says nothing about measuring when everything is already measured', () => {
    /**
     * ⚠️ The silence has to be earned. A box that always said "measuring 0 symbols" would train
     * the reader to skip it, and it is the one thing on the screen explaining a wait.
     */
    searchAnswer = { symbols: [broker()], snapshot: null }
    known = new Map([['EURUSD', history()]])
    missing = []
    render(<CollectSymbol />)
    pick('EURUSD')

    expect(screen.queryByText(/nobody has measured yet/i)).not.toBeInTheDocument()
  })

  it('names the symbols that start after the window does', () => {
    /**
     * ⚠️ Coming back short is an ordinary answer, not a failure — `run_collection` treats an
     * empty year as ordinary. Which is exactly why it must be said: the request succeeds, the
     * row reports fewer candles than the dates imply, and without this the only explanation
     * available to the reader is "a bug".
     */
    searchAnswer = { symbols: [broker(), broker({ symbol: 'BTCUSD' })], snapshot: null }
    known = new Map([
      ['EURUSD', history()],
      ['BTCUSD', history({ symbol: 'BTCUSD', usable_from: '2022-05-10T00:00:00Z' })],
    ])
    render(<CollectSymbol />)
    pick('EURUSD')
    pick('BTCUSD')
    fireEvent.change(screen.getByLabelText('H1 from'), { target: { value: '2015-01-01' } })

    expect(screen.getByText(/come back shorter/i)).toBeInTheDocument()
    expect(screen.getByText(/usable from 2022-05-10/)).toBeInTheDocument()
  })

  it('says nothing when every chosen symbol covers the window', () => {
    searchAnswer = { symbols: [broker()], snapshot: null }
    known = new Map([['EURUSD', history()]])
    render(<CollectSymbol />)
    pick('EURUSD')
    fireEvent.change(screen.getByLabelText('H1 from'), { target: { value: '2015-01-01' } })

    expect(screen.queryByText(/come back shorter/i)).not.toBeInTheDocument()
  })

  it('the suggested window does not flag itself as short', () => {
    /**
     * ⚠️ The boundary that matters most, because it is the default. The suggestion opens exactly
     * on the binding floor, so a `>=` comparison would make the screen warn about its own
     * pre-filled window every single time — and a warning that is always on is a warning nobody
     * reads.
     */
    searchAnswer = { symbols: [broker({ symbol: 'BTCUSD' })], snapshot: null }
    known = new Map([['BTCUSD', history({ symbol: 'BTCUSD', usable_from: '2022-05-10T00:00:00Z' })]])
    render(<CollectSymbol />)
    pick('BTCUSD')

    expect(screen.getByLabelText('H1 from')).toHaveValue('2022-05-10')
    expect(screen.queryByText(/come back shorter/i)).not.toBeInTheDocument()
  })
})


describe('timeframe rows', () => {
  it('opens with a single H1 row', () => {
    render(<CollectSymbol />)

    expect(screen.getByLabelText('Timeframe of row r0')).toHaveValue('H1')
  })

  it('adding a timeframe opens another row on the next free one', () => {
    /**
     * \u26a0\ufe0f The next **free** timeframe, never a duplicate. The API refuses a repeated
     * timeframe because two collections of the same series overwrite each other's year
     * partitions — and the symptom is a *missing* year, not a duplicate one.
     */
    render(<CollectSymbol />)

    fireEvent.click(screen.getByRole('button', { name: /add timeframe/i }))

    const chosen = screen
      .getAllByLabelText(/^Timeframe of row/)
      .map((node) => (node as HTMLSelectElement).value)
    expect(chosen).toHaveLength(2)
    expect(new Set(chosen).size).toBe(2)
  })

  it('a new row opens on its own budget rather than inheriting the last row dates', () => {
    // A year of M1 and seventeen of H1 are the same bars. A row inheriting the previous row's
    // dates would ask for seventeen years of M1, which the terminal will not serve.
    render(<CollectSymbol />)
    fireEvent.click(screen.getByRole('button', { name: /add timeframe/i }))

    const rows = screen.getAllByLabelText(/^Timeframe of row/)
    const second = (rows[1] as HTMLSelectElement | undefined)?.value ?? ''
    expect(screen.getByLabelText(`${second} from`)).not.toHaveValue('')
  })

  it('sends one row per timeframe, each with its own window', () => {
    searchAnswer = { symbols: [broker()], snapshot: null }
    render(<CollectSymbol />)
    pick('EURUSD')
    fireEvent.click(screen.getByRole('button', { name: /add timeframe/i }))

    fireEvent.click(collectButton())

    const sent = rowsSent()
    expect(sent).toHaveLength(2)
    expect(new Set(sent.map((line) => line.timeframe)).size).toBe(2)
  })

  it('each remove button names its own timeframe', () => {
    // Several rows on screen with a button each, all called "Remove", would be several controls
    // nobody can tell apart — a defect, not test friction.
    render(<CollectSymbol />)
    fireEvent.click(screen.getByRole('button', { name: /add timeframe/i }))

    const names = screen
      .getAllByRole('button', { name: /^Remove .* row$/ })
      .map((node) => node.getAttribute('aria-label'))

    expect(new Set(names).size).toBe(2)
  })

  it('removing a row takes it off the batch', () => {
    render(<CollectSymbol />)
    fireEvent.click(screen.getByRole('button', { name: /add timeframe/i }))
    const before = screen.getAllByLabelText(/^Timeframe of row/).length

    const removals = screen.getAllByRole('button', { name: /^Remove .* row$/ })
    const second = removals[1]
    if (second === undefined) throw new Error('expected two remove buttons')
    fireEvent.click(second)

    expect(screen.getAllByLabelText(/^Timeframe of row/)).toHaveLength(before - 1)
  })

  it('the last row cannot be removed, because a batch with no timeframe is nothing', () => {
    render(<CollectSymbol />)

    expect(screen.getByRole('button', { name: /^Remove H1 row$/ })).toBeDisabled()
  })

  it('the batch counts one collection per symbol per row', () => {
    searchAnswer = { symbols: [broker(), broker({ symbol: 'GBPUSD' })], snapshot: null }
    render(<CollectSymbol />)
    pick('EURUSD')
    pick('GBPUSD')
    fireEvent.click(screen.getByRole('button', { name: /add timeframe/i }))

    // Two symbols across two timeframes.
    expect(screen.getByText('4')).toBeInTheDocument()
  })
})
