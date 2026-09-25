import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
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
  counts: { total: 1200, done: 1200, running: 0, queued: 0, failed: 0 },
  runs: [],
} as unknown as SweepOut

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

  it('takes the confirmation back when the windows change', () => {
    renderWithProviders(<SweepWalkForwardLauncher sweep={sweep} />)
    fireEvent.click(screen.getByLabelText(/Queues about/))

    fireEvent.change(field('Folds'), { target: { value: '8' } })

    expect(screen.getByText('Confirm the cost first.')).toBeInTheDocument()
  })
})
