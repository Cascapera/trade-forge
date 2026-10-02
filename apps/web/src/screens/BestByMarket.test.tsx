import { fireEvent, screen, within } from '@testing-library/react'

import type { BestCellOut, BestMapOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useBestMap: vi.fn(),
  useBestCell: vi.fn(),
}))

import { useBestCell, useBestMap } from '../api/hooks'

import { BestByMarket } from './BestByMarket'

const map = vi.mocked(useBestMap)
const cell = vi.mocked(useBestCell)

const MAP: BestMapOut = {
  metric: 'recovery_r',
  every_run: false,
  engine_version: '0.5.0',
  cells: [
    {
      market: 'Forex',
      symbol: 'EURUSD',
      entry_id: 'e1',
      entry_name: 'MM9',
      timeframe: 'H1',
      value: '2.5',
      unbounded: false,
      run_id: 'r1',
      sweep_id: 's1',
      ranked: 120,
    },
    {
      market: 'Metals',
      symbol: 'GOLD',
      entry_id: 'e1',
      entry_name: 'MM9',
      timeframe: 'H1',
      value: '-1.2',
      unbounded: false,
      run_id: 'r2',
      sweep_id: 's1',
      ranked: 80,
    },
  ],
}

const TOP: BestCellOut = {
  symbol: 'EURUSD',
  entry_id: 'e1',
  entry_name: 'MM9',
  timeframe: 'H1',
  metric: 'recovery_r',
  every_run: false,
  ranked: 120,
  points: [
    {
      run_id: 'r1',
      sweep_id: 's1',
      strategy_id: 'st1',
      label: "H1 · side='short', rr=10",
      values: {},
      date_from: '2020-01-01T00:00:00Z',
      date_to: '2025-01-01T00:00:00Z',
      value: '2.5',
      unbounded: false,
      net_r: '25',
      net_r_per_year: '5',
      recovery_r: '2.5',
      positive_year_share: '0.8',
      max_drawdown_r: '10',
      total_trades: 140,
      profit_factor: '1.4',
      yearly_r: { '2020': '3', '2021': '-1' },
      tests: [
        {
          sweep_id: 't1',
          status: 'done',
          date_from: '2025-01-01T00:00:00Z',
          date_to: '2026-01-01T00:00:00Z',
          net_r: '-3.1',
          total_trades: 19,
        },
      ],
    },
  ],
}

function answer(data: unknown): never {
  return { data, isPending: false, isError: false, error: null } as never
}

beforeEach(() => {
  map.mockReturnValue(answer(MAP))
  cell.mockReturnValue(answer(TOP))
})

describe('BestByMarket', () => {
  it('draws one group per market and one cell per setup and chart, each with its number', () => {
    renderWithProviders(<BestByMarket />)

    expect(screen.getByRole('button', { name: /Forex \(1 market\)/ })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Metals \(1 market\)/ })).toBeInTheDocument()
    expect(
      screen.getByRole('button', { name: /EURUSD, MM9 H1: 2\.5, best of 120 runs/ }),
    ).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /GOLD, MM9 H1: -1\.2/ })).toBeInTheDocument()
  })

  it('asks the server again when another measure is chosen', () => {
    renderWithProviders(<BestByMarket />)

    fireEvent.change(screen.getByLabelText('Rank by'), { target: { value: 'net_r_per_year' } })

    expect(map).toHaveBeenLastCalledWith('net_r_per_year', false)
  })

  it('folds a market group', () => {
    renderWithProviders(<BestByMarket />)

    fireEvent.click(screen.getByRole('button', { name: /Metals \(1 market\)/ }))

    expect(screen.queryByRole('button', { name: /GOLD, MM9 H1/ })).not.toBeInTheDocument()
  })

  it('opens a cell’s top runs with what the reserved window found', () => {
    renderWithProviders(<BestByMarket />)

    fireEvent.click(screen.getByRole('button', { name: /EURUSD, MM9 H1/ }))

    const panel = screen.getByRole('complementary', { name: /Best runs of MM9 on EURUSD H1/ })
    expect(cell).toHaveBeenLastCalledWith({
      symbol: 'EURUSD',
      entryId: 'e1',
      entryName: 'MM9',
      timeframe: 'H1',
      metric: 'recovery_r',
      everyRun: false,
    })
    expect(within(panel).getByText(/120 runs ranked in this cell/)).toBeInTheDocument()
    expect(within(panel).getByText(/side='short', rr=10/)).toBeInTheDocument()
    expect(within(panel).getByText(/-3\.10 R over 19 trades/)).toBeInTheDocument()
  })

  it('says when nothing can be ranked yet', () => {
    map.mockReturnValue(answer({ ...MAP, cells: [] }))

    renderWithProviders(<BestByMarket />)

    expect(screen.getByText(/No run can be ranked yet/)).toBeInTheDocument()
  })
})
