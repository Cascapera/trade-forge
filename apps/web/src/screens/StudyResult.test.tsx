import { fireEvent, screen } from '@testing-library/react'

import type { BacktestListItem, Metrics, StudyOut, StudyPoint } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useStudy: vi.fn(),
  useEquityCurves: vi.fn(),
}))

// The chart draws to a canvas jsdom lacks. Stubbed to report what it was *asked* to draw, which
// is what this screen decides — its own test covers the drawing.
vi.mock('../components/ComparisonChart', () => ({
  ComparisonChart: ({ series }: { series: { id: string }[] }) => (
    <div data-testid="chart">{series.map((one) => one.id).join(' ')}</div>
  ),
}))

import { useEquityCurves, useStudy } from '../api/hooks'

import { StudyResult } from './StudyResult'

const mockedStudy = vi.mocked(useStudy)
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

function run(id: string, netProfit: string | null): BacktestListItem {
  return {
    id,
    strategy_id: id,
    strategy_name: `MME9 [${id}]`,
    strategy_version: 1,
    symbol: 'AAPL',
    timeframe: 'H1',
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
}

/** A 2x1 study whose two points returned the profits given. */
function study(profits: readonly (string | null)[], over: Partial<StudyOut> = {}): StudyOut {
  const points: StudyPoint[] = [5, 9].map((period) => ({
    backtest_id: `p${String(period)}`,
    strategy_id: `p${String(period)}`,
    label: `period=${String(period)}`,
    values: { 'setup.params.period': period },
    status: 'done',
  }))
  const finished = profits.filter((each) => each !== null).length
  return {
    id: 'study-1',
    strategy_id: 'base',
    strategy_name: 'MME9 breakout',
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: '10000',
    created_at: '2024-01-01T00:00:00Z',
    grid: { 'setup.params.period': [5, 9] },
    points,
    aggregate: {
      points_total: 2,
      points_finished: finished,
      points_failed: 0,
      points_profitable: profits.filter((each) => each !== null && Number(each) > 0).length,
      best_label: finished > 0 ? 'period=9' : null,
      best_return: finished > 0 ? '0.2' : null,
      worst_label: finished > 0 ? 'period=5' : null,
      worst_return: finished > 0 ? '-0.05' : null,
      median_return: finished > 0 ? '0.075' : null,
    },
    runs: [run('p5', profits[0] ?? null), run('p9', profits[1] ?? null)],
    ...over,
  }
}

function showing(data: StudyOut | undefined, state: 'pending' | 'error' | 'ok' = 'ok'): void {
  mockedStudy.mockReturnValue({
    data,
    isPending: state === 'pending',
    isError: state === 'error',
    error: state === 'error' ? new Error('nope') : null,
  } as unknown as ReturnType<typeof useStudy>)
  // A curve **per selected run**, not a fixed empty map. Returning nothing whatever the screen
  // asked for would leave the chart empty in every case, and every assertion about seating
  // would pass for the wrong reason — the same trap the basket's own test documents.
  mockedCurves.mockImplementation((ids: readonly string[]) => ({
    curves: new Map(ids.map((id) => [id, [{ time: '2024-01-01T00:00:00Z', equity: '11000' }]])),
    isPending: false,
    isError: false,
  }))
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('StudyResult', () => {
  it('says how many points are still running rather than showing dashes in silence', () => {
    showing(study(['-500', null]))

    renderWithProviders(<StudyResult />)

    expect(screen.getByRole('status')).toHaveTextContent('1 of 2 points still running')
  })

  it('leads with the median and does not put the best point at the top', () => {
    // ⚠️ The reading order is the feature. A grid always has a best point, so a screen that
    // opened with it would present the luckiest corner of a search as the result.
    showing(study(['-500', '2000']))

    renderWithProviders(<StudyResult />)

    expect(screen.getByText('Median point')).toBeInTheDocument()
    expect(screen.getByText('7.5%')).toBeInTheDocument()
    // Scoped: the heatmap's own caption says it too, which is the point rather than a clash.
    expect(screen.getByText(/Every figure here is/)).toBeInTheDocument()
  })

  it('draws the grid and lists every point in the run table', () => {
    showing(study(['-500', '2000']))

    renderWithProviders(<StudyResult />)

    expect(screen.getByRole('table', { name: /Return of every combination/ })).toBeInTheDocument()
    // Regex, because the run log renders the name, its version and the window in one cell —
    // an exact-text match would be asserting the cell's layout rather than the name.
    expect(screen.getByText(/MME9 \[p5\]/)).toBeInTheDocument()
    expect(screen.getByText(/MME9 \[p9\]/)).toBeInTheDocument()
  })

  it('seats nothing on the comparison chart on its own', () => {
    // ⚠️ The deliberate difference from a basket, and it is a difference of scale. Seating each
    // run as it lands is a service for a handful of markets; for five hundred points it would
    // fill every seat with whichever combinations happened to finish first, which is not a
    // comparison anybody chose.
    showing(study(['-500', '2000']))

    renderWithProviders(<StudyResult />)

    expect(screen.getByTestId('chart')).toHaveTextContent('')
    expect(screen.getByText(/Tick up to \d+ points/)).toBeInTheDocument()
  })

  it('puts a point on the chart when the reader picks it', () => {
    showing(study(['-500', '2000']))

    renderWithProviders(<StudyResult />)
    fireEvent.click(screen.getAllByRole('checkbox')[0]!)

    expect(screen.getByTestId('chart')).toHaveTextContent('p5')
  })

  it('says it is loading rather than rendering an empty study', () => {
    showing(undefined, 'pending')

    renderWithProviders(<StudyResult />)

    expect(screen.getByText(/Loading the study/)).toBeInTheDocument()
  })

  it('reports a failure to load instead of a blank screen', () => {
    showing(undefined, 'error')

    renderWithProviders(<StudyResult />)

    expect(screen.getByText(/Could not load this study/)).toBeInTheDocument()
  })
})
