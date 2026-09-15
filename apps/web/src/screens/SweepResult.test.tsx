import { fireEvent, screen, within } from '@testing-library/react'

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

  it('says how many backtests are still running rather than showing dashes in silence', () => {
    showing(
      sweep({}, [
        {
          entry_id: ZETA.id,
          entry_name: ZETA.name,
          aggregate: aggregate({ points_total: 2, points_finished: 1 }),
        },
        {
          entry_id: ALPHA.id,
          entry_name: ALPHA.name,
          aggregate: aggregate({ points_total: 1 }),
        },
      ]),
    )

    renderWithProviders(<SweepResult />)

    // Summed across the sections: one in zeta and one in alpha.
    expect(screen.getByRole('status')).toHaveTextContent('2 of 3 backtests still running')
  })

  it('counts a failed run as landed, not as still running', () => {
    // A failed run is not coming back. Counted as pending, the screen would promise an update
    // that never arrives — and it would say so for as long as the tab stays open.
    showing(
      sweep({}, [
        {
          entry_id: ZETA.id,
          entry_name: ZETA.name,
          aggregate: aggregate({ points_total: 2, points_finished: 1, points_failed: 1 }),
        },
        {
          entry_id: ALPHA.id,
          entry_name: ALPHA.name,
          aggregate: aggregate({ points_total: 1, points_finished: 1 }),
        },
      ]),
    )

    renderWithProviders(<SweepResult />)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
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

  it('compares runs from different entries on the one chart', () => {
    // ⚠️ The reason there is one chart and not one per section: the comparison worth making often
    // crosses methods, and seats kept per section could never hold both ends of it.
    showing(sweep())

    renderWithProviders(<SweepResult />)
    const [zeta, alpha] = sections()
    fireEvent.click(within(zeta!).getAllByRole('checkbox')[0]!)
    fireEvent.click(within(alpha!).getAllByRole('checkbox')[0]!)

    const chart = screen.getByTestId('chart')
    expect(chart).toHaveTextContent('z5')
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
