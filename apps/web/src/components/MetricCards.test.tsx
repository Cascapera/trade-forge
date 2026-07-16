import { render, screen } from '@testing-library/react'

import type { Metrics } from '../api/types'
import { MetricCards } from './MetricCards'

const metrics: Metrics = {
  net_profit: '100',
  gross_profit: '200',
  gross_loss: '-100',
  total_trades: 2,
  long_trades: 1,
  short_trades: 1,
  win_rate: '0.5',
  payoff: '2',
  profit_factor: '2',
  expectancy: '50',
  max_drawdown_abs: '100',
  max_drawdown_pct: '0.0098',
  max_dd_duration_days: 0,
  sharpe: '0.5',
  sortino: '0.7',
  cagr: null,
  avg_trade_duration: null,
}

describe('MetricCards', () => {
  it('renders the headline numbers', () => {
    render(<MetricCards metrics={metrics} />)
    expect(screen.getByText('+100.00')).toBeInTheDocument()
    expect(screen.getByText('50.0%')).toBeInTheDocument()
    // Profit factor and payoff both read 2.00.
    expect(screen.getAllByText('2.00')).toHaveLength(2)
    expect(screen.getByText('1.0%')).toBeInTheDocument() // drawdown, one-decimal display
    expect(screen.getByText('0.50')).toBeInTheDocument() // sharpe
  })

  it('shows an em dash for an undefined expectancy', () => {
    render(<MetricCards metrics={{ ...metrics, expectancy: null }} />)
    expect(screen.getByText('—')).toBeInTheDocument()
  })
})
