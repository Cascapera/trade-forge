import { fireEvent, screen, within } from '@testing-library/react'

import type { DashboardSlice, SweepDashboard as Body } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useSweepDashboard: vi.fn(),
}))

import { useSweepDashboard } from '../api/hooks'

import { SweepDashboard } from './SweepDashboard'

const mocked = vi.mocked(useSweepDashboard)
const refetch = vi.fn()

function slice(over: Partial<DashboardSlice>): DashboardSlice {
  return {
    key: 'k',
    label: 'k',
    runs: 10,
    finished: 10,
    failed: 0,
    winners: 4,
    losers: 5,
    flat: 1,
    median_return: '-0.01',
    mean_return: '-0.02',
    best_return: '0.3',
    worst_return: '-0.5',
    median_drawdown: '0.1',
    worst_drawdown: '0.6',
    ...over,
  }
}

function body(over: Partial<Body> = {}): Body {
  return {
    launched_from: null,
    launched_to: null,
    totals: {
      sweeps: 2,
      entries: 2,
      symbols: ['AUDUSD', 'EURUSD'],
      timeframes: ['H4', 'M15'],
      runs: { total: 794, done: 784, running: 0, queued: 1, failed: 9 },
      measurements: 534,
      trades: 99737,
      runs_without_trades: 106,
      left_out: [],
    },
    overall: slice({
      key: 'all',
      label: 'All sweeps',
      runs: 794,
      finished: 784,
      winners: 260,
      losers: 404,
      flat: 120,
      median_return: '-0.0013485',
      best_return: '0.343534',
    }),
    win_rate: { median: '0.1494', runs: 678 },
    profit_factor: { median: '0.8757', runs: 664 },
    expectancy: { median: '-0.000555', runs: 678 },
    by_entry: [
      slice({ key: 'e1', label: 'PC DE COMPRA CLASSICO', median_return: '-0.000029' }),
      slice({ key: 'e2', label: null, runs: 54, finished: 44, median_return: '-0.23' }),
    ],
    by_symbol: [slice({ key: 'EURUSD', label: 'EURUSD', median_return: '0.02' })],
    by_timeframe: [slice({ key: 'H4', label: 'H4', median_return: null, finished: 0 })],
    sweeps: [
      {
        id: 'sw1',
        created_at: '2026-09-15T12:00:00Z',
        entry_names: ['PC DE COMPRA CLASSICO', null],
        runs: 500,
        finished: 500,
        winners: 169,
        median_return: '-0.0098',
        left_out: 0,
      },
    ],
    ...over,
  }
}

function serve(data: Body | undefined, state: Record<string, unknown> = {}): void {
  mocked.mockReturnValue({
    data,
    isPending: data === undefined,
    isError: false,
    isFetching: false,
    refetch,
    ...state,
  } as unknown as ReturnType<typeof useSweepDashboard>)
}

