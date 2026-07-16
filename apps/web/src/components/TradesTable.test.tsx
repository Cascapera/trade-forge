import { render, screen } from '@testing-library/react'

import type { Trade } from '../api/types'
import { TradesTable } from './TradesTable'

const win: Trade = {
  id: 1,
  direction: 'long',
  entry_time: '2024-01-01T00:00:00Z',
  entry_price: '1.10000',
  exit_time: '2024-01-01T01:00:00Z',
  exit_price: '1.10200',
  exit_reason: 'tp',
  volume: '1',
  stop_loss: '1.09900',
  take_profit: '1.10200',
  gross_pnl: '200',
  costs: '0',
  net_pnl: '200',
  r_multiple: '2',
  context: {},
}

const open: Trade = {
  ...win,
  id: 2,
  exit_time: null,
  exit_price: null,
  exit_reason: null,
  net_pnl: null,
  r_multiple: null,
}

describe('TradesTable', () => {
  it('renders a row per trade with net and R', () => {
    render(<TradesTable trades={[win]} />)
    expect(screen.getByText('+200.00')).toBeInTheDocument()
    expect(screen.getByText('2.00R')).toBeInTheDocument()
    expect(screen.getByText('tp')).toBeInTheDocument()
  })

  it('shows em dashes for the columns an open trade has not filled', () => {
    render(<TradesTable trades={[open]} />)
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3)
  })

  it('shows an empty-state message for no trades', () => {
    render(<TradesTable trades={[]} />)
    expect(screen.getByText('This run produced no trades.')).toBeInTheDocument()
  })
})
