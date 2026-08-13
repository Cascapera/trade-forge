import { fireEvent, screen } from '@testing-library/react'

import type { Backtest, CandlesResponse, Metrics, Trade } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useBacktest: vi.fn(),
  useTrades: vi.fn(),
  useEquity: vi.fn(),
  useCandles: vi.fn(),
  useOverlays: vi.fn(),
  useTradeSnapshot: vi.fn(),
}))

// Both charts draw to a canvas jsdom lacks; stub them out — each has its own test.
vi.mock('../components/EquityCurve', () => ({ EquityCurve: () => <div>equity chart</div> }))
vi.mock('../components/PriceChart', () => ({
  PriceChart: ({ selectedTradeId }: { selectedTradeId: number | null }) => (
    <div>price chart, showing trade {selectedTradeId ?? 'none'}</div>
  ),
}))

import { useBacktest, useCandles, useEquity, useOverlays, useTrades } from '../api/hooks'
import { Results } from './Results'

const mockedBacktest = vi.mocked(useBacktest)
const mockedTrades = vi.mocked(useTrades)
const mockedEquity = vi.mocked(useEquity)
const mockedCandles = vi.mocked(useCandles)
const mockedOverlays = vi.mocked(useOverlays)

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
    candles_seen: null,
    first_candle: null,
    last_candle: null,
    metrics: null,
    ...over,
  }
}

function stubBacktest(value: unknown): void {
  mockedBacktest.mockReturnValue(value as ReturnType<typeof useBacktest>)
}

function trade(over: Partial<Trade>): Trade {
  return {
    id: 1,
    direction: 'long',
    entry_time: '2024-08-01T14:00:00Z',
    entry_price: '226.50',
    exit_time: '2024-08-01T17:00:00Z',
    exit_price: '228.00',
    exit_reason: 'take_profit',
    volume: '10',
    stop_loss: '225.00',
    take_profit: '229.50',
    gross_pnl: '15.00',
    costs: '0',
    net_pnl: '15.00',
    r_multiple: '2.00',
    context: {},
    has_snapshot: false,
    ...over,
  }
}

function candles(over: Partial<CandlesResponse> = {}): CandlesResponse {
  return {
    timeframe: 'H1',
    symbol: 'AAPL',
    candles_seen: 3,
    first_candle: '2024-08-01T13:00:00Z',
    last_candle: '2024-08-01T15:00:00Z',
    count: 3,
    candles: [],
    ...over,
  }
}

beforeEach(() => {
  mockedTrades.mockReturnValue({ data: { total: 0, limit: 100, offset: 0, items: [] } } as never)
  mockedEquity.mockReturnValue({ data: [] } as never)
  mockedCandles.mockReturnValue({ data: candles(), isPending: false, isError: false } as never)
  mockedOverlays.mockReturnValue({ data: { symbol: 'AAPL', timeframe: 'H1', candles_seen: 3, count: 3, series: [] } } as never)
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

  it('warns when the run covered less than was asked for', () => {
    stubBacktest({
      isPending: false,
      isError: false,
      data: backtest({
        status: 'done',
        metrics,
        date_from: '2024-01-01T00:00:00Z',
        date_to: '2026-08-03T00:00:00Z',
        candles_seen: 3480,
        first_candle: '2024-08-01T13:00:00Z',
        last_candle: '2026-07-31T19:00:00Z',
      }),
    })
    renderWithProviders(<Results />)

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('2024-08-01 to 2026-07-31')
    expect(notice).toHaveTextContent('3,480 candles')
  })

  it('says nothing about coverage when the run covered the request', () => {
    stubBacktest({
      isPending: false,
      isError: false,
      data: backtest({
        status: 'done',
        metrics,
        date_from: '2024-09-01T00:00:00Z',
        date_to: '2024-09-30T00:00:00Z',
        candles_seen: 500,
        first_candle: '2024-08-15T00:00:00Z',
        last_candle: '2024-10-05T00:00:00Z',
      }),
    })
    renderWithProviders(<Results />)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
  })
})

describe('Results — the price tab', () => {
  function done(): void {
    stubBacktest({ isPending: false, isError: false, data: backtest({ status: 'done', metrics }) })
  }

  it('opens on the results tab, not on the price one', () => {
    done()
    renderWithProviders(<Results />)

    expect(screen.getByRole('tab', { name: 'Results' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('tab', { name: 'Price' })).toHaveAttribute('aria-selected', 'false')
    expect(screen.queryByText(/price chart/)).not.toBeInTheDocument()
  })

  it('does not fetch the candles until the price tab is open', () => {
    // The point of the `enabled` flag, and it is worth a test of its own: this is the largest
    // payload the page can ask for — thousands of bars against a handful of metrics — and a
    // reader who never leaves the results tab must not pay for it. A hook wired without the
    // condition would still render correctly, and the cost would be invisible on screen.
    done()
    renderWithProviders(<Results />)

    expect(mockedCandles).toHaveBeenLastCalledWith(undefined, false)

    fireEvent.click(screen.getByRole('tab', { name: 'Price' }))

    expect(mockedCandles).toHaveBeenLastCalledWith(undefined, true)
  })

  it('takes the reader to the price chart, on the trade they picked', () => {
    done()
    mockedTrades.mockReturnValue({
      data: { total: 1, limit: 100, offset: 0, items: [trade({ id: 42 })] },
    } as never)
    renderWithProviders(<Results />)

    fireEvent.click(screen.getByRole('button', { name: /on the price chart/i }))

    expect(screen.getByRole('tab', { name: 'Price' })).toHaveAttribute('aria-selected', 'true')
    // Both halves matter: switching tabs without carrying the choice would land the reader on a
    // chart of the whole run with nothing to say which trade they had asked about.
    expect(screen.getByText('price chart, showing trade 42')).toBeInTheDocument()
  })

  it('says so when the dataset no longer matches what the run read', () => {
    // A re-collected dataset can hold a different number of bars for the same window. The chart
    // would still draw, and it would be a chart of bars the trades were not decided on.
    done()
    mockedCandles.mockReturnValue({
      data: candles({ candles_seen: 3480, count: 3200 }),
      isPending: false,
      isError: false,
    } as never)
    renderWithProviders(<Results />)

    fireEvent.click(screen.getByRole('tab', { name: 'Price' }))

    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('3,480')
    expect(notice).toHaveTextContent('3,200')
  })

  it('says nothing when the dataset still matches', () => {
    done()
    renderWithProviders(<Results />)

    fireEvent.click(screen.getByRole('tab', { name: 'Price' }))

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText(/price chart/)).toBeInTheDocument()
  })

  it('surfaces a failure to load the price instead of an empty chart', () => {
    done()
    mockedCandles.mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new Error('this run did not record which candles it read'),
    } as never)
    renderWithProviders(<Results />)

    fireEvent.click(screen.getByRole('tab', { name: 'Price' }))

    expect(screen.getByText(/did not record which candles/i)).toBeInTheDocument()
    expect(screen.queryByText(/price chart/)).not.toBeInTheDocument()
  })
})
