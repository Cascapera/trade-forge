import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
import type { SweepOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { SweepWalkForwardLauncher } from './SweepWalkForwardLauncher'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: { ...actual.api, createSweepWalkForward: vi.fn(), listSweepWalkForwards: vi.fn() },
  }
})

const navigate = vi.fn()
vi.mock('react-router-dom', async (original) => ({
  ...(await original<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const createSweepWalkForward = vi.mocked(api.createSweepWalkForward)
const listSweepWalkForwards = vi.mocked(api.listSweepWalkForwards)

const sweep = {
  id: 'sweep-1',
  date_from: '2009-01-01T00:00:00Z',
  date_to: '2020-01-01T00:00:00Z',
  timeframes: ['H1', 'H4'],
  counts: { total: 1200, done: 1200, running: 0, queued: 0, failed: 0 },
  runs: [],
} as unknown as SweepOut

const USED = {
  message:
    'a test window of this walk-forward was already used by 1 earlier test of this sweep; the reserved window is ' +
    'used once — send retest: true to test it again knowingly',
  used_by: [
    {
      kind: 'holdout',
      id: 'test-0',
      test_id: 'test-0',
      fold: null,
      date_from: '2026-01-01T00:00:00Z',
      date_to: '2026-06-01T00:00:00Z',
      created_at: '2026-09-20T10:00:00Z',
    },
  ],
}

function field(label: string): HTMLInputElement {
  return screen.getByLabelText(label)
}

beforeEach(() => {
  vi.clearAllMocks()
  listSweepWalkForwards.mockResolvedValue([])
  createSweepWalkForward.mockResolvedValue({ id: 'wf-1', folds: 4, runs: 4800 })
})

describe('SweepWalkForwardLauncher', () => {
  it('says the folds and the cost, and waits for the cost to be confirmed', () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    // From 2009, 6 years of training, 2 of test, 4 folds, anchored.
    expect(screen.getByText(/fold 1: train 2009–2014 → test 2015–2016/)).toBeInTheDocument()
    expect(screen.getByText(/fold 4: train 2009–2020 → test 2021–2022/)).toBeInTheDocument()
    expect(screen.getByText(/about 4,800 training runs \(1200 × 4/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Walk forward' })).toBeDisabled()
  })

  it('rolls the training window when it is not anchored', () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    fireEvent.click(screen.getByLabelText(/Anchored/))

    expect(screen.getByText(/fold 2: train 2011–2016 → test 2017–2018/)).toBeInTheDocument()
  })

  it('sends the windows and the rule once confirmed, then opens the walk-forward', async () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    fireEvent.change(field('Folds'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('Ranked by'), { target: { value: 'recovery_r' } })
    fireEvent.click(screen.getByLabelText(/Queues about/))
    fireEvent.click(screen.getByRole('button', { name: 'Walk forward' }))

    await waitFor(() => {
      expect(createSweepWalkForward).toHaveBeenCalledWith('sweep-1', {
        start_year: 2009,
        train_years: 6,
        test_years: 2,
        folds: 3,
        anchored: true,
        top_n: 3,
        metric: 'recovery_r',
      })
    })
    expect(navigate).toHaveBeenCalledWith('/sweep-walkforwards/wf-1')
  })

  it('shows each chart’s ranking floor where its field is blank (01/10)', () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    expect(field('fewest trades on H1').placeholder).toBe('30')
    expect(field('fewest trades on H4').placeholder).toBe('20')
  })

  it('walks only the charts ticked, with their floors, and says the smaller cost', async () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    fireEvent.click(screen.getByRole('checkbox', { name: 'H4' }))
    fireEvent.change(field('fewest trades on H1'), { target: { value: '10' } })

    // Half the sweep's 1 200 runs, times four folds — an even split, and said so.
    expect(screen.getByLabelText(/Queues about 2,400 training runs/)).toBeInTheDocument()
    expect(screen.getByText(/on H1 only — an even share of the sweep's 1200/)).toBeInTheDocument()
    expect(field('fewest trades on H4')).toBeDisabled()
    fireEvent.click(screen.getByLabelText(/Queues about/))
    fireEvent.click(screen.getByRole('button', { name: 'Walk forward' }))

    await waitFor(() => {
      expect(createSweepWalkForward).toHaveBeenCalledWith(
        'sweep-1',
        expect.objectContaining({ timeframes: ['H1'], min_trades: { H1: 10 } }),
      )
    })
  })

  it('refuses no chart at all, and a floor that is not a whole number', () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    fireEvent.change(field('fewest trades on H1'), { target: { value: '0' } })
    expect(screen.getByText('A trade floor is a whole number of at least 1.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: 'H1' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'H4' }))
    expect(screen.getByText('Tick at least one chart.')).toBeInTheDocument()
  })

  it('refuses a fold that would test after this year, and says how many fit', () => {
    vi.useFakeTimers({ toFake: ['Date'] })
    vi.setSystemTime(new Date('2026-09-28T12:00:00Z'))
    try {
      renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

      // From 2009, six years of training and two of test: folds test 2015, 2017 … 2025, 2027.
      fireEvent.change(field('Folds'), { target: { value: '7' } })

      expect(
        screen.getByText('Fold 7 would test from 2027, in the future — at most 6 folds fit.'),
      ).toBeInTheDocument()
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not count the runs the sweep failed in the cost', () => {
    renderWithProviders(
      <SweepWalkForwardLauncher
        sweep={{ ...sweep, counts: { total: 1200, done: 1000, running: 0, queued: 0, failed: 200 } }}
      />,
    )

    expect(screen.getByLabelText(/Queues about 4,000 training runs \(1000 × 4/)).toBeInTheDocument()
  })

  it('takes the confirmation back when the windows change', () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)
    fireEvent.click(screen.getByLabelText(/Queues about/))

    fireEvent.change(field('Folds'), { target: { value: '5' } })

    expect(screen.getByText('Confirm the cost first.')).toBeInTheDocument()
  })

  it('names the earlier tests of used test years, and walks again as a retest once ticked (01/10)', async () => {
    createSweepWalkForward.mockRejectedValueOnce(new ApiError(409, USED))
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    fireEvent.click(screen.getByLabelText(/Queues about/))
    fireEvent.click(screen.getByRole('button', { name: 'Walk forward' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/already used by 1 earlier test/)
    expect(screen.getByRole('link', { name: 'Reserved-window test' })).toHaveAttribute(
      'href',
      '/sweeps/test-0',
    )
    fireEvent.click(screen.getByLabelText(/Test again \(retest\)/))
    fireEvent.click(screen.getByRole('button', { name: 'Walk forward' }))

    await waitFor(() => {
      expect(navigate).toHaveBeenCalledWith('/sweep-walkforwards/wf-1')
    })
    expect(createSweepWalkForward.mock.calls[0]?.[1]).not.toHaveProperty('retest')
    expect(createSweepWalkForward.mock.calls[1]?.[1]).toMatchObject({ retest: true })
  })

  it('walks by cut without a cost to confirm, by net R, over whole years of the sweep (01/10)', async () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)

    fireEvent.click(screen.getByLabelText(/By cut/))

    expect(screen.getByText(/ranks by\s+net R only/)).toBeInTheDocument()
    expect(screen.getByText('Ranked by net R')).toBeInTheDocument()
    expect(screen.queryByLabelText('Ranked by')).not.toBeInTheDocument()
    expect(screen.queryByLabelText(/Queues about/)).not.toBeInTheDocument()
    // Four folds of two test years reach 2022; the sweep holds 2009–2019 whole.
    expect(
      screen.getByText('By cut, every year must be a whole year of the sweep: 2009–2019.'),
    ).toBeInTheDocument()

    fireEvent.change(field('Folds'), { target: { value: '2' } })
    fireEvent.click(screen.getByRole('button', { name: 'Walk forward' }))

    await waitFor(() => {
      expect(createSweepWalkForward).toHaveBeenCalledWith(
        'sweep-1',
        expect.objectContaining({ folds: 2, metric: 'net_r', mode: 'cut' }),
      )
    })
  })

  it('counts a sweep that starts after 1 January from its first whole year', () => {
    renderWithProviders(
      <SweepWalkForwardLauncher sweep={{ ...sweep, date_from: '2009-03-01T00:00:00Z' }} />,
    )

    fireEvent.click(screen.getByLabelText(/By cut/))
    fireEvent.change(field('Folds'), { target: { value: '2' } })

    expect(
      screen.getByText('By cut, every year must be a whole year of the sweep: 2010–2019.'),
    ).toBeInTheDocument()
  })
})
