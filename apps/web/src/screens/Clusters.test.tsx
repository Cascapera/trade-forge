import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { ClusterOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { Clusters } from './Clusters'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, listClusters: vi.fn(), createCluster: vi.fn() } }
})

const navigate = vi.fn()
vi.mock('react-router-dom', async (original) => ({
  ...(await original<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const listClusters = vi.mocked(api.listClusters)
const createCluster = vi.mocked(api.createCluster)

const A = '11111111-1111-4111-8111-111111111111'
const B = '22222222-2222-4222-8222-222222222222'

function openedWith(members: { backtest_id: string; label: string }[]): void {
  renderWithProviders(<Clusters />, {
    pathname: '/clusters',
    state: { name: 'from a test', members },
  })
}

beforeEach(() => {
  vi.clearAllMocks()
  listClusters.mockResolvedValue([])
  createCluster.mockResolvedValue({ id: 'c-9' } as ClusterOut)
})

describe('Clusters', () => {
  it('opens with the members a test handed over, and sends only the risks that were typed', async () => {
    openedWith([
      { backtest_id: A, label: 'EURUSD H4 · edge' },
      { backtest_id: B, label: 'GBPUSD H4 · edge' },
    ])

    expect(screen.getByDisplayValue('from a test')).toBeInTheDocument()
    fireEvent.change(screen.getByLabelText('risk of GBPUSD H4 · edge'), {
      target: { value: '0.5' },
    })
    fireEvent.change(screen.getByLabelText('Open positions, at most'), { target: { value: '3' } })
    fireEvent.click(screen.getByRole('button', { name: 'Replay on one account' }))

    await waitFor(() => {
      expect(createCluster).toHaveBeenCalledWith({
        name: 'from a test',
        members: [{ backtest_id: A }, { backtest_id: B, risk_percent: '0.5' }],
        initial_capital: '10000',
        max_open_positions: 3,
        max_open_risk_percent: '5',
      })
    })
    expect(navigate).toHaveBeenCalledWith('/clusters/c-9')
  })

  it('refuses to build with no member, or with a risk that is not a percent', () => {
    openedWith([{ backtest_id: A, label: 'EURUSD H4 · edge' }])

    fireEvent.change(screen.getByLabelText('risk of EURUSD H4 · edge'), {
      target: { value: '150' },
    })
    expect(screen.getByText(/member risk is a percent/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Replay on one account' })).toBeDisabled()

    fireEvent.click(screen.getByRole('button', { name: 'Remove EURUSD H4 · edge' }))
    expect(screen.getByText('Add at least one member.')).toBeInTheDocument()
  })

  it('adds a run by its id, once', () => {
    openedWith([])

    fireEvent.change(screen.getByLabelText('Add a run by its id'), { target: { value: 'nope' } })
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Add a run by its id'), { target: { value: A } })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    expect(screen.getByRole('link', { name: A })).toHaveAttribute('href', `/results/${A}`)

    fireEvent.change(screen.getByLabelText('Add a run by its id'), { target: { value: A } })
    expect(screen.getByRole('button', { name: 'Add' })).toBeDisabled()
  })
})
