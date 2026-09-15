import { fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes, useParams } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
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

const COVERAGE_ERROR = '2 of these markets have no candles in this window; move the window or collect them first'

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

    // ⚠️ The **whole** form, window included, and the button seen live before the entry goes.
    // An earlier draft left the dates blank, so the disabled button below was `Choose a period.`
    // talking — the assertion passed with the uncountable-sweep refusal deleted.
    await fillIn()
    expect(await screen.findByText('1 backtest.')).toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })

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
    // And the launch is refused here, rather than sent off to come back as a 404.
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
  })
})

describe('the three refusals stay apart', () => {
  it('sends somebody to collect candles, not to edit a grid', async () => {
    // ⚠️ The coverage gap is the only one of the three whose fix is outside this screen, and the
    // two shapes of it are said differently: never collected is a backfill, collected for other
    // years is a window to move.
    //
    // ⚠️ The server's real shape: on a coverage gap `error` is filled **beside** `uncovered`
    // (`routers/sweeps.py`). A fixture with `error: null` here was a shape the server never
    // sends, and under it the guard that stops the same no being printed twice was invisible.
    previewSweep.mockResolvedValue(
      preview({
        runs: 0,
        uncovered: [
          { symbol: 'GBPUSD', timeframe: 'M15', covers: null },
          { symbol: 'EURUSD', timeframe: 'M15', covers: '2020-01-01 to 2021-01-01' },
        ],
        error: COVERAGE_ERROR,
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(await screen.findByText(/never collected/i)).toBeInTheDocument()
    expect(screen.getByText(/collected 2020-01-01 to 2021-01-01/i)).toBeInTheDocument()
    // Not described as a combination that cannot run: nothing about the entries is wrong.
    expect(screen.queryByText(/cannot run and will be left out/i)).not.toBeInTheDocument()
    // And said once, as the list — not a second time as the server's sentence under it.
    expect(screen.queryByText(COVERAGE_ERROR)).not.toBeInTheDocument()
  })

  it('refuses a sweep with nothing runnable, in the server’s words', async () => {
    // The third no, with no list beside it: every combination was refused, or the product is over
    // the cap after the refusals were subtracted. Only the server knows the net count, so only
    // its sentence can say this — and the button must follow it.
    previewSweep.mockResolvedValue(
      preview({ runs: 0, error: 'no combination in this sweep can run' }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(await screen.findByText('no combination in this sweep can run')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
  })

  it('does not refuse on a count the server does not use', async () => {
    // ⚠️ **The cap is on what will run, and only the server knows that number.** 3001 points over
    // one market and one chart, one of them refused by the DSL, is 3000 runs — exactly the cap,
    // and the server accepts it. A form that capped its own gross count would refuse a sweep the
    // server starts; this is the shape where the two counts disagree.
    listCatalog.mockResolvedValue({ total: 1, items: [entry('a', 'nine one plain', 3001)] })
    previewSweep.mockResolvedValue(
      preview({
        runs: 3000,
        entries: [
          {
            entry_id: 'a',
            name: 'nine one plain',
            points: 3001,
            refusals: [{ label: 'M15 · period=5', values: {}, reason: 'nope' }],
          },
        ],
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    // Seen landing before the silence is asserted: waiting only for the call asserts the silence
    // of a machine that has not yet had the chance to speak.
    await screen.findByText(/will be left out/i)
    expect(screen.queryByText(/over the 3000/)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
  })

  it('refuses a sweep over the cap in the server’s words', async () => {
    previewSweep.mockResolvedValue(
      preview({
        runs: 3001,
        error: 'this sweep expands to 3001 backtests, over the 3000 one sweep will run',
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(await screen.findByText(/this sweep expands to 3001 backtests/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
  })

  it('says the form’s own refusal beside the button', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    fireEvent.change(screen.getByLabelText('Initial capital'), { target: { value: '0' } })

    expect(await screen.findByText('Initial capital must be positive.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
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
    //
    // ⚠️ `error: null` beside a non-empty `uncovered` is **deliberately** a shape today's server
    // never sends. The screen blocks on the list itself, not only on the sentence, so a server
    // that stopped filling `error` on a coverage gap would still fail closed — this is the one
    // test that can see that guard, and it can only see it through the impossible shape.
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

describe('the answer on hand', () => {
  it('drops a verdict the moment the form stops asking it', async () => {
    // ⚠️ **The gap the debounce opens.** For 400ms after an edit the query is still keyed on the
    // previous question, so what is in hand is a true verdict about a sweep nobody is proposing.
    // Asserted **synchronously** after the edit, on purpose: once the debounce fires the key
    // moves and the data goes undefined on its own, so a `waitFor` here would pass with the
    // `asked` comparison deleted — it would wait out the very window this test is about.
    previewSweep.mockResolvedValueOnce(
      preview({
        runs: 0,
        uncovered: [{ symbol: 'EURUSD', timeframe: 'M15', covers: null }],
        error: COVERAGE_ERROR,
      }),
    )
    previewSweep.mockReturnValue(new Promise<SweepPreview>(() => undefined))
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    expect(await screen.findByText(/never collected/i)).toBeInTheDocument()

    // The reader fixes the window — the gap was about the old one.
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2025-02-01' } })

    expect(screen.queryByText(/never collected/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
  })

  it('says the check failed rather than showing an all-clear', async () => {
    // ⚠️ A failed request used to fall through to the empty answer, which is exactly what "this
    // sweep is fine" looks like. The reason is the server's own, via `apiFailure` — here the
    // 422 a request with more than fifty entries gets, a limit this form does not apply.
    previewSweep.mockRejectedValue(new ApiError(422, 'entry_ids: at most 50 items'))
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(await screen.findByText(/entry_ids: at most 50 items/)).toBeInTheDocument()
    expect(screen.getByText(/still be checked when you press it/i)).toBeInTheDocument()
    // Not a gate: the launch is checked again by the server.
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
  })

  it('drops a failure the moment the form stops asking it', async () => {
    // The failure's own copy of the stale-verdict rule. A failed request carries no `asked` to
    // compare, so it is matched against the question the query is keyed on — and asserted
    // synchronously for the same reason as above: after the debounce the old error goes away
    // by itself, and waiting would pass with the comparison deleted.
    previewSweep.mockRejectedValueOnce(new ApiError(422, 'entry_ids: at most 50 items'))
    previewSweep.mockReturnValue(new Promise<SweepPreview>(() => undefined))
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    expect(await screen.findByText(/entry_ids: at most 50 items/)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2025-02-01' } })

    expect(screen.queryByText(/entry_ids: at most 50 items/)).not.toBeInTheDocument()
  })
})

/** Stands in for the sweep's screen, and says which sweep it was sent to. */
function SweepLanded(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  return <p>sweep screen for {id}</p>
}

describe('launching', () => {
  it('sends the axes it was given and opens the sweep it created', async () => {
    renderWithProviders(
      <Routes>
        <Route path="/" element={<LaunchSweep />} />
        <Route path="/sweeps/:id" element={<SweepLanded />} />
      </Routes>,
    )
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
    // The id the server handed back reaches the address. Asserted as text rather than as "some
    // sweep screen rendered", which a navigation carrying the wrong field would also satisfy.
    expect(await screen.findByText('sweep screen for sweep-1')).toBeInTheDocument()
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
