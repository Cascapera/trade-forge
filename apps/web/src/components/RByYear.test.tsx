import { fireEvent, screen, waitFor, within } from '@testing-library/react'

import { ApiError, api } from '../api/client'
import type { Backtest } from '../api/types'
import { renderWithProviders } from '../test-utils'
import { RByYear } from './RByYear'

function run(over: Partial<Backtest>): Backtest {
  return {
    id: 'b1',
    strategy_id: 's1',
    instrument_id: 'i1',
    timeframe: 'H1',
    date_from: '2021-01-01T00:00:00Z',
    date_to: '2024-01-01T00:00:00Z',
    initial_capital: '10000',
    status: 'done',
    error: null,
    engine_version: '0.5.0',
    recorded: 'full',
    created_at: '2026-10-01T00:00:00Z',
    started_at: null,
    finished_at: null,
    candles_seen: null,
    first_candle: null,
    last_candle: null,
    metrics: null,
    targets: null,
    waiting_for: [],
    r_by_years: {
      '2021': { '2021': '2.5', '2022': '-1' },
      '2022': { '2022': '3' },
      '2023': { '2023': '-0.75' },
    },
    ...over,
  }
}

describe('RByYear', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('says a run recorded before 29/09 has no R by year, and asks for no cut', () => {
    const spy = vi.spyOn(api, 'getYearCut')
    renderWithProviders(<RByYear run={run({ r_by_years: null })} />)
    expect(screen.getByText(/recorded before 29\/09 and kept no R by year/)).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(spy).not.toHaveBeenCalled()
  })

  it('shows a row per year of entry, summed over its exits, and the total', () => {
    vi.spyOn(api, 'getYearCut').mockResolvedValue({
      first_year: 2021,
      last_year: 2023,
      net_r: '3.75',
      yearly_r: { '2021': '1.5', '2022': '3', '2023': '-0.75' },
    })
    renderWithProviders(<RByYear run={run({})} />)
    const table = screen.getByRole('table', { name: 'R by year' })
    const rows = within(table).getAllByRole('row')
    // Header, three years, total.
    expect(rows).toHaveLength(5)
    expect(within(rows[1]!).getByText(/\+1\.50 R/)).toBeInTheDocument()
    expect(within(rows[2]!).getByText('+3.00 R')).toBeInTheDocument()
    expect(within(rows[3]!).getByText('-0.75 R')).toBeInTheDocument()
    expect(within(rows[4]!).getByText('+3.75 R')).toBeInTheDocument()
  })

  it('marks only the year whose trades left later, with the split by year of exit', () => {
    vi.spyOn(api, 'getYearCut').mockReturnValue(new Promise(() => undefined))
    renderWithProviders(<RByYear run={run({})} />)
    const split = 'Entered in 2021 — left in 2021: +2.50 R · 2022: -1.00 R'
    expect(screen.getByTitle(split)).toBeInTheDocument()
    expect(screen.getAllByText('↷', { selector: '[title]' })).toHaveLength(1)
  })

  it('cuts the run to the years picked and shows the R of the cut', async () => {
    const spy = vi.spyOn(api, 'getYearCut').mockImplementation((_id, first, last) =>
      Promise.resolve({
        first_year: first,
        last_year: last,
        net_r: first === 2022 ? '2.25' : '4.75',
        yearly_r: first === 2022 ? { '2022': '3', '2023': '-0.75' } : {},
      }),
    )
    renderWithProviders(<RByYear run={run({})} />)
    // The whole window first: 2021 to 2023 (the window ends on 1 January 2024, exclusive).
    expect(spy).toHaveBeenCalledWith('b1', 2021, 2023)
    fireEvent.change(screen.getByLabelText('Cut from'), { target: { value: '2022' } })
    const status = screen.getByRole('status', { name: 'R of the cut' })
    await waitFor(() => {
      expect(within(status).getByText('+2.25 R')).toBeInTheDocument()
    })
    expect(spy).toHaveBeenLastCalledWith('b1', 2022, 2023)
    expect(within(status).getByText(/2022 \+3\.00 R · 2023 -0\.75 R/)).toBeInTheDocument()
  })

  it('says in a sentence why the server refused a cut (422)', async () => {
    vi.spyOn(api, 'getYearCut').mockRejectedValue(
      new ApiError(
        422,
        'the run turned 3 signal(s) away for a lot of zero: from there it traded differently from a run started later',
      ),
    )
    renderWithProviders(<RByYear run={run({})} />)
    const status = screen.getByRole('status', { name: 'R of the cut' })
    await waitFor(() => {
      expect(
        within(status).getByText(/This run cannot stand for 2021 to 2023 without running it again/),
      ).toBeInTheDocument()
    })
    expect(within(status).getByText(/turned 3 signal\(s\) away for a lot of zero/)).toBeInTheDocument()
    expect(within(status).queryByText(/Could not cut/)).not.toBeInTheDocument()
  })

  it('tells a failure apart from a refusal', async () => {
    vi.spyOn(api, 'getYearCut').mockRejectedValue(new ApiError(500, 'boom'))
    renderWithProviders(<RByYear run={run({})} />)
    await waitFor(() => {
      expect(screen.getByText(/Could not cut this run/)).toBeInTheDocument()
    })
  })
})
