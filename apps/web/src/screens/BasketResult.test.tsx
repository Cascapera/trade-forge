import { fireEvent, screen } from '@testing-library/react'

import type { BacktestListItem, BasketAggregate, BasketOut, Metrics } from '../api/types'
import { compareLabel } from '../backtest/compare'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useBasket: vi.fn(),
  useEquityCurves: vi.fn(),
}))

// The chart draws to a canvas jsdom lacks. Stubbed to report what it was *asked* to draw, which
// is what this screen is responsible for deciding — its own test covers the drawing.
vi.mock('../components/ComparisonChart', () => ({
  ComparisonChart: ({ series }: { series: { id: string; color: string }[] }) => (
    <div data-testid="chart">{series.map((one) => one.id).join(' ')}</div>
  ),
}))

import { useBasket, useEquityCurves } from '../api/hooks'
import { BasketResult } from './BasketResult'

const mockedBasket = vi.mocked(useBasket)
const mockedCurves = vi.mocked(useEquityCurves)

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

function run(over: Partial<BacktestListItem>): BacktestListItem {
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
    cost_model: { type: 'spread', spread_points: '12' },
    status: 'done',
    error: null,
    created_at: '2026-08-12T12:00:00Z',
    finished_at: '2026-08-12T12:00:30Z',
    metrics,
    ...over,
  }
}

function aggregate(over: Partial<BasketAggregate>): BasketAggregate {
  return {
    runs_total: 2,
    runs_finished: 2,
    runs_failed: 0,
    runs_profitable: 1,
    best_symbol: 'AAPL',
    best_return: '0.30',
    worst_symbol: 'EURUSD',
    worst_return: '-0.25',
    median_return: '0.02',
    ...over,
  }
}

function stub(runs: BacktestListItem[], over: Partial<BasketAggregate> = {}): BasketOut {
  const data: BasketOut = {
    id: 'k1',
    strategy_id: 's1',
    strategy_name: 'Ponto Contínuo',
    strategy_version: 2,
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-12-31T00:00:00Z',
    initial_capital: '10000',
    created_at: '2026-08-12T12:00:00Z',
    aggregate: aggregate({ runs_total: runs.length, ...over }),
    runs,
  }
  mockedBasket.mockReturnValue({ isPending: false, isError: false, data } as never)
  return data
}

beforeEach(() => {
  // The curves arrive for whatever is seated, which is how `useEquityCurves` behaves: one request
  // per selected run. Returning a fixed empty map instead would leave the chart empty whatever the
  // screen decided, and every assertion about seating would pass for the wrong reason.
  mockedCurves.mockImplementation((ids: readonly string[]) => ({
    curves: new Map(ids.map((id) => [id, [{ time: '2024-01-01T00:00:00Z', equity: '11000' }]])),
    isPending: false,
    isError: false,
  }))
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('BasketResult', () => {
  it('heads the screen with the strategy, the window and the capital per market', () => {
    stub([run({ id: 'a', symbol: 'AAPL' }), run({ id: 'b', symbol: 'EURUSD' })])
    renderWithProviders(<BasketResult />, '/baskets/k1')

    expect(screen.getByRole('heading', { name: /Ponto Contínuo v2 across 2 markets/ })).toBeInTheDocument()
    // Per market, not in total: every run started with the whole balance.
    expect(screen.getByText(/10,000.00 per market/)).toBeInTheDocument()
  })

  it('puts the finished runs on the chart without anyone ticking a box', () => {
    // A basket is launched to be read as a whole. Requiring a tick per market would make the
    // reader assemble by hand the comparison they just asked the server to run.
    stub([run({ id: 'a', symbol: 'AAPL' }), run({ id: 'b', symbol: 'EURUSD' })])
    renderWithProviders(<BasketResult />, '/baskets/k1')

    expect(screen.getByRole('checkbox', { name: compareLabel(run({ id: 'a' })) })).toBeChecked()
    expect(screen.getByTestId('chart')).toHaveTextContent('a b')
  })

  it('leaves a run unticked once the reader unticks it, across a refetch', () => {
    // ⚠️ The regression `newlyComparable`'s `offered` set exists for. The query polls, so the runs
    // arrive again every second; "seat every finished run that is not seated" would put this one
    // straight back and the checkbox would refuse to stay off, with nothing looking broken.
    const runs = [run({ id: 'a', symbol: 'AAPL' }), run({ id: 'b', symbol: 'EURUSD' })]
    stub(runs)
    const view = renderWithProviders(<BasketResult />, '/baskets/k1')

    fireEvent.click(screen.getByRole('checkbox', { name: compareLabel(run({ id: 'a' })) }))
    expect(screen.getByTestId('chart')).toHaveTextContent('b')

    // A new array with the same contents, exactly as a poll would deliver it.
    stub(runs.map((one) => ({ ...one })))
    view.rerender(<BasketResult />)

    expect(screen.getByRole('checkbox', { name: compareLabel(run({ id: 'a' })) })).not.toBeChecked()
    expect(screen.getByTestId('chart')).toHaveTextContent('b')
  })

  it('seats a market when it finishes, not while it is still queued', () => {
    const queued = run({ id: 'b', symbol: 'EURUSD', status: 'queued', metrics: null })
    stub([run({ id: 'a', symbol: 'AAPL' }), queued], { runs_finished: 1, runs_profitable: 1 })
    const view = renderWithProviders(<BasketResult />, '/baskets/k1')

    // A queued run has no curve; seating it would spend a colour slot on an empty line.
    expect(screen.getByTestId('chart')).toHaveTextContent('a')
    expect(screen.getByRole('status')).toHaveTextContent('1 of 2 markets still running')

    stub([run({ id: 'a', symbol: 'AAPL' }), run({ id: 'b', symbol: 'EURUSD' })])
    view.rerender(<BasketResult />)

    expect(screen.getByTestId('chart')).toHaveTextContent('a b')
    expect(screen.queryByText(/still running/)).not.toBeInTheDocument()
  })

  it('warns by name about the markets in this basket that paid nothing', () => {
    stub([
      run({ id: 'a', symbol: 'AAPL' }),
      run({ id: 'b', symbol: 'US500', cost_model: { type: 'none' } }),
    ])
    renderWithProviders(<BasketResult />, '/baskets/k1')

    const warnings = screen.getAllByRole('status')
    const costless = warnings.find((one) => one.textContent.includes('no spread'))
    expect(costless).toBeDefined()
    expect(costless).toHaveTextContent('US500')
    expect(costless).not.toHaveTextContent('1 of 2 markets still running')
  })

  it('says the returns are not additive, on the screen and not only in the code', () => {
    stub([run({ id: 'a' }), run({ id: 'b', symbol: 'EURUSD' })])
    renderWithProviders(<BasketResult />, '/baskets/k1')

    expect(screen.getByText(/never additive/i)).toBeInTheDocument()
  })

  it('reports a basket that could not be loaded', () => {
    mockedBasket.mockReturnValue({ isPending: false, isError: true, data: undefined } as never)
    renderWithProviders(<BasketResult />, '/baskets/k1')
    expect(screen.getByText(/could not load this basket/i)).toBeInTheDocument()

    mockedBasket.mockReturnValue({ isPending: true, isError: false, data: undefined } as never)
    renderWithProviders(<BasketResult />, '/baskets/k1')
    expect(screen.getByText(/loading the basket/i)).toBeInTheDocument()
  })
})
