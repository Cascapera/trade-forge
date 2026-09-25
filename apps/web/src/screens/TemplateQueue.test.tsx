import { fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { Instrument, SweepTemplateOut, TemplateItem } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { TemplateQueue } from './TemplateQueue'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: {
      ...actual.api,
      getSweepTemplate: vi.fn(),
      listInstruments: vi.fn(),
      queueMarkets: vi.fn(),
      pauseTemplate: vi.fn(),
      resumeTemplate: vi.fn(),
      removeTemplateItem: vi.fn(),
      combineSweeps: vi.fn(),
    },
  }
})

const navigate = vi.fn()
vi.mock('react-router-dom', async (original) => ({
  ...(await original<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const mocked = vi.mocked(api)

function item(over: Partial<TemplateItem>): TemplateItem {
  return {
    id: 'i1',
    symbol: 'EURUSD',
    cost_model: { spread_points: '9', commission_per_unit: '0' },
    position: 0,
    status: 'launched',
    sweep_id: 's1',
    error: null,
    runs: 360,
    done: 360,
    failed: 0,
    finished: true,
    ...over,
  }
}

const template: SweepTemplateOut = {
  id: 't1',
  name: 'CHOCH H1+H4',
  entry_ids: ['e1'],
  entry_names: ['CHOCH COMPLETO'],
  timeframes: ['H1', 'H4'],
  date_from: '2009-01-01T00:00:00Z',
  date_to: '2020-01-01T00:00:00Z',
  initial_capital: '10000',
  paused: false,
  created_at: '2026-09-26T10:00:00Z',
  items: [
    item({}),
    item({ id: 'i2', symbol: 'GBPUSD', sweep_id: 's2', position: 1 }),
    item({ id: 'i3', symbol: 'USDJPY', sweep_id: null, status: 'waiting', runs: 0, done: 0, finished: false, position: 2 }),
  ],
}

const instruments = [
  { id: 'a', symbol: 'AUDUSD', default_spread_points: '10.0000000000' },
  { id: 'b', symbol: 'XAUUSD', default_spread_points: null },
] as Instrument[]

function show(): void {
  renderWithProviders(
    <Routes>
      <Route path="/templates/:id" element={<TemplateQueue />} />
    </Routes>,
    '/templates/t1',
  )
}

beforeEach(() => {
  vi.clearAllMocks()
  mocked.getSweepTemplate.mockResolvedValue(template)
  mocked.listInstruments.mockResolvedValue(instruments)
  mocked.queueMarkets.mockResolvedValue(template)
  mocked.pauseTemplate.mockResolvedValue({ ...template, paused: true })
  mocked.removeTemplateItem.mockResolvedValue(template)
  mocked.combineSweeps.mockResolvedValue({ id: 'c1', runs: 720, skipped: [] })
})

describe('TemplateQueue', () => {
  it('shows each market, its state and its progress', async () => {
    show()

    expect(await screen.findByRole('link', { name: 'EURUSD' })).toHaveAttribute('href', '/sweeps/s1')
    expect(screen.getAllByText('finished')).toHaveLength(2)
    expect(screen.getByText('waiting')).toBeInTheDocument()
    expect(screen.getAllByText('360 of 360')).toHaveLength(2)
  })

  it('prefills the measured spread and sends only what was typed', async () => {
    show()
    fireEvent.click(await screen.findByLabelText('AUDUSD'))
    fireEvent.click(screen.getByLabelText('XAUUSD'))

    expect(screen.getByLabelText('spread of AUDUSD')).toHaveValue('10')
    expect(screen.getByLabelText('spread of XAUUSD')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('spread of XAUUSD'), { target: { value: '30' } })
    fireEvent.change(screen.getByLabelText('swap long of AUDUSD'), { target: { value: '-4.5' } })
    fireEvent.click(screen.getByRole('button', { name: 'Queue 2 markets' }))

    await waitFor(() => {
      expect(mocked.queueMarkets).toHaveBeenCalledWith('t1', [
        { symbol: 'AUDUSD', spread_points: '10', swap_long_per_lot: '-4.5' },
        { symbol: 'XAUUSD', spread_points: '30' },
      ])
    })
  })

  it('pauses the queue and removes a waiting market', async () => {
    show()

    fireEvent.click(await screen.findByRole('button', { name: 'Pause the queue' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove USDJPY from the queue' }))

    await waitFor(() => {
      expect(mocked.pauseTemplate).toHaveBeenCalledWith('t1')
      expect(mocked.removeTemplateItem).toHaveBeenCalledWith('t1', 'i3')
    })
  })

  it('reads the chosen finished markets together and opens the combination', async () => {
    show()

    fireEvent.click(await screen.findByRole('button', { name: /choose every finished one \(2\)/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Read 2 together' }))

    await waitFor(() => {
      expect(mocked.combineSweeps).toHaveBeenCalledWith(['s1', 's2'])
    })
    expect(navigate).toHaveBeenCalledWith('/sweeps/c1')
  })

  it('offers no reading together for a market still running', async () => {
    show()

    await screen.findByRole('link', { name: 'EURUSD' })
    expect(screen.queryByLabelText('read USDJPY together')).not.toBeInTheDocument()
  })
})
