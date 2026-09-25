import { fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes, useParams } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
import type { SweepOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { HoldoutLauncher } from './HoldoutLauncher'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, createHoldout: vi.fn() } }
})

const createHoldout = vi.mocked(api.createHoldout)

const SWEEP: SweepOut = {
  id: 'sweep-1',
  entry_ids: ['e1'],
  symbols: ['EURUSD'],
  timeframes: ['M15', 'D1'],
  date_from: '2020-01-01T00:00:00Z',
  date_to: '2026-01-01T00:00:00Z',
  initial_capital: '10000',
  created_at: '2026-09-24T00:00:00Z',
  entries: [],
  runs: [],
  skipped: [],
  failed_collections: [],
}

function Landed(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  return <p>landed on {id}</p>
}

function render(): void {
  renderWithProviders(
    <Routes>
      <Route path="/" element={<HoldoutLauncher sweep={SWEEP} />} />
      <Route path="/sweeps/:id" element={<Landed />} />
    </Routes>,
  )
}

function field(label: RegExp | string): HTMLInputElement {
  return screen.getByLabelText(label)
}

describe('HoldoutLauncher', () => {
  beforeEach(() => {
    createHoldout.mockReset()
  })

  it('starts the window where the search ended', () => {
    render()

    expect(field('From').value).toBe('2026-01-01')
    expect(field('Best per chart').value).toBe('3')
  })

  it('sends the window, the rule and only the floors that were set, then opens the test', async () => {
    createHoldout.mockResolvedValue({ id: 'test-9', runs: 6, skipped: [] })
    render()

    fireEvent.change(field('To'), { target: { value: '2026-09-19' } })
    fireEvent.change(field('Best per chart'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Ranked by'), { target: { value: 'profit_factor' } })
    fireEvent.change(field('fewest trades on D1'), { target: { value: '10' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run the test' }))

    await waitFor(() => {
      expect(screen.getByText('landed on test-9')).toBeInTheDocument()
    })
    expect(createHoldout).toHaveBeenCalledWith('sweep-1', {
      date_from: '2026-01-01T00:00:00Z',
      date_to: '2026-09-19T00:00:00Z',
      top_n: 2,
      metric: 'profit_factor',
      min_trades: { D1: 10 },
    })
  })

  it('sends the limits on the risk in R only when they are set, the years as a share', async () => {
    createHoldout.mockResolvedValue({ id: 'test-9', runs: 6, skipped: [] })
    render()

    fireEvent.change(field('To'), { target: { value: '2026-09-19' } })
    fireEvent.change(screen.getByLabelText('Ranked by'), { target: { value: 'recovery_r' } })
    fireEvent.change(field('Deepest drawdown (R)'), { target: { value: '10' } })
    fireEvent.change(field('Years positive, at least (%)'), { target: { value: '60' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run the test' }))

    await waitFor(() => {
      expect(createHoldout).toHaveBeenCalledWith('sweep-1', {
        date_from: '2026-01-01T00:00:00Z',
        date_to: '2026-09-19T00:00:00Z',
        top_n: 3,
        metric: 'recovery_r',
        max_drawdown_r: '10',
        min_positive_year_share: '0.6',
        min_trades: {},
      })
    })
  })

  it('refuses a limit that is not a positive number, or a share above 100%', () => {
    render()

    fireEvent.change(field('Deepest drawdown (R)'), { target: { value: '-3' } })
    expect(screen.getByText(/a limit is a positive number/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run the test' })).toBeDisabled()

    fireEvent.change(field('Deepest drawdown (R)'), { target: { value: '' } })
    fireEvent.change(field('Years positive, at least (%)'), { target: { value: '120' } })
    expect(screen.getByRole('button', { name: 'Run the test' })).toBeDisabled()
  })

  it('refuses a window that shares bars with the one searched', () => {
    render()

    fireEvent.change(field('From'), { target: { value: '2025-06-01' } })

    expect(screen.getByRole('alert')).toHaveTextContent(/would not be out of sample/)
    expect(screen.getByRole('button', { name: 'Run the test' })).toBeDisabled()
  })

  it('refuses a window that ends before it starts', () => {
    render()

    fireEvent.change(field('To'), { target: { value: '2025-12-31' } })

    expect(screen.getByText(/has to end after it starts/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run the test' })).toBeDisabled()
  })

  it('refuses a floor that is not a whole number of at least one', () => {
    render()

    fireEvent.change(field('To'), { target: { value: '2026-09-19' } })
    fireEvent.change(field('fewest trades on M15'), { target: { value: '0' } })

    expect(screen.getByText(/whole number of at least 1/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run the test' })).toBeDisabled()
  })

  it('shows the server refusal in its own words', async () => {
    createHoldout.mockRejectedValue(
      new ApiError(422, 'no finished run of this sweep can be ranked'),
    )
    render()

    fireEvent.change(field('To'), { target: { value: '2026-09-19' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run the test' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/can be ranked/)
  })
})
