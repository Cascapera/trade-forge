import { fireEvent, screen } from '@testing-library/react'

import type { BacktestListItem, Metrics } from '../api/types'
import { SERIES_COLORS, compareLabel } from '../backtest/compare'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useBacktests: vi.fn(),
  useEquityCurves: vi.fn(),
  useInstruments: vi.fn(),
}))

// The chart draws to a canvas jsdom lacks. Stubbed to report what it was *asked* to draw, which
// is what this screen is responsible for deciding — its own test covers the drawing.
vi.mock('../components/ComparisonChart', () => ({
  ComparisonChart: ({ series }: { series: { id: string; color: string }[] }) => (
    <div data-testid="chart">{series.map((one) => `${one.id}:${one.color}`).join(' ')}</div>
  ),
}))

import { useBacktests, useEquityCurves, useInstruments } from '../api/hooks'
import { RunLog } from './RunLog'

const mockedPage = vi.mocked(useBacktests)
const mockedCurves = vi.mocked(useEquityCurves)
const mockedInstruments = vi.mocked(useInstruments)

const metrics: Metrics = {
  net_profit: '1234.5',
  gross_profit: '3000',
  gross_loss: '-1765.5',
  total_trades: 25,
  long_trades: 25,
  short_trades: 0,
  win_rate: '0.44',
  payoff: '2.1',
  profit_factor: '1.7',
  expectancy: '49.38',
  max_drawdown_abs: '800',
  max_drawdown_pct: '0.082',
  max_dd_duration_days: 40,
  sharpe: '0.91',
  sortino: '1.35',
  cagr: '0.06',
  avg_trade_duration: null,
}

function listed(over: Partial<BacktestListItem>): BacktestListItem {
  return {
    id: 'b1',
    strategy_id: 's1',
    strategy_name: 'Ponto Contínuo',
    strategy_version: 2,
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-08-01T00:00:00Z',
    date_to: '2026-07-31T00:00:00Z',
    initial_capital: '10000',
    cost_model: { type: 'spread', spread_points: '12' },
    status: 'done',
    error: null,
    created_at: '2026-08-06T12:00:00Z',
    finished_at: '2026-08-06T12:00:30Z',
    metrics,
    ...over,
  }
}

function stubPage(items: BacktestListItem[], over: Record<string, unknown> = {}): void {
  mockedPage.mockReturnValue({
    isPending: false,
    isError: false,
    data: { total: items.length, limit: 50, offset: 0, items },
    ...over,
  } as never)
}

function tick(run: BacktestListItem): void {
  fireEvent.click(screen.getByRole('checkbox', { name: compareLabel(run) }))
}

