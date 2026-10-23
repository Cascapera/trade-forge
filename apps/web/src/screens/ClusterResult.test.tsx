import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { ClusterOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { ClusterResult } from './ClusterResult'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, getCluster: vi.fn() } }
})

// The chart draws on a canvas jsdom does not have; its own test covers it.
vi.mock('../components/EquityCurve', () => ({
  EquityCurve: () => <div>equity curve</div>,
}))

const getCluster = vi.mocked(api.getCluster)

function cluster(over: Partial<ClusterOut>): ClusterOut {
  return {
    id: 'c1',
    name: 'H4 majors',
    status: 'done',
    error: null,
    initial_capital: '10000',
    max_open_positions: 5,
    max_open_risk_percent: '5',
    created_at: '2026-09-25T18:00:00Z',
    finished_at: '2026-09-25T18:00:05Z',
    members: [
      {
        backtest_id: 'r1',
        risk_percent: '1',
        label: 'MM9 [H4 · rr=2]',
        symbol: 'EURUSD',
        timeframe: 'H4',
        offered: 40,
        taken: 35,
        skipped: { positions: 3, risk: 2 },
        net_pnl: '812.5',
      },
    ],
    final_balance: '10812.5',
    net_return: '0.08125',
    max_drawdown_pct: '0.064',
    max_drawdown_abs: '690',
    most_open: 4,
    yearly_return: { '2020': '0.05', '2021': '-0.02' },
    curve: [{ time: '2020-01-02T00:00:00Z', balance: '10000', equity: '10000' }],
    ...over,
  }
}

function show(): void {
  renderWithProviders(
    <Routes>
      <Route path="/clusters/:id" element={<ClusterResult />} />
    </Routes>,
    '/clusters/c1',
  )
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('ClusterResult', () => {
  it('shows what the shared account went through and what each member brought', async () => {
    getCluster.mockResolvedValue(cluster({}))
    show()

    expect(await screen.findByText('10,812.50')).toBeInTheDocument()
    expect(screen.getByText('8.1%')).toBeInTheDocument()
    expect(screen.getByText('6.4% · 690.00')).toBeInTheDocument()
    expect(screen.getByText('35 of 40')).toBeInTheDocument()
    expect(screen.getByText('3 too many open, 2 too much risk open')).toBeInTheDocument()
    expect(screen.getByText('2021 -2.0%')).toBeInTheDocument()
    expect(screen.getByText('equity curve')).toBeInTheDocument()
  })

  it('says a replay failed, in its own words', async () => {
    getCluster.mockResolvedValue(
      cluster({ status: 'failed', error: 'KeyError: gone', final_balance: null, curve: null }),
    )
    show()

    expect(await screen.findByRole('alert')).toHaveTextContent('The replay failed: KeyError: gone')
  })

  it('marks a member that was run again to keep its trades, and says the cluster waits for it', async () => {
    getCluster.mockResolvedValue(
      cluster({
        status: 'queued',
        final_balance: null,
        curve: null,
        members: [
          {
            backtest_id: 'twin-1',
            rerun_of: 'orig-1',
            risk_percent: '1',
            label: 'MM9 [H4 · rr=2]',
            symbol: 'EURUSD',
            timeframe: 'H4',
            offered: null,
            taken: null,
            skipped: null,
            net_pnl: null,
          },
        ],
      }),
    )
    show()

    expect(await screen.findByText('run again')).toHaveAttribute(
      'title',
      expect.stringContaining('orig-1'),
    )
    expect(screen.getByText(/being run again first/)).toBeInTheDocument()
  })

  it('says it is still replaying rather than showing empty numbers', async () => {
    getCluster.mockResolvedValue(cluster({ status: 'running', final_balance: null, curve: null }))
    show()

    expect(await screen.findByText(/Replaying on one account/)).toBeInTheDocument()
    expect(screen.queryByText('Final balance')).not.toBeInTheDocument()
  })
})
