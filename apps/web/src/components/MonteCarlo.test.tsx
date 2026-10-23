import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { MonteCarloOut, MonteCarloPoint } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { MonteCarlo } from './MonteCarlo'

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
    in_blocks: {
      paths: 1000,
      trades: 120,
      drawdown_r: { p5: '5', p50: '10.5', p95: '19', p99: '25' },
      losing_streak: { p5: '4', p50: '7', p95: '12', p99: '14' },
      net_r: { p5: '-8', p50: '9', p95: '25', p99: '31' },
      negative_share: '0.21',
      block_trades: 5,
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
      in_blocks: null,
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
      in_blocks: null,
    }),
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  listMonteCarlos.mockResolvedValue([])
  createMonteCarlo.mockResolvedValue(resampled)
})

describe('MonteCarlo', () => {
  it('waits for the test to finish', async () => {
    renderWithProviders(<MonteCarlo sweepId="t1" settled={false} />)

    expect(await screen.findByText(/still running/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('sends the paths, and the seed only when one was typed', async () => {
    renderWithProviders(<MonteCarlo sweepId="t1" settled />)

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

    fireEvent.change(screen.getByLabelText('Block (trades in a row)'), { target: { value: '8' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resample' }))
    await waitFor(() => {
      expect(createMonteCarlo).toHaveBeenLastCalledWith('t1', {
        paths: 2000,
        seed: 'mine',
        block_trades: 8,
      })
    })
  })

  it('refuses a block outside 2 to 50 trades', async () => {
    renderWithProviders(<MonteCarlo sweepId="t1" settled />)

    fireEvent.change(await screen.findByLabelText('Block (trades in a row)'), {
      target: { value: '1' },
    })

    expect(screen.getByText(/A block of 2 to 50 trades/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('resamples the top of a ranking by the measure the screen ranks by (01/10)', async () => {
    renderWithProviders(<MonteCarlo sweepId="s1" settled rankBy="net_r" />)

    expect(
      await screen.findByRole('heading', { name: 'Resample the ranking (Monte Carlo)' }),
    ).toBeInTheDocument()
    expect(screen.getByText(/by net r as ranked below/)).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('Runs per entry'), { target: { value: '5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Resample' }))
    await waitFor(() => {
      expect(createMonteCarlo).toHaveBeenCalledWith('s1', {
        paths: 1000,
        ranking: { rank_by: 'net_r', top_n: 5 },
      })
    })

    fireEvent.change(screen.getByLabelText('Runs per entry'), { target: { value: '21' } })
    expect(screen.getByText('Between 1 and 20 runs per entry.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('waits for a ranking still moving', async () => {
    renderWithProviders(<MonteCarlo sweepId="s1" settled={false} rankBy="return" />)

    expect(await screen.findByText(/sweep is still running/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('refuses a path count outside 100 to 5000', async () => {
    renderWithProviders(<MonteCarlo sweepId="t1" settled />)

    fireEvent.change(await screen.findByLabelText('Paths per point'), { target: { value: '50' } })

    expect(screen.getByText('Between 100 and 5000 paths.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Resample' })).toBeDisabled()
  })

  it('shows what happened beside the median and the 95% to be ready for', async () => {
    listMonteCarlos.mockResolvedValue([resampled])
    renderWithProviders(<MonteCarlo sweepId="t1" settled />)

    const article = await screen.findByRole('article', { name: /1000 paths per point · seed abc/ })
    expect(within(article).getByText(/fell 6\.5 R · 5 losses in a row/)).toBeInTheDocument()
    expect(within(article).getByText('fell 8.3 R')).toBeInTheDocument()
    expect(within(article).getByText('95%: 17.5 R')).toBeInTheDocument()
    expect(within(article).getByText(/^6 losses in a row · 95%: 10$/)).toBeInTheDocument()
    expect(within(article).getByText('ends negative 18%')).toBeInTheDocument()
    expect(within(article).getByText('12 trades — too few to resample')).toBeInTheDocument()
    expect(within(article).getByText('no trades kept')).toBeInTheDocument()
  })

  it('shows trade by trade beside in blocks, and says when the losses come in runs', async () => {
    const runsOfLosses = point({
      run_id: 'r4',
      label: 'H4 · regime',
      in_blocks: {
        paths: 1000,
        trades: 120,
        drawdown_r: { p5: '9', p50: '20', p95: '31', p99: '40' },
        losing_streak: { p5: '6', p50: '11', p95: '18', p99: '22' },
        net_r: { p5: '-20', p50: '9', p95: '30', p99: '36' },
        negative_share: '0.4',
        block_trades: 5,
      },
    })
    listMonteCarlos.mockResolvedValue([
      {
        ...resampled,
        ranking: { rank_by: 'return', top_n: 10 },
        points: [point({}), runsOfLosses],
      },
    ])
    renderWithProviders(<MonteCarlo sweepId="s1" settled rankBy="return" />)

    const article = await screen.findByRole('article', {
      name: /1000 paths per point · top 10 per entry by return · seed abc/,
    })
    expect(within(article).getByRole('columnheader', { name: 'Trade by trade' })).toBeInTheDocument()
    expect(
      within(article).getByRole('columnheader', { name: 'In blocks of N (each point’s own)' }),
    ).toBeInTheDocument()
    expect(within(article).getByText(/drawdown in blocks is much worse/)).toBeInTheDocument()
    const rows = within(article).getAllByRole('row').slice(1)
    // 19 R against 17.5 R: about the same — the losses do not cluster.
    expect(within(rows[0]!).getByText('95%: 19.0 R')).toBeInTheDocument()
    expect(within(rows[0]!).getByText('blocks of 5')).toBeInTheDocument()
    expect(within(rows[0]!).queryByText('Losses come in runs')).not.toBeInTheDocument()
    // 31 R against 17.5 R: the losses come in runs.
    expect(within(rows[1]!).getByText('95%: 31.0 R')).toBeInTheDocument()
    expect(within(rows[1]!).getByText('Losses come in runs')).toBeInTheDocument()
  })

  it('heads the block asked for, and reads a resampling kept before blocks', async () => {
    // A point kept before 01/10 carries no `in_blocks` key at all.
    const before: MonteCarloPoint = point({})
    delete before.in_blocks
    listMonteCarlos.mockResolvedValue([
      { ...resampled, id: 'm2', block_trades: 8, points: [point({})] },
      { ...resampled, points: [before] },
    ])
    renderWithProviders(<MonteCarlo sweepId="t1" settled />)

    const [asked, old] = await screen.findAllByRole('article')
    expect(within(asked!).getByRole('columnheader', { name: 'In blocks of 8' })).toBeInTheDocument()
    expect(within(old!).getByText('not drawn in blocks (before 01/10)')).toBeInTheDocument()
    expect(within(old!).getByText('95%: 17.5 R')).toBeInTheDocument()
  })
})