function table(name: string): HTMLElement {
  return screen.getByRole('table', { name })
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('SweepDashboard', () => {
  it('asks for all time until a day is picked', () => {
    serve(body())

    renderWithProviders(<SweepDashboard />)

    expect(mocked).toHaveBeenLastCalledWith({})
  })

  it('asks for the picked days as a window, and all time again on demand', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    fireEvent.change(screen.getByLabelText('Launched from'), { target: { value: '2026-09-15' } })
    fireEvent.change(screen.getByLabelText('Launched to (included)'), {
      target: { value: '2026-09-16' },
    })

    const window = mocked.mock.lastCall?.[0]
    expect(window).toHaveProperty('launched_from')
    expect(window).toHaveProperty('launched_to')

    fireEvent.click(screen.getByRole('button', { name: 'All time' }))
    expect(mocked).toHaveBeenLastCalledWith({})
    expect(screen.getByRole('button', { name: 'All time' })).toBeDisabled()
  })

  it('refuses a backwards period instead of asking the server', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    fireEvent.change(screen.getByLabelText('Launched from'), { target: { value: '2026-09-16' } })
    fireEvent.change(screen.getByLabelText('Launched to (included)'), {
      target: { value: '2026-09-15' },
    })

    expect(mocked).toHaveBeenLastCalledWith(null)
    expect(screen.getByRole('alert')).toHaveTextContent('The last day comes before the first one.')
    expect(screen.queryByText('Sweeps')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Refresh' })).toBeDisabled()
  })

  it('refreshes by hand', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))

    expect(refetch).toHaveBeenCalledTimes(1)
  })

  it('says it is refreshing while the numbers on screen are the old ones', () => {
    serve(body(), { isFetching: true })
    renderWithProviders(<SweepDashboard />)

    expect(screen.getByRole('button', { name: 'Refreshing…' })).toBeDisabled()
  })

  it('shows what ran', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    expect(screen.getByText('794')).toBeInTheDocument()
    expect(
      screen.getByText('784 of 794 done · 1 queued · 9 failed · 534 distinct measurements'),
    ).toBeInTheDocument()
    expect(screen.getByText('99,737')).toBeInTheDocument()
    expect(screen.getByText('106 finished runs never traded')).toBeInTheDocument()
    expect(screen.getByText('Markets: AUDUSD, EURUSD · Charts: H4, M15')).toBeInTheDocument()
  })

  it('names the pairs the sweeps left out, and says nothing when none were', () => {
    // His call (22/09). They have no runs, so no table can show them — the list is the only place.
    serve(
      body({
        totals: {
          ...body().totals,
          left_out: [
            { symbol: 'GBPUSD', timeframe: 'H1', sweeps: 2 },
            { symbol: 'USDJPY', timeframe: 'M15', sweeps: 1 },
          ],
        },
        sweeps: body().sweeps.map((one) => ({ ...one, left_out: 2 })),
      }),
    )
    renderWithProviders(<SweepDashboard />)

    const note = screen.getByRole('status', { name: 'left out' })
    expect(note).toHaveTextContent('Left out — no candles in the window')
    expect(note).toHaveTextContent('GBPUSD H1 — in 2 sweeps')
    expect(note).toHaveTextContent('USDJPY M15 — in 1 sweep')
    // And on the sweep that skipped them, in the timeline.
    expect(screen.getByText(/2 pairs left out/)).toBeInTheDocument()
  })

  it('says nothing about left-out pairs when every pair ran', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    expect(screen.queryByRole('status', { name: 'left out' })).not.toBeInTheDocument()
    expect(screen.queryByText(/left out/)).not.toBeInTheDocument()
  })

  it('splits the finished runs into winners, flat and losers, never by colour alone', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    // 260/784 = 33%, 120/784 = 15%, 404/784 = 52%.
    expect(screen.getByRole('img')).toHaveAccessibleName(
      'Winners 260 (33%), Flat 120 (15%), Losers 404 (52%)',
    )
  })

  it('draws no bar before anything has finished, and keeps the legend', () => {
    serve(body({ overall: slice({ finished: 0, winners: 0, losers: 0, flat: 0 }) }))
    renderWithProviders(<SweepDashboard />)

    expect(screen.queryByRole('img')).not.toBeInTheDocument()
    expect(screen.getAllByRole('listitem')[0]).toHaveTextContent('Winners 0—')
  })

  it('leads with the median and says over how many runs each ratio exists', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    expect(screen.getByText('-0.13%')).toBeInTheDocument()
    expect(screen.getByText('14.9%')).toBeInTheDocument()
    expect(screen.getByText('Runs that traded · over 678 runs')).toBeInTheDocument()
    expect(screen.getByText('0.88')).toBeInTheDocument()
    expect(screen.getByText('Runs with a losing trade · over 664 runs')).toBeInTheDocument()
    expect(screen.getByText('-0.056%')).toBeInTheDocument()
  })

  it('shows a return that rounds to zero as zero, not as a loss', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    const [pc] = within(table('By strategy, best median first')).getAllByRole('row').slice(1)
    const median = within(pc!).getAllByRole('cell')[4]!
    expect(median).toHaveTextContent(/^0\.00%$/)
    expect(median.firstElementChild).toHaveClass('text-slate-100')
  })

  it('tables every breakdown, with a removed entry and unfinished runs said as such', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    const strategies = table('By strategy, best median first')
    const removed = within(strategies).getByRole('rowheader', { name: 'Removed entry' })
    expect(removed.closest('tr')).toHaveTextContent('44 / 54')
    expect(within(table('By market')).getByRole('rowheader', { name: 'EURUSD' })).toBeInTheDocument()
    const h4 = within(table('By chart')).getByRole('rowheader', { name: 'H4' }).closest('tr')!
    // Nothing finished: a dash, never 0%.
    expect(within(h4).getAllByRole('cell')[4]).toHaveTextContent('—')
  })

  it('says what every figure is, before anyone reads the best one', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    expect(screen.getByText(/in-sample/).closest('p')).toHaveTextContent(
      'A run repeated by a later sweep is the same measurement and counts once.',
    )
    expect(
      screen.getByText(/A sweep's median pools its strategies/),
    ).toBeInTheDocument()
  })

  it('lists each sweep with a link to it and counts runs per day', () => {
    serve(body())
    renderWithProviders(<SweepDashboard />)

    const link = screen.getByRole('link', { name: /2026/ })
    expect(link).toHaveAttribute('href', '/sweeps/sw1')
    expect(link.closest('tr')).toHaveTextContent('PC DE COMPRA CLASSICO, removed entry')
    expect(link.closest('tr')).toHaveTextContent('169 34%')
    expect(screen.getByRole('rowheader', { name: '2026-09-15' }).closest('tr')).toHaveTextContent(
      '500',
    )
  })

  it('says so when nothing was launched in the period', () => {
    serve(body({ totals: { ...body().totals, sweeps: 0 } }))
    renderWithProviders(<SweepDashboard />)

    expect(screen.getByText('No sweep was launched in this period.')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it('says so while loading, and when the dashboard cannot be read', () => {
    serve(undefined)
    const { unmount } = renderWithProviders(<SweepDashboard />)
    expect(screen.getByText('Loading the dashboard…')).toBeInTheDocument()
    unmount()

    serve(undefined, { isPending: false, isError: true })
    renderWithProviders(<SweepDashboard />)
    expect(screen.getByText('Could not load the dashboard.')).toBeInTheDocument()
  })
})