beforeEach(() => {
  mockedInstruments.mockReturnValue({ data: [] } as never)
  mockedCurves.mockReturnValue({ curves: new Map(), isPending: false, isError: false })
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('RunLog', () => {
  it('shows a loading state while the log is fetching', () => {
    mockedPage.mockReturnValue({ isPending: true, isError: false } as never)
    renderWithProviders(<RunLog />)
    expect(screen.getByText(/loading the run log/i)).toBeInTheDocument()
  })

  it('shows an error state when the log cannot be loaded', () => {
    mockedPage.mockReturnValue({ isPending: false, isError: true } as never)
    renderWithProviders(<RunLog />)
    expect(screen.getByText(/could not load/i)).toBeInTheDocument()
  })

  it('counts the runs the API says exist, not the ones on this page', () => {
    // `total` is what sizes the log; a page of 50 out of 200 must not report 50.
    mockedPage.mockReturnValue({
      isPending: false,
      isError: false,
      data: { total: 200, limit: 50, offset: 0, items: [listed({})] },
    } as never)
    renderWithProviders(<RunLog />)
    expect(screen.getByText(/200 runs, newest first/i)).toBeInTheDocument()
  })

  it('says so plainly when no run matches the filters', () => {
    stubPage([])
    renderWithProviders(<RunLog />)
    expect(screen.getByText(/no runs match these filters/i)).toBeInTheDocument()
  })

  it('asks the API only for the filters that were set', () => {
    stubPage([listed({})])
    renderWithProviders(<RunLog />)
    expect(mockedPage).toHaveBeenCalledWith({})

    fireEvent.change(screen.getByLabelText('Timeframe'), { target: { value: 'M15' } })

    // Only timeframe. An empty `symbol` would ask for runs whose symbol is '' and match nothing.
    expect(mockedPage).toHaveBeenLastCalledWith({ timeframe: 'M15' })
  })

  it('puts a ticked run on the chart once its curve arrives', () => {
    const run = listed({})
    stubPage([run])
    mockedCurves.mockReturnValue({
      curves: new Map([['b1', [{ time: '2024-08-01T13:00:00Z', equity: '11000' }]]]),
      isPending: false,
      isError: false,
    })

    renderWithProviders(<RunLog />)
    tick(run)

    expect(screen.getByTestId('chart')).toHaveTextContent(`b1:${SERIES_COLORS[0] ?? ''}`)
  })

  it('requests a curve only for the runs that were ticked', () => {
    // The reason the list holds ids and not runs: thirty-six curves are nine megabytes, and the
    // two on screen are the only ones anyone is looking at.
    const run = listed({})
    // A second run of the *same* configuration, which is what a research log is full of: same
    // strategy, same instrument, relaunched. Their compare controls must still be tellable apart.
    stubPage([run, listed({ id: 'b2', created_at: '2026-08-06T18:00:00Z' })])

    renderWithProviders(<RunLog />)
    expect(mockedCurves).toHaveBeenLastCalledWith([])

    tick(run)
    expect(mockedCurves).toHaveBeenLastCalledWith(['b1'])
  })

  it('warns when a selected run charged nothing to trade', () => {
    const free = listed({ id: 'free', cost_model: { type: 'none' } })
    stubPage([free])

    renderWithProviders(<RunLog />)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    tick(free)

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('no spread and no commission')
    expect(notice).toHaveTextContent('AAPL H1 · Ponto Contínuo v2')
  })

  it('says nothing about costs when every selected run paid them', () => {
    // The warning has to stay rare to stay read. A costed run must not trip it.
    const costed = listed({ id: 'costed' })
    stubPage([costed])

    renderWithProviders(<RunLog />)
    tick(costed)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })

  it('says "every run" when the whole selection ran free', () => {
    // Different wording from the mixed case on purpose: "1 of these" tells the reader to go find
    // which one, and there is nothing to find when the answer is all of them.
    const free = listed({ id: 'free', cost_model: { type: 'none' } })
    stubPage([free])

    renderWithProviders(<RunLog />)
    tick(free)

    expect(screen.getByRole('status')).toHaveTextContent('Every run charged')
  })

  it('names which of a mixed selection ran free, not just that some did', () => {
    const free = listed({ id: 'free', symbol: 'EURUSD', timeframe: 'M15', cost_model: { type: 'none' } })
    const costed = listed({ id: 'costed' })
    stubPage([free, costed])

    renderWithProviders(<RunLog />)
    tick(free)
    tick(costed)

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('1 of these')
    expect(notice).toHaveTextContent('EURUSD M15 · Ponto Contínuo v2')
    expect(notice).not.toHaveTextContent('AAPL')
  })

  it('offers the symbols the API knows about as filters', () => {
    mockedInstruments.mockReturnValue({
      data: [{ symbol: 'AAPL' }, { symbol: 'EURUSD' }],
    } as never)
    stubPage([listed({})])

    renderWithProviders(<RunLog />)
    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: 'EURUSD' } })

    expect(mockedPage).toHaveBeenLastCalledWith({ symbol: 'EURUSD' })
  })

  it('filters by status', () => {
    stubPage([listed({})])
    renderWithProviders(<RunLog />)

    fireEvent.change(screen.getByLabelText('Status'), { target: { value: 'failed' } })

    expect(mockedPage).toHaveBeenLastCalledWith({ status: 'failed' })
  })

  it('says a curve is on its way rather than showing an empty chart', () => {
    const run = listed({})
    stubPage([run])
    mockedCurves.mockReturnValue({ curves: new Map(), isPending: true, isError: false })

    renderWithProviders(<RunLog />)
    expect(screen.queryByText(/loading 1 curve/i)).not.toBeInTheDocument()

    tick(run)

    expect(screen.getByText(/loading 1 curve/i)).toBeInTheDocument()
  })

  it('explains the limit once every seat is taken', () => {
    const runs = Array.from({ length: 7 }, (_, i) =>
      listed({ id: `r${String(i)}`, created_at: `2026-08-0${String(i + 1)}T12:00:00Z` }),
    )
    stubPage(runs)

    renderWithProviders(<RunLog />)
    for (const run of runs.slice(0, 6)) tick(run)

    expect(screen.getByText(/untick one to add another/i)).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: compareLabel(runs[6]!) })).toBeDisabled()
  })

  it('counts one run in the singular', () => {
    stubPage([listed({})])
    renderWithProviders(<RunLog />)
    expect(screen.getByText(/1 run, newest first/i)).toBeInTheDocument()
  })

  it('calls the comparison aligned on real dates only once there are two', () => {
    const a = listed({ id: 'a' })
    const b = listed({ id: 'b', created_at: '2026-08-05T12:00:00Z' })
    stubPage([a, b])
    mockedCurves.mockReturnValue({
      curves: new Map([
        ['a', [{ time: '2024-08-01T13:00:00Z', equity: '11000' }]],
        ['b', [{ time: '2025-01-01T13:00:00Z', equity: '9000' }]],
      ]),
      isPending: false,
      isError: false,
    })

    renderWithProviders(<RunLog />)
    tick(a)
    expect(screen.queryByText(/aligned on real dates/i)).not.toBeInTheDocument()

    tick(b)
    expect(screen.getByText(/aligned on real dates/i)).toBeInTheDocument()
  })
})
