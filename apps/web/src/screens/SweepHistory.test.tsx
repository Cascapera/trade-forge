import { fireEvent, screen, within } from '@testing-library/react'

import type { SweepListItem, SweepRunCounts, SweepsPage } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  SWEEPS_PER_PAGE: 10,
  useSweeps: vi.fn(),
}))

import { useSweeps } from '../api/hooks'

import { SweepHistory } from './SweepHistory'

const mockedSweeps = vi.mocked(useSweeps)

const SETTLED: SweepRunCounts = { total: 4, done: 3, running: 0, queued: 0, failed: 1 }

function line(id: string, over: Partial<SweepListItem> = {}): SweepListItem {
  return {
    id,
    created_at: '2026-09-15T21:40:00Z',
    entries: [{ entry_id: 'e1', name: `entry of ${id}` }],
    symbols: ['EURUSD'],
    timeframes: ['M15'],
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2025-01-01T00:00:00Z',
    runs: SETTLED,
    ...over,
  }
}

/** Serve `total` sweeps, ten per page, answering whichever offset the screen asks for. */
function serve(total: number, pageItems?: (offset: number) => SweepListItem[]): void {
  mockedSweeps.mockImplementation((offset: number) => {
    const items =
      pageItems?.(offset) ??
      Array.from({ length: Math.max(0, Math.min(10, total - offset)) }, (_, i) =>
        line(`s${String(offset + i)}`),
      )
    const data: SweepsPage = { total, limit: 10, offset, items }
    return { isPending: false, isError: false, data } as unknown as ReturnType<typeof useSweeps>
  })
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('SweepHistory', () => {
  it('shows what each sweep asked, how far it got, and opens it on click', () => {
    serve(1, () => [
      line('abc', {
        entries: [
          { entry_id: 'e2', name: 'PC DE COMPRA CLASSICO' },
          { entry_id: 'e1', name: null },
        ],
        symbols: ['EURUSD', 'AUDUSD'],
        timeframes: ['H4', 'M15'],
        runs: { total: 500, done: 480, running: 1, queued: 17, failed: 2 },
      }),
    ])

    renderWithProviders(<SweepHistory />)

    const link = screen.getByRole('link', { name: /PC DE COMPRA CLASSICO/ })
    expect(link).toHaveAttribute('href', '/sweeps/abc')
    // The entries in the order they were asked for, and a removed one still takes its place.
    expect(within(link).getByText('PC DE COMPRA CLASSICO, removed entry')).toBeInTheDocument()
    expect(
      within(link).getByText('EURUSD, AUDUSD · H4, M15 · 2024-01-01 → 2025-01-01'),
    ).toBeInTheDocument()
    expect(
      within(link).getByText('480 of 500 done · 1 running · 17 queued · 2 failed'),
    ).toBeInTheDocument()
    expect(screen.getByText('1 sweep, newest first.')).toBeInTheDocument()
    // The launch instant in the reader's clock, to the minute. The day depends on the zone the
    // test runs in (21:40 UTC is already the 16th east of +02:20), the minute does not.
    expect(within(link).getByText(/^2026-09-1[56] \d{2}:\d{2}$/)).toBeInTheDocument()
  })

  it('says a finished sweep is finished rather than showing nothing', () => {
    serve(1, () => [line('done', { runs: { ...SETTLED, failed: 0, done: 4 } })])

    renderWithProviders(<SweepHistory />)

    expect(screen.getByText('4 of 4 done')).toBeInTheDocument()
  })

  it('invites a first launch when there is no history at all', () => {
    serve(0)

    renderWithProviders(<SweepHistory />)

    expect(screen.getByText(/No sweep has been launched yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Launch one' })).toHaveAttribute('href', '/sweep')
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('does not call a page past the end an empty history', () => {
    serve(12, () => [])

    renderWithProviders(<SweepHistory />)

    expect(screen.queryByText(/No sweep has been launched yet/)).not.toBeInTheDocument()
    expect(screen.getByText('Page 1 of 2')).toBeInTheDocument()
  })

  it('has no pager when ten sweeps fit on one page', () => {
    serve(10)

    renderWithProviders(<SweepHistory />)

    expect(screen.getAllByRole('link')).toHaveLength(10)
    expect(screen.queryByRole('navigation')).not.toBeInTheDocument()
  })

  it('pages back in time ten at a time, and forward again', () => {
    serve(25)

    renderWithProviders(<SweepHistory />)
    const pager = screen.getByRole('navigation', { name: 'Sweep history pages' })
    const newer = within(pager).getByRole('button', { name: '← Newer' })
    const older = within(pager).getByRole('button', { name: 'Older →' })

    expect(within(pager).getByText('Page 1 of 3')).toBeInTheDocument()
    expect(newer).toBeDisabled()
    expect(mockedSweeps).toHaveBeenLastCalledWith(0)

    fireEvent.click(older)
    expect(mockedSweeps).toHaveBeenLastCalledWith(10)
    fireEvent.click(older)
    expect(mockedSweeps).toHaveBeenLastCalledWith(20)
    // The last page holds the remaining five, and there is nothing older than it.
    expect(within(pager).getByText('Page 3 of 3')).toBeInTheDocument()
    expect(screen.getAllByRole('link')).toHaveLength(5)
    expect(older).toBeDisabled()

    fireEvent.click(newer)
    expect(mockedSweeps).toHaveBeenLastCalledWith(10)
    expect(within(pager).getByText('Page 2 of 3')).toBeInTheDocument()
  })

  it('numbers the page on screen, not the one still loading', () => {
    // While page 2 loads, React Query keeps page 1 up. The label must say 1 over those lines.
    mockedSweeps.mockImplementation(() => {
      const data: SweepsPage = { total: 25, limit: 10, offset: 0, items: [line('first')] }
      return { isPending: false, isError: false, data } as unknown as ReturnType<typeof useSweeps>
    })

    renderWithProviders(<SweepHistory />)
    fireEvent.click(screen.getByRole('button', { name: 'Older →' }))

    expect(mockedSweeps).toHaveBeenLastCalledWith(10)
    expect(screen.getByText('Page 1 of 3')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '← Newer' })).toBeDisabled()
    // A second click over page 1 still means page 2 — counted from the request it would skip to
    // page 3, a page the reader never saw the one before.
    fireEvent.click(screen.getByRole('button', { name: 'Older →' }))
    expect(mockedSweeps).toHaveBeenLastCalledWith(10)
  })

  it('says so while loading, and when the history cannot be read', () => {
    mockedSweeps.mockReturnValue({ isPending: true } as unknown as ReturnType<typeof useSweeps>)
    const { unmount } = renderWithProviders(<SweepHistory />)
    expect(screen.getByText('Loading the sweep history…')).toBeInTheDocument()
    unmount()

    mockedSweeps.mockReturnValue({ isPending: false, isError: true } as unknown as ReturnType<
      typeof useSweeps
    >)
    renderWithProviders(<SweepHistory />)
    expect(screen.getByText('Could not load the sweep history.')).toBeInTheDocument()
  })
})
