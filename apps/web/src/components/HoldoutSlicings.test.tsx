import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { SlicedPoint, SlicingOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { HoldoutSlicings } from './HoldoutSlicings'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, listSlicings: vi.fn(), createSlicing: vi.fn() } }
})

const listSlicings = vi.mocked(api.listSlicings)
const createSlicing = vi.mocked(api.createSlicing)

function point(over: Partial<SlicedPoint>): SlicedPoint {
  return {
    run_id: 'r1',
    entry_id: 'e1',
    entry_name: 'CHOCH',
    symbol: 'EURUSD',
    timeframe: 'M15',
    label: 'M15 · edge',
    trades_kept: true,
    unscored: 0,
    net_r: '4',
    slices: [],
    counted: 0,
    positive: 0,
    share: null,
    passed: false,
    ...over,
  }
}

const judged: SlicingOut = {
  id: 's1',
  sweep_id: 't1',
  mode: 'calendar',
  block_trades: null,
  pass_share: '0.7',
  created_at: '2026-09-25T14:00:00Z',
  groups: [
    { entry_id: 'e1', entry_name: 'CHOCH', timeframe: 'M15', points: 2, judged: 1, passed: 1 },
  ],
  points: [
    point({
      run_id: 'r1',
      slices: [
        {
          label: '2020',
          date_from: '2020-01-01T00:00:00Z',
          date_to: '2021-01-01T00:00:00Z',
          trades: 12,
          net_r: '3.25',
          counted: true,
        },
        {
          label: '2021',
          date_from: '2021-01-01T00:00:00Z',
          date_to: '2022-01-01T00:00:00Z',
          trades: 0,
          net_r: '0',
          counted: false,
        },
        {
          label: '2022',
          date_from: '2022-01-01T00:00:00Z',
          date_to: '2023-01-01T00:00:00Z',
          trades: 9,
          net_r: '0.75',
          counted: true,
        },
      ],
      counted: 2,
      positive: 2,
      share: '1',
      passed: true,
    }),
    point({ run_id: 'r2', label: 'M15 · lost', trades_kept: false, net_r: '0' }),
  ],
}

beforeEach(() => {
  vi.clearAllMocks()
  listSlicings.mockResolvedValue([])
  createSlicing.mockResolvedValue(judged)
})

describe('HoldoutSlicings', () => {
  it('waits for the test to finish before judging it', async () => {
    // A test still running would be judged on whichever runs happened to land first.
    renderWithProviders(<HoldoutSlicings sweepId="t1" settled={false} />)

    expect(await screen.findByText(/still running/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Judge' })).toBeDisabled()
  })

  it('cuts by calendar year with no block size, and the bar as a share', async () => {
    renderWithProviders(<HoldoutSlicings sweepId="t1" settled />)

    fireEvent.click(await screen.findByRole('button', { name: 'Judge' }))

    await waitFor(() => {
      expect(createSlicing).toHaveBeenCalledWith('t1', { mode: 'calendar', pass_share: '0.7' })
    })
  })

  it('cuts into blocks of the size asked, and refuses a block under five', async () => {
    renderWithProviders(<HoldoutSlicings sweepId="t1" settled />)
    fireEvent.click(await screen.findByLabelText('By blocks of trades'))

    fireEvent.change(screen.getByLabelText('Trades per block'), { target: { value: '4' } })
    expect(screen.getByText('A block holds at least 5 trades.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Judge' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Trades per block'), { target: { value: '25' } })
    fireEvent.change(screen.getByLabelText(/passes at/i), { target: { value: '60' } })
    fireEvent.click(screen.getByRole('button', { name: 'Judge' }))

    await waitFor(() => {
      expect(createSlicing).toHaveBeenCalledWith('t1', {
        mode: 'trades',
        block_trades: 25,
        pass_share: '0.6',
      })
    })
  })

  it('shows each kept judgement with its rule, its pieces in R and its verdict', async () => {
    listSlicings.mockResolvedValue([judged])
    renderWithProviders(<HoldoutSlicings sweepId="t1" settled />)

    const article = await screen.findByRole('article', { name: /cut by calendar year/i })
    expect(within(article).getByText(/passes at 70% of pieces positive/)).toBeInTheDocument()
    expect(within(article).getByText('2020 +3.3 R')).toBeInTheDocument()
    // ⚠️ The empty year is shown, and marked as not counted — never hidden, never a loss.
    expect(within(article).getByText('2021 0.0 R')).toHaveAttribute(
      'title',
      expect.stringContaining('not counted'),
    )
    expect(within(article).getByText('passed · 2/2')).toBeInTheDocument()
    expect(within(article).getByText('1 of 1 judged')).toBeInTheDocument()
    expect(within(article).getByText('(2 tested)')).toBeInTheDocument()
  })

  it('says a run with no trades kept was not judged, rather than calling it a failure', async () => {
    listSlicings.mockResolvedValue([judged])
    renderWithProviders(<HoldoutSlicings sweepId="t1" settled />)

    expect(await screen.findByText('no trades kept')).toBeInTheDocument()
    expect(screen.queryByText(/^failed/)).not.toBeInTheDocument()
  })
})
