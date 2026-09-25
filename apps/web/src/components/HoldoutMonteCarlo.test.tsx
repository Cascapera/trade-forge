import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { MonteCarloOut, MonteCarloPoint } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { HoldoutMonteCarlo } from './HoldoutMonteCarlo'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: { ...actual.api, listMonteCarlos: vi.fn(), createMonteCarlo: vi.fn() },
  }
})

const listMonteCarlos = vi.mocked(api.listMonteCarlos)
const createMonteCarlo = vi.mocked(api.createMonteCarlo)

function point(over: Partial<MonteCarloPoint>): MonteCarloPoint {
  return {
    run_id: 'r1',
    entry_id: 'e1',
    entry_name: 'CHOCH',
    symbol: 'EURUSD',
    timeframe: 'H4',
    label: 'H4 · edge',
    trades_kept: true,
    trades: 120,
    observed_net_r: '9',
    observed_drawdown_r: '6.5',
    observed_losing_streak: 5,
    simulated: {
      paths: 1000,
      trades: 120,
      drawdown_r: { p5: '4', p50: '8.25', p95: '17.5', p99: '22' },
      losing_streak: { p5: '4', p50: '6', p95: '10', p99: '12' },
      net_r: { p5: '-6', p50: '9', p95: '24', p99: '30' },
      negative_share: '0.18',
    },
    ...over,
  }
}

const resampled: MonteCarloOut = {
  id: 'm1',
  sweep_id: 't1',
  paths: 1000,
  seed: 'abc',
  created_at: '2026-09-25T18:00:00Z',
  points: [
    point({}),
    point({
      run_id: 'r2',
      label: 'H4 · thin',
      trades: 12,
      observed_drawdown_r: '2',
      observed_losing_streak: 2,
      simulated: null,
    }),
    point({
      run_id: 'r3',
      label: 'H4 · lost',
      trades_kept: false,
      trades: 0,
      observed_net_r: '0',
      observed_drawdown_r: '0',
      observed_losing_streak: 0,
      simulated: null,
    }),
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  listMonteCarlos.mockResolvedValue([])
  createMonteCarlo.mockResolvedValue(resampled)
})

describe('HoldoutMonteCarlo', () => {
  it('waits for the test to finish', async () => {
    renderWithProviders(<HoldoutMonteCarlo sweepId="t1" settled={false} />)

    expect(await screen.findByText(/still running/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('sends the paths, and the seed only when one was typed', async () => {
    renderWithProviders(<HoldoutMonteCarlo sweepId="t1" settled />)

    fireEvent.click(await screen.findByRole('button', { name: 'Resample' }))
    await waitFor(() => {
      expect(createMonteCarlo).toHaveBeenCalledWith('t1', { paths: 1000 })
    })

    fireEvent.change(screen.getByLabelText('Paths per point'), { target: { value: '2000' } })
    fireEvent.change(screen.getByLabelText('Seed (optional)'), { target: { value: ' mine ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resample' }))
    await waitFor(() => {
      expect(createMonteCarlo).toHaveBeenLastCalledWith('t1', { paths: 2000, seed: 'mine' })
    })
  })

  it('refuses a path count outside 100 to 5000', async () => {
    renderWithProviders(<HoldoutMonteCarlo sweepId="t1" settled />)

    fireEvent.change(await screen.findByLabelText('Paths per point'), { target: { value: '50' } })

    expect(screen.getByText('Between 100 and 5000 paths.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('shows what happened beside the median and the 95% to be ready for', async () => {
    listMonteCarlos.mockResolvedValue([resampled])
    renderWithProviders(<HoldoutMonteCarlo sweepId="t1" settled />)

    const article = await screen.findByRole('article', { name: /1000 paths per point · seed abc/ })
    expect(within(article).getByText(/fell 6\.5 R · 5 losses in a row/)).toBeInTheDocument()
    expect(within(article).getByText('8.3 R')).toBeInTheDocument()
    expect(within(article).getByText('95%: 17.5 R')).toBeInTheDocument()
    expect(within(article).getByText('95%: 10')).toBeInTheDocument()
    expect(within(article).getByText('18%')).toBeInTheDocument()
    expect(within(article).getByText('12 trades — too few to resample')).toBeInTheDocument()
    expect(within(article).getByText('no trades kept')).toBeInTheDocument()
  })
})
