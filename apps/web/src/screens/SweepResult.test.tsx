import { fireEvent, screen, within } from '@testing-library/react'

import { apiUrl } from '../api/client'
import type {
  BacktestListItem,
  Metrics,
  StudyAggregate,
  SweepEntryOut,
  SweepOut,
  SweepRunOut,
} from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useSweep: vi.fn(),
  useEquityCurves: vi.fn(),
}))

// ⚠️ A prefix the real client never produces. With the real module a test run spells `/api` both
// ways, so a link hard-coded to `/api/sweeps/…` and one built by `apiUrl` would be indistinguishable
// here — and the hard-coded one points at the wrong host the day the API moves.
vi.mock('../api/client', () => ({
  apiUrl: (path: string) => `https://api.test${path}`,
}))

// The chart draws to a canvas jsdom lacks. Stubbed to report what it was *asked* to draw, which
// is what this screen decides — its own test covers the drawing.
vi.mock('../components/ComparisonChart', () => ({
  ComparisonChart: ({ series }: { series: { id: string }[] }) => (
    <div data-testid="chart">{series.map((one) => one.id).join(' ')}</div>
  ),
}))

import { useEquityCurves, useSweep } from '../api/hooks'

import { SweepResult } from './SweepResult'

const mockedSweep = vi.mocked(useSweep)
const mockedCurves = vi.mocked(useEquityCurves)

function metrics(netProfit: string): Metrics {
  return {
    net_profit: netProfit,
    gross_profit: '0',
    gross_loss: '0',
    total_trades: 3,
    long_trades: 3,
    short_trades: 0,
    win_rate: '0.5',
    payoff: null,
    profit_factor: null,
    expectancy: null,
    max_drawdown_abs: '0',
    max_drawdown_pct: '0',
    max_dd_duration_days: 0,
    sharpe: null,
    sortino: null,
    cagr: null,
    avg_trade_duration: null,
  }
}

/** A run of `entry`, named the way the server names a sweep point: `{entry} [{label}]`. */
function row(
  id: string,
  entry: { id: string; name: string },
  label: string,
  netProfit: string | null,
): SweepRunOut {
  const run: BacktestListItem = {
    id,
    strategy_id: id,
    strategy_name: `${entry.name} [${label}]`,
    strategy_version: 1,
    symbol: 'EURUSD',
    timeframe: 'M15',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: '10000',
    cost_model: { type: 'none' },
    status: netProfit === null ? 'queued' : 'done',
    error: null,
    created_at: '2024-01-01T00:00:00Z',
    finished_at: null,
    metrics: netProfit === null ? null : metrics(netProfit),
  }
  return { entry_id: entry.id, entry_name: entry.name, label, values: {}, run }
}

function aggregate(over: Partial<StudyAggregate>): StudyAggregate {
  return {
    points_total: 0,
    points_finished: 0,
    points_failed: 0,
    points_profitable: 0,
    best_label: null,
    best_return: null,
    worst_label: null,
    worst_return: null,
    median_return: null,
    ...over,
  }
}

const ZETA = { id: 'entry-z', name: 'zeta' }
const ALPHA = { id: 'entry-a', name: 'alpha' }

/**
 * Two entries, asked for as zeta then alpha, with medians that cannot be confused.
 *
 * ⚠️ **The runs arrive alpha first**, which is the order the server's rows come back in: its
 * query orders by `created_at` first, but every run of a sweep is written in one transaction and
 * Postgres's `now()` is the transaction's start, so the tie falls to the strategy's name. A screen
 * that built its sections by walking the runs would print them backwards, and the section test
 * below would see it.
 */
function sweep(over: Partial<SweepOut> = {}, entries?: SweepEntryOut[]): SweepOut {
  return {
    id: 'sweep-1',
    entry_ids: [ZETA.id, ALPHA.id],
    symbols: ['EURUSD', 'GBPUSD'],
    timeframes: ['M15', 'H1'],
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: '10000',
    created_at: '2024-01-01T00:00:00Z',
    skipped: [],
    entries: entries ?? [
      {
        entry_id: ZETA.id,
        entry_name: ZETA.name,
        aggregate: aggregate({
          points_total: 2,
          points_finished: 2,
          points_profitable: 2,
          median_return: '0.025',
          best_return: '0.03',
          worst_return: '0.02',
        }),
      },
      {
        entry_id: ALPHA.id,
        entry_name: ALPHA.name,
        aggregate: aggregate({
          points_total: 1,
          points_finished: 1,
          median_return: '-0.02',
          best_return: '-0.02',
          worst_return: '-0.02',
        }),
      },
    ],
    runs: [
      row('a1', ALPHA, 'M15', '-200'),
      row('z5', ZETA, 'M15 · period=5', '200'),
      row('z9', ZETA, 'M15 · period=9', '300'),
    ],
    ...over,
  }
}

