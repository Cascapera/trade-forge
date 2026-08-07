import { fireEvent, screen } from '@testing-library/react'

import type { BacktestListItem, Metrics } from '../api/types'
import { EMPTY_SEATS, MAX_COMPARED, SERIES_COLORS, compareLabel, toggleSeat } from '../backtest/compare'
import { renderWithProviders } from '../test-utils'
import { RunTable } from './RunTable'

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
    cost_model: { type: 'none' },
    status: 'done',
    error: null,
    created_at: '2026-08-06T12:00:00Z',
    finished_at: '2026-08-06T12:00:30Z',
    metrics,
    ...over,
  }
}

function box(run: BacktestListItem): HTMLElement {
  return screen.getByRole('checkbox', { name: compareLabel(run) })
}

describe('RunTable', () => {
  it('shows all three column groups for a finished run', () => {
    const run = listed({})
    renderWithProviders(<RunTable runs={[run]} seats={EMPTY_SEATS} onToggle={vi.fn()} />)

    expect(screen.getByText('+1,234.50')).toBeInTheDocument() // essential
    expect(screen.getByText('25')).toBeInTheDocument()
    expect(screen.getByText('8.2%')).toBeInTheDocument()
    expect(screen.getByText('1.70')).toBeInTheDocument() // quality
    expect(screen.getByText('44.0%')).toBeInTheDocument()
    expect(screen.getByText('2.10')).toBeInTheDocument()
    expect(screen.getByText('0.91')).toBeInTheDocument() // risk-adjusted
    expect(screen.getByText('1.35')).toBeInTheDocument()
    expect(screen.getByText('none')).toBeInTheDocument() // costs
  })

  it('names the cost model rather than hiding it', () => {
    const run = listed({ cost_model: { type: 'spread', spread_points: '12' } })
    renderWithProviders(<RunTable runs={[run]} seats={EMPTY_SEATS} onToggle={vi.fn()} />)
    expect(screen.getByText('spread 12 pts')).toBeInTheDocument()
  })

  it('dashes the metrics of a run that has none instead of printing zeros', () => {
    // A queued run has not measured anything. Rendering 0.00 would read as "it made nothing",
    // which is a claim about a run that has not happened.
    const run = listed({ status: 'queued', metrics: null })
    renderWithProviders(<RunTable runs={[run]} seats={EMPTY_SEATS} onToggle={vi.fn()} />)

    expect(screen.getAllByText('—')).toHaveLength(8)
    expect(screen.queryByText('+0.00')).not.toBeInTheDocument()
  })

  it('will not let an unfinished run be ticked', () => {
    const run = listed({ status: 'running', metrics: null })
    renderWithProviders(<RunTable runs={[run]} seats={EMPTY_SEATS} onToggle={vi.fn()} />)
    expect(box(run)).toBeDisabled()
  })

  it('reports the run that was ticked', () => {
    const onToggle = vi.fn()
    const run = listed({})
    renderWithProviders(<RunTable runs={[run]} seats={EMPTY_SEATS} onToggle={onToggle} />)

    fireEvent.click(box(run))

    expect(onToggle).toHaveBeenCalledWith('b1')
  })

  it('paints a ticked row in the colour its line wears', () => {
    const run = listed({})
    const seats = toggleSeat(EMPTY_SEATS, 'b1')
    const { container } = renderWithProviders(
      <RunTable runs={[run]} seats={seats} onToggle={vi.fn()} />,
    )

    // The swatch on the control is the only tie between this row and its line on the chart, so it
    // has to be the same hex the chart draws — not a look-alike.
    const swatch = container.querySelector('span[style*="border-color"]')
    expect(swatch).toHaveStyle({ borderColor: SERIES_COLORS[0]! })
    expect(box(run)).toBeChecked()
  })

  it('blocks the run past the limit and says why', () => {
    // Six seats taken by runs that are not on this page, so the seventh cannot be ticked.
    let seats = EMPTY_SEATS
    for (let i = 0; i < MAX_COMPARED; i += 1) seats = toggleSeat(seats, `other-${String(i)}`)
    const run = listed({})

    renderWithProviders(<RunTable runs={[run]} seats={seats} onToggle={vi.fn()} />)

    expect(box(run)).toBeDisabled()
    expect(box(run)).toHaveAttribute('title', expect.stringContaining('untick one first'))
  })

  it('still lets a ticked run be unticked when the seats are full', () => {
    // The regression the `blocked` flag exists to avoid: disabling every box once full would trap
    // the reader with six runs and no way to change them.
    let seats = EMPTY_SEATS
    seats = toggleSeat(seats, 'b1')
    for (let i = 1; i < MAX_COMPARED; i += 1) seats = toggleSeat(seats, `other-${String(i)}`)
    const run = listed({})

    renderWithProviders(<RunTable runs={[run]} seats={seats} onToggle={vi.fn()} />)

    expect(box(run)).toBeEnabled()
  })
})
