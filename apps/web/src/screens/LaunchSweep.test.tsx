import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { CatalogEntry, SweepPreview } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { LaunchSweep } from './LaunchSweep'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: {
      ...actual.api,
      listInstruments: vi.fn(),
      listCatalog: vi.fn(),
      createSweep: vi.fn(),
      previewSweep: vi.fn(),
    },
  }
})

const listInstruments = vi.mocked(api.listInstruments)
const listCatalog = vi.mocked(api.listCatalog)
const createSweep = vi.mocked(api.createSweep)
const previewSweep = vi.mocked(api.previewSweep)

function instrument(symbol: string) {
  return {
    id: symbol,
    symbol,
    name: symbol,
    asset_class: 'forex',
    currency_quote: 'USD',
    currency_base: 'EUR',
    tick_size: '0.00001',
    tick_value: '1',
    contract_size: '100000',
    digits: 5,
    default_spread_points: '8',
  }
}

function entry(id: string, name: string, points: number): CatalogEntry {
  return {
    id,
    name,
    description: null,
    strategy_id: `strategy-${id}`,
    strategy_name: `MME9-${id}`,
    strategy_version: 1,
    setup: 'mme9_breakout',
    grid: {},
    points,
    created_at: '2026-09-01T00:00:00Z',
  }
}

function preview(patch: Partial<SweepPreview> = {}): SweepPreview {
  return { runs: 2, documents: 2, entries: [], uncovered: [], error: null, ...patch }
}

/**
 * Fill in every axis so the form is launchable, leaving one thing for the test to break.
 *
 * ⚠️ The market is matched on an **anchored** pattern, never on the bare symbol. `SymbolPicker`
 * gives each checkbox the accessible name `EURUSD, 8 ticks` — the measured cost is part of the
 * label on purpose, so somebody choosing markets by ear makes the same decision a sighted reader
 * makes by reading the column. An exact-string query finds nothing and reads like a missing
 * element rather than like a label that says more than the test assumed.
 */
async function fillIn(): Promise<void> {
  fireEvent.click(await screen.findByLabelText(/nine one plain/i))
  fireEvent.click(await screen.findByLabelText(/^EURUSD,/))
  fireEvent.click(screen.getByLabelText('M15'))
  fireEvent.change(screen.getByLabelText('From'), { target: { value: '2025-01-01' } })
  fireEvent.change(screen.getByLabelText('To'), { target: { value: '2025-06-01' } })
}

beforeEach(() => {
  vi.clearAllMocks()
  listInstruments.mockResolvedValue([instrument('EURUSD'), instrument('GBPUSD')])
  listCatalog.mockResolvedValue({
    total: 2,
    items: [entry('a', 'nine one plain', 1), entry('b', 'nine one swept', 3)],
  })
  previewSweep.mockResolvedValue(preview())
  createSweep.mockResolvedValue({ id: 'sweep-1', runs: 2 })
})

describe('the count on screen', () => {
  it('adds the entries and multiplies the charts and the markets', async () => {
    // ⚠️ Asymmetric on purpose: one entry of 1 point and one of 3, over two markets and one
    // chart, is (1 + 3) x 2 x 1 = 8. A screen that multiplied the entries would say 6, and a
    // fixture where both entries had the same size could not tell the two apart.
    renderWithProviders(<LaunchSweep />)

    fireEvent.click(await screen.findByLabelText(/nine one plain/i))
    fireEvent.click(await screen.findByLabelText(/nine one swept/i))
    fireEvent.click(await screen.findByLabelText(/^EURUSD,/))
    fireEvent.click(screen.getByLabelText(/^GBPUSD,/))
    fireEvent.click(screen.getByLabelText('M15'))

    expect(await screen.findByText('8 backtests.')).toBeInTheDocument()
  })

  it('says it could not count rather than showing a zero', async () => {
    // ⚠️ **The defect this screen is built to avoid.** An entry ticked and then removed from the
    // shelf leaves `runCount` unable to answer — and `0 backtests` would claim a measurement,
    // leaving no way to tell "nothing to run" from "I could not count".
    const { client } = renderWithProviders(<LaunchSweep />)

    fireEvent.click(await screen.findByLabelText(/nine one plain/i))
    fireEvent.click(await screen.findByLabelText(/^EURUSD,/))
    fireEvent.click(screen.getByLabelText('M15'))
    expect(await screen.findByText('1 backtest.')).toBeInTheDocument()

    // ⚠️ The shelf loses the entry from underneath the tick — and it has to be an actual
    // **refetch**, not merely a re-pointed mock. Re-pointing alone changes nothing on a mounted
    // screen, and a test written that way would pass against a `runCount` that had never learned
    // to say "I could not count": it would be asserting about a state the screen never entered.
    listCatalog.mockResolvedValue({ total: 1, items: [entry('b', 'nine one swept', 3)] })
    await client.invalidateQueries({ queryKey: ['catalog'] })

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent(/no longer on the shelf/i)
    })
    expect(screen.queryByText('0 backtests.')).not.toBeInTheDocument()
    // And the launch is refused, not waved through on the grounds that no number exceeded the cap.
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
  })
})

