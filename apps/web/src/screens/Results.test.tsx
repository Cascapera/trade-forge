import { screen } from '@testing-library/react'

import type { Backtest, Metrics } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useBacktest: vi.fn(),
  useTrades: vi.fn(),
  useEquity: vi.fn(),
}))

// The chart draws to a canvas jsdom lacks; stub it out — its own test covers the wiring.
vi.mock('../components/EquityCurve', () => ({ EquityCurve: () => <div>equity chart</div> }))

import { useBacktest, useEquity, useTrades } from '../api/hooks'
import { Results } from './Results'

const mockedBacktest = vi.mocked(useBacktest)
const mockedTrades = vi.mocked(useTrades)
const mockedEquity = vi.mocked(useEquity)

const metrics: Metrics = {
  net_profit: '100',
  gross_profit: '200',
  gross_loss: '-100',
  total_trades: 0,
  long_trades: 0,
  short_trades: 0,
  win_rate: '0',
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

function backtest(over: Partial<Backtest>): Backtest {
  return {
    id: 'b1',
    strategy_id: 's1',
    instrument_id: 'i1',
    timeframe: 'H1',
    date_from: '',
    date_to: '',
    initial_capital: '10000',
    status: 'queued',
    error: null,
    engine_version: '0.1.0',
    created_at: '',
    started_at: null,
    finished_at: null,
    metrics: null,
    ...over,
  }
}

function stubBacktest(value: unknown): void {
  mockedBacktest.mockReturnValue(value as ReturnType<typeof useBacktest>)
}

beforeEach(() => {
  mockedTrades.mockReturnValue({ data: { total: 0, limit: 100, offset: 0, items: [] } } as never)
  mockedEquity.mockReturnValue({ data: [] } as never)
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('Results', () => {
  it('shows a loading state while the first fetch is pending', () => {
    stubBacktest({ isPending: true, isError: false })
    renderWithProviders(<Results />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('shows an error state when the backtest cannot be loaded', () => {
    stubBacktest({ isPending: false, isError: true })
    renderWithProviders(<Results />)
    expect(screen.getByText(/could not load/i)).toBeInTheDocument()
  })

  it('tells the user the run is still going', () => {
    stubBacktest({ isPending: false, isError: false, data: backtest({ status: 'running' }) })
    renderWithProviders(<Results />)
    expect(screen.getByText(/updates itself/i)).toBeInTheDocument()
  })

  it('surfaces the reason a run failed', () => {
    stubBacktest({
      isPending: false,
      isError: false,
      data: backtest({ status: 'failed', error: 'no data' }),
    })
    renderWithProviders(<Results />)
    expect(screen.getByText(/no data/i)).toBeInTheDocument()
  })

  it('renders metrics, the equity chart and the trades once done', () => {
    stubBacktest({ isPending: false, isError: false, data: backtest({ status: 'done', metrics }) })
    renderWithProviders(<Results />)
    expect(screen.getByText('+100.00')).toBeInTheDocument()
    expect(screen.getByText('equity chart')).toBeInTheDocument()
    expect(screen.getByText('This run produced no trades.')).toBeInTheDocument()
  })
})