function showing(data: SweepOut | undefined, state: 'pending' | 'error' | 'ok' = 'ok'): void {
  mockedSweep.mockReturnValue({
    data,
    isPending: state === 'pending',
    isError: state === 'error',
    error: state === 'error' ? new Error('nope') : null,
  } as unknown as ReturnType<typeof useSweep>)
  // A curve **per selected run**, not a fixed empty map — otherwise every assertion about the
  // chart would pass because nothing could ever be drawn.
  mockedCurves.mockImplementation((ids: readonly string[]) => ({
    curves: new Map(ids.map((id) => [id, [{ time: '2024-01-01T00:00:00Z', equity: '11000' }]])),
    isPending: false,
    isError: false,
  }))
}

function sections(): HTMLElement[] {
  return screen.getAllByRole('region')
}

function medianIn(section: HTMLElement): HTMLElement {
  // The tile, not the bare number: the best and worst tiles can print the same figure, and a
  // match on the number alone would pass on whichever tile happened to carry it.
  return within(section).getByText('Median point').parentElement!
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('SweepResult', () => {
  it('says what was asked', () => {
    showing(sweep())

    renderWithProviders(<SweepResult />)

    expect(screen.getByRole('heading', { level: 2 })).toHaveTextContent('2 entries over 3 backtests')
    expect(
      screen.getByText('EURUSD, GBPUSD · M15, H1 · 2024-01-01 → 2024-02-01 · 10,000.00 per run'),
    ).toBeInTheDocument()
    // Nothing left out, so nothing said: the warning is not furniture.
    expect(screen.queryByText(/left out/i)).not.toBeInTheDocument()
  })

  it('says which pairs were left out, under the axes that were asked', () => {
    // ⚠️ The header lists GBPUSD and H1 either way; without this line a pair with no runs reads
    // as measured. Read from the sweep, so it survives a reload — unlike the basket's.
    showing(
      sweep({
        skipped: [
          { symbol: 'GBPUSD', timeframe: 'H1', covers: null },
          { symbol: 'EURUSD', timeframe: 'H1', covers: '2020-01-01 to 2021-01-01' },
        ],
      }),
    )

    renderWithProviders(<SweepResult />)

    const warning = screen.getByRole('status', { name: 'left out' })
    expect(warning).toHaveTextContent('Left out — no candles in this window for 2 pairs:')
    expect(warning).toHaveTextContent('GBPUSD H1 — never collected')
    expect(warning).toHaveTextContent('EURUSD H1 — collected 2020-01-01 to 2021-01-01')
  })

  it('offers the dataset beside the dictionary that says what its columns are', () => {
    // Both, and as links the browser opens itself: the file for a model, the legend for whoever —
    // or whatever — reads it. A dataset offered without its dictionary is the half an AI misreads.
    showing(sweep())

    renderWithProviders(<SweepResult />)

    expect(screen.getByRole('link', { name: /download the dataset/i })).toHaveAttribute(
      'href',
      apiUrl('/sweeps/sweep-1/dataset.csv'),
    )
    expect(screen.getByRole('link', { name: /what each column means/i })).toHaveAttribute(
      'href',
      apiUrl('/sweeps/sweep-1/dataset/dictionary'),
    )
  })

  it('summarises each entry on its own, in the order the entries were asked for', () => {
    // ⚠️ The decision the whole screen holds. Entries are alternatives, so one median across them
    // is the median of two methods; a screen that printed the first summary under every heading
    // would still show a median in each section, and only the second section's figure tells.
    showing(sweep())

    renderWithProviders(<SweepResult />)

    const [zeta, alpha] = sections()
    expect(within(zeta!).getByRole('heading', { level: 3 })).toHaveTextContent('zeta')
    expect(within(alpha!).getByRole('heading', { level: 3 })).toHaveTextContent('alpha')
    expect(medianIn(zeta!)).toHaveTextContent('2.5%')
    expect(medianIn(alpha!)).toHaveTextContent('-2.0%')
  })

  it('lists under each entry only the runs that entry produced', () => {
    showing(sweep())

    renderWithProviders(<SweepResult />)

    const [zeta, alpha] = sections()
    // Regex, because the run table prints the name, its version and the window in one cell.
    expect(within(zeta!).getByText(/zeta \[M15 · period=5\]/)).toBeInTheDocument()
    expect(within(zeta!).getByText(/zeta \[M15 · period=9\]/)).toBeInTheDocument()
    expect(within(zeta!).queryByText(/alpha \[/)).not.toBeInTheDocument()
    expect(within(alpha!).getByText(/alpha \[M15\]/)).toBeInTheDocument()
    expect(within(alpha!).queryByText(/zeta \[/)).not.toBeInTheDocument()
  })

  it('counts the runs as X of Y while they are still landing', () => {
    // ⚠️ Read off the runs' own statuses, not the entries' aggregates: this fixture's aggregates
    // still say every point finished, so a count taken from them would read 3 of 3.
    const busy = sweep()
    busy.runs[0]!.run.status = 'queued'
    busy.runs[1]!.run.status = 'running'
    showing(busy)

    renderWithProviders(<SweepResult />)

    // Exact, suffix included: `toHaveTextContent` with a string matches any substring.
    expect(screen.getByRole('status')).toHaveTextContent(
      /^1 of 3 done · 1 running · 1 queued — this updates on its own\.$/,
    )
  })

  it('keeps the count on screen after everything has landed', () => {
    // The old line vanished once nothing was outstanding, so a finished sweep said nothing at all.
    showing(sweep())

    renderWithProviders(<SweepResult />)

    expect(screen.getByText('3 of 3 done')).toBeInTheDocument()
    // Nothing is in flight, so there is no live region to announce — the line is plain text.
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('counts a failed run as landed and keeps it in view', () => {
    // A failed run is not coming back, so it is not "still running" — but hiding it would make a
    // sweep that half worked read like one that worked.
    const withFailure = sweep()
    withFailure.runs[0]!.run.status = 'failed'
    showing(withFailure)

    renderWithProviders(<SweepResult />)

    expect(screen.getByText('2 of 3 done · 1 failed')).toBeInTheDocument()
  })

  it('names a run that will never execute as queued, not running', () => {
    // ⚠️ Measured on this project: a worker that took its job before Postgres accepted
    // connections left a run `queued` with no error on the row. The old line called it "still
    // running"; the counter names it queued.
    const stuck = sweep()
    stuck.runs[0]!.run.status = 'queued'
    showing(stuck)

    renderWithProviders(<SweepResult />)

    expect(screen.getByRole('status')).toHaveTextContent('2 of 3 done · 1 queued')
  })

  it('heads a removed entry as removed rather than borrowing one of its runs for a name', () => {
    const [zeta, alpha] = sweep().entries
    showing(sweep({}, [{ ...zeta!, entry_name: null }, alpha!]))

    renderWithProviders(<SweepResult />)

    const [removed] = sections()
    expect(within(removed!).getByRole('heading', { level: 3 })).toHaveTextContent(
      'An entry since removed from the shelf',
    )
    // The summary survives the label: removing a name must never blank a measurement.
    expect(medianIn(removed!)).toHaveTextContent('2.5%')
  })

  /**
   * Twenty-three zeta runs whose return and drawdown rank in opposite orders: `z<n>` returns n
   * and falls n %, so the best return (z23) has the deepest drawdown. One more zeta run has not
   * finished. Alpha has twelve runs, so it has a second page of its own.
   */
  function long(): SweepOut {
    const zetas = Array.from({ length: 23 }, (_, i) => {
      const n = i + 1
      const one = row(`z${String(n)}`, ZETA, `M15 · period=${String(n)}`, String(n))
      one.run.metrics!.max_drawdown_pct = String(n / 100)
      return one
    })
    const alphas = Array.from({ length: 12 }, (_, i) =>
      row(`a${String(i + 1)}`, ALPHA, `M15 · period=${String(i + 1)}`, String(-i)),
    )
    return sweep({
      runs: [...alphas, row('zq', ZETA, 'M15 · period=99', null), ...zetas],
    })
  }

  function shownIn(section: HTMLElement): string[] {
    return within(section)
      .getAllByRole('link')
      .map((link) => link.getAttribute('href')?.replace('/results/z', '') ?? '?')
  }

  it('lists each entry best return first, ten at a time, with unfinished runs last', () => {
    showing(long())
    renderWithProviders(<SweepResult />)
    const [zeta] = sections()

    expect(shownIn(zeta!)).toEqual(['23', '22', '21', '20', '19', '18', '17', '16', '15', '14'])
    expect(
      within(zeta!).getByText(
        'Runs 1–10 of 24, best return first. Runs with nothing to rank by — unfinished, failed, or without this measure — come last.',
      ),
    ).toBeInTheDocument()

    const pager = within(zeta!).getByRole('navigation', { name: 'zeta pages' })
    fireEvent.click(within(pager).getByRole('button', { name: 'Worse →' }))
    fireEvent.click(within(pager).getByRole('button', { name: 'Worse →' }))

    expect(within(pager).getByText('Page 3 of 3')).toBeInTheDocument()
    expect(shownIn(zeta!)).toEqual(['3', '2', '1', 'q'])
    expect(within(pager).getByRole('button', { name: 'Worse →' })).toBeDisabled()
    fireEvent.click(within(pager).getByRole('button', { name: '← Better' }))
    expect(within(pager).getByText('Page 2 of 3')).toBeInTheDocument()
  })

  it('pages each entry on its own', () => {
    showing(long())
    renderWithProviders(<SweepResult />)
    const [zeta, alpha] = sections()

    fireEvent.click(within(zeta!).getByRole('button', { name: 'Worse →' }))

    expect(within(zeta!).getByText(/^Runs 11–20 of 24,/)).toBeInTheDocument()
    expect(within(alpha!).getByText(/^Runs 1–10 of 12,/)).toBeInTheDocument()
  })

  it('gives a list that fits one page no pager', () => {
    showing(sweep())
    renderWithProviders(<SweepResult />)
    const [, alpha] = sections()

    expect(within(alpha!).queryByRole('navigation')).not.toBeInTheDocument()
    expect(within(alpha!).getByText(/^Runs 1–1 of 1,/)).toBeInTheDocument()
  })

  it('reranks by the measure picked, and starts every entry from its best again', () => {
    showing(long())
    renderWithProviders(<SweepResult />)
    const [zeta, alpha] = sections()
    fireEvent.click(within(zeta!).getByRole('button', { name: 'Worse →' }))
    fireEvent.click(within(alpha!).getByRole('button', { name: 'Worse →' }))

    fireEvent.change(screen.getByLabelText("Rank each entry's runs by"), {
      target: { value: 'drawdown' },
    })

    // Shallowest first: the opposite of the return order.
    expect(shownIn(zeta!)).toEqual(['1', '2', '3', '4', '5', '6', '7', '8', '9', '10'])
    expect(
      within(zeta!).getByText(/^Runs 1–10 of 24, best smallest drawdown first\./),
    ).toBeInTheDocument()
    expect(within(alpha!).getByText(/^Runs 1–10 of 12,/)).toBeInTheDocument()
  })

  it('puts the median above the ranked list, so the best run is never read first', () => {
    showing(long())
    renderWithProviders(<SweepResult />)
    const [zeta] = sections()

    const median = medianIn(zeta!)
    const table = within(zeta!).getByRole('table')
    // DOCUMENT_POSITION_FOLLOWING: the table comes after the median tile.
    expect(median.compareDocumentPosition(table) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('says an entry whose every point was refused has no runs', () => {
    // Reachable: a launch drops the combinations the DSL refuses, and an entry can lose all of them.
    showing(sweep({ runs: [row('a1', ALPHA, 'M15', '-200')] }))
    renderWithProviders(<SweepResult />)
    const [zeta] = sections()

    expect(within(zeta!).getByText('No runs yet.')).toBeInTheDocument()
    expect(within(zeta!).queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('offers every measure to rank by', () => {
    showing(sweep())
    renderWithProviders(<SweepResult />)

    const options = within(screen.getByLabelText("Rank each entry's runs by")).getAllByRole(
      'option',
    )
    expect(options.map((one) => one.textContent)).toEqual([
      'Return',
      'Profit factor',
      'Win rate',
      'Expectancy',
      'Smallest drawdown',
    ])
  })

  it('keeps a run seated on the chart after its page is left', () => {
    // Seats are by run id over every run, not over the page on screen.
    showing(long())
    renderWithProviders(<SweepResult />)
    const [zeta] = sections()
    fireEvent.click(within(zeta!).getAllByRole('checkbox')[0]!)

    fireEvent.click(within(zeta!).getByRole('button', { name: 'Worse →' }))

    expect(screen.getByTestId('chart')).toHaveTextContent('z23')
  })

  it('compares runs from different entries on the one chart', () => {
    // ⚠️ The reason there is one chart and not one per section: the comparison worth making often
    // crosses methods, and seats kept per section could never hold both ends of it.
    showing(sweep())

    renderWithProviders(<SweepResult />)
    const [zeta, alpha] = sections()
    fireEvent.click(within(zeta!).getAllByRole('checkbox')[0]!)
    fireEvent.click(within(alpha!).getAllByRole('checkbox')[0]!)

    // The first row of each section is its best run by return, not the first to arrive.
    const chart = screen.getByTestId('chart')
    expect(chart).toHaveTextContent('z9')
    expect(chart).toHaveTextContent('a1')
  })

  it('seats nothing on the chart on its own', () => {
    showing(sweep())

    renderWithProviders(<SweepResult />)

    expect(screen.getByTestId('chart')).toHaveTextContent('')
  })

  it('says it is loading rather than rendering an empty sweep', () => {
    showing(undefined, 'pending')

    renderWithProviders(<SweepResult />)

    expect(screen.getByText(/Loading the sweep/)).toBeInTheDocument()
  })

  it('reports a failure to load instead of a blank screen', () => {
    showing(undefined, 'error')

    renderWithProviders(<SweepResult />)

    expect(screen.getByText(/Could not load this sweep/)).toBeInTheDocument()
  })
})