describe('the three refusals stay apart', () => {
  it('sends somebody to collect candles, not to edit a grid', async () => {
    // ⚠️ The coverage gap is the only one of the three whose fix is outside this screen, and the
    // two shapes of it are said differently: never collected is a backfill, collected for other
    // years is a window to move.
    previewSweep.mockResolvedValue(
      preview({
        runs: 0,
        uncovered: [
          { symbol: 'GBPUSD', timeframe: 'M15', covers: null },
          { symbol: 'EURUSD', timeframe: 'M15', covers: '2020-01-01 to 2021-01-01' },
        ],
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(await screen.findByText(/never collected/i)).toBeInTheDocument()
    expect(screen.getByText(/collected 2020-01-01 to 2021-01-01/i)).toBeInTheDocument()
    // Not described as a combination that cannot run: nothing about the entries is wrong.
    expect(screen.queryByText(/cannot run and will be left out/i)).not.toBeInTheDocument()
  })

  it('names the entry beside each refused combination', async () => {
    // ⚠️ A sweep holds several entries, and `htf must be coarser than H4` says nothing about
    // which shelf label caused it. Pooling the refusals without their labels is the failure.
    previewSweep.mockResolvedValue(
      preview({
        runs: 1,
        entries: [
          {
            entry_id: 'b',
            name: 'nine one swept',
            points: 3,
            refusals: [
              { label: 'M15 · period=5', values: {}, reason: 'htf must be coarser than M15' },
            ],
          },
        ],
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    const refusal = await screen.findByText(/htf must be coarser than M15/i)
    expect(refusal).toHaveTextContent('nine one swept')
    expect(refusal).toHaveTextContent('M15 · period=5')
  })

  it('leaves refused combinations out instead of blocking the launch', async () => {
    // Refused points are dropped by the server and the rest still run — so the button stays
    // live. Blocking here would refuse a sweep the server would happily accept.
    previewSweep.mockResolvedValue(
      preview({
        runs: 1,
        entries: [
          {
            entry_id: 'b',
            name: 'nine one swept',
            points: 3,
            refusals: [{ label: 'M15 · period=5', values: {}, reason: 'nope' }],
          },
        ],
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    await screen.findByText(/will be left out/i)
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
  })

  it('blocks the launch when a market has no candles at all', async () => {
    // The other side of the pair above: this one the server refuses whole, so the screen must
    // not offer a button that is going to 422.
    previewSweep.mockResolvedValue(
      preview({ runs: 0, uncovered: [{ symbol: 'EURUSD', timeframe: 'M15', covers: null }] }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    await screen.findByText(/never collected/i)
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
    })
  })
})

describe('launching', () => {
  it('sends the axes it was given and reports what was queued', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })

    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    await waitFor(() => {
      expect(createSweep).toHaveBeenCalledWith(
        expect.objectContaining({
          entry_ids: ['a'],
          symbols: ['EURUSD'],
          timeframes: ['M15'],
          date_from: '2025-01-01T00:00:00.000Z',
          date_to: '2025-06-01T00:00:00.000Z',
          cost_model: { type: 'none' },
        }),
      )
    })
    // ⚠️ It reports rather than navigating. `/sweeps/{id}` does not exist yet, and the router's
    // catch-all would have sent the reader to the builder with no word about what they started.
    expect(await screen.findByText(/Launched 2 backtests/i)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /run log/i })).toHaveAttribute('href', '/runs')
  })

  it('does not ask the server about a sweep with no window yet', async () => {
    // ⚠️ Coverage is per (symbol, timeframe) *inside a window*, so a question without dates
    // could not carry the one answer it exists to give.
    renderWithProviders(<LaunchSweep />)

    fireEvent.click(await screen.findByLabelText(/nine one plain/i))
    fireEvent.click(await screen.findByLabelText(/^EURUSD,/))
    fireEvent.click(screen.getByLabelText('M15'))

    await waitFor(() => {
      expect(screen.getByRole('status')).toHaveTextContent('1 backtest.')
    })
    expect(previewSweep).not.toHaveBeenCalled()
  })
})
