import { screen } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { SweepWalkForwardOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { SweepWalkForwardResult } from './SweepWalkForwardResult'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, getSweepWalkForward: vi.fn() } }
})

const getSweepWalkForward = vi.mocked(api.getSweepWalkForward)

const walk: SweepWalkForwardOut = {
  id: 'wf-1',
  parent_sweep_id: 'sweep-1',
  start_year: 2009,
  train_years: 6,
  test_years: 2,
  anchored: true,
  rule: { top_n: 3, metric: 'net_profit' },
  status: 'done',
  error: null,
  created_at: '2026-09-25T20:00:00Z',
  finished_at: '2026-09-26T02:00:00Z',
  folds: [
    {
      index: 0,
      train_from: '2009-01-01T00:00:00Z',
      train_to: '2015-01-01T00:00:00Z',
      test_from: '2015-01-01T00:00:00Z',
      test_to: '2017-01-01T00:00:00Z',
      train_sweep_id: 't0',
      test_sweep_id: 'x0',
      stage: 'done',
      error: null,
    },
    {
      index: 1,
      train_from: '2009-01-01T00:00:00Z',
      train_to: '2017-01-01T00:00:00Z',
      test_from: '2017-01-01T00:00:00Z',
      test_to: '2019-01-01T00:00:00Z',
      train_sweep_id: 't1',
      test_sweep_id: null,
      stage: 'failed',
      error: 'no finished run of this sweep can be ranked',
    },
  ],
  groups: [
    {
      entry_id: 'e1',
      entry_name: 'MM9',
      timeframe: 'H4',
      medians: ['0.034', null],
      folds: 1,
      positive_folds: 1,
      most_chosen: 'EURUSD · H4 · rr=2',
      most_chosen_folds: 1,
    },
  ],
}

function show(): void {
  renderWithProviders(
    <Routes>
      <Route path="/sweep-walkforwards/:id" element={<SweepWalkForwardResult />} />
    </Routes>,
    '/sweep-walkforwards/wf-1',
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  getSweepWalkForward.mockResolvedValue(walk)
})

describe('SweepWalkForwardResult', () => {
  it('lists each fold with its windows, its sweeps and why one was not tested', async () => {
    show()

    expect(await screen.findByRole('link', { name: '2015–2016' })).toHaveAttribute(
      'href',
      '/sweeps/x0',
    )
    expect(screen.getByRole('link', { name: '2009–2016' })).toHaveAttribute('href', '/sweeps/t1')
    expect(screen.getByText('not tested')).toBeInTheDocument()
    expect(screen.getByText(/can be ranked/)).toBeInTheDocument()
  })

  it('reads each entry and chart across the folds, with what kept being chosen', async () => {
    show()

    expect(await screen.findByText('3.4%')).toBeInTheDocument()
    expect(screen.getByText('1 of 1')).toBeInTheDocument()
    expect(screen.getByText('EURUSD · H4 · rr=2 (1 of 1)')).toBeInTheDocument()
  })
})
