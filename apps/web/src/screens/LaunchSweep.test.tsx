import { fireEvent, screen, waitFor } from '@testing-library/react'
import { Route, Routes, useParams } from 'react-router-dom'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
import type { CatalogEntry, PlannedCollection, SweepPreview } from '../api/types'
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
      planCollections: vi.fn(),
      searchSymbols: vi.fn(),
    },
  }
})

const listInstruments = vi.mocked(api.listInstruments)
const listCatalog = vi.mocked(api.listCatalog)
const createSweep = vi.mocked(api.createSweep)
const previewSweep = vi.mocked(api.previewSweep)
const planCollections = vi.mocked(api.planCollections)
const searchSymbols = vi.mocked(api.searchSymbols)

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

// The server's all-skipped sentence (PR-269): `_nothing_to_read`, in `coverage.describe`'s words.
const COVERAGE_ERROR =
  'no candles in this window for: GBPUSD M15 (never collected), EURUSD M15 (on disk: 2020-01-01 to 2021-01-01)'

function planned(symbol: string, timeframe: string): PlannedCollection {
  return {
    symbol,
    timeframe,
    covers: null,
    in_window: false,
    windows: [{ date_from: '2025-01-01T00:00:00Z', date_to: '2025-12-31T23:59:59.999999Z' }],
    at_broker: true,
    time: null,
  }
}

function preview(patch: Partial<SweepPreview> = {}): SweepPreview {
  return {
    runs: 2,
    documents: 2,
    entries: [],
    uncovered: [],
    backtest_time: null,
    error: null,
    ...patch,
  }
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
  // Nothing missing unless a test says otherwise: the click then launches as it always did.
  planCollections.mockResolvedValue([])
  createSweep.mockResolvedValue({ id: 'sweep-1', runs: 2, skipped: [] })
  // The broker offers one market nobody has collected, which is what the search is for.
  searchSymbols.mockResolvedValue({
    symbols: [
      {
        symbol: 'AUDUSD',
        description: 'Australian Dollar vs US Dollar',
        path: 'Forex/Majors/AUDUSD',
        digits: 5,
        visible: true,
        asset_class_from_path: 'forex',
        catalogued: false,
      },
    ],
    snapshot: null,
  })
})

describe('a market found in the broker list (PR-306)', () => {
  async function pickAudusd(): Promise<void> {
    fireEvent.change(screen.getByRole('combobox', { name: /find any market/i }), {
      target: { value: 'aud' },
    })
    fireEvent.mouseDown(await screen.findByRole('option', { name: /AUDUSD/ }))
  }

  it('can be chosen, and the launch says to collect it first rather than failing with a 422', async () => {
    /**
     * ⚠️ His ask of 24/09: the pickers showed only the catalogued four. Now any market the broker
     * offers can be found — and one never collected has no instrument, which the API refuses
     * outright, so the screen says what fixes it before the click, with the way there.
     */
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    await pickAudusd()

    expect(
      await screen.findByRole('button', { name: 'Remove AUDUSD, never collected' }),
    ).toBeInTheDocument()
    expect(
      screen.getByText(
        'AUDUSD has never been collected, so there are no candles to run on — collect it first.',
      ),
    ).toBeInTheDocument()
    expect(screen.getByRole('link', { name: /collect it first/i })).toHaveAttribute(
      'href',
      '/collect',
    )
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
  })

  it('stops refusing once the market is taken off again', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    await pickAudusd()

    fireEvent.click(await screen.findByRole('button', { name: 'Remove AUDUSD, never collected' }))

    expect(screen.queryByText(/never been collected/)).not.toBeInTheDocument()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })
  })
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

  it('says how long the runs would take beside the count, measured here', async () => {
    // His call (22/09): shown beside the count, never in place of it.
    previewSweep.mockResolvedValue(preview({ backtest_time: { seconds: 3 * 3600, based_on: 37 } }))
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(
      await screen.findByText(
        'about 3 h to run, one after another — measured on the last 37 finished runs.',
      ),
    ).toBeInTheDocument()
  })

  it('says there is no estimate yet when nothing has finished here', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(
      await screen.findByText(
        'No run has finished here yet, so there is no estimate of how long this takes.',
      ),
    ).toBeInTheDocument()
  })

  it('refuses a sweep with nothing runnable, in the server’s words', async () => {
    // The third no, with no list beside it: every combination was refused by the DSL. Only the
    // server knows the net count, so only its sentence can say this — and the button must follow
    // it.
    previewSweep.mockResolvedValue(
      preview({ runs: 0, error: 'no combination in this sweep can run' }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    expect(await screen.findByText('no combination in this sweep can run')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()
  })

  it('does not refuse on a count the server does not use', async () => {
    // ⚠️ **No cap, and a big sweep leaves the button alive** (18/09). 3001 points over one market
    // and one chart, one of them refused by the DSL, is 3000 runs — past what the old cap let a
    // form count without refusing. A form that capped its own gross count would refuse a sweep
    // the server starts.
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

  it('does not block a sweep whose every pair lacks data, because collecting is the fix', async () => {
    // ⚠️ Before PR-269 this blocked. Now the server's `error` on that shape is the all-skipped
    // sentence, and a disabled button would keep the reader from the prompt that collects.
    previewSweep.mockResolvedValue(
      preview({
        runs: 0,
        uncovered: [{ symbol: 'EURUSD', timeframe: 'M15', covers: null }],
        error: 'no candles in this window for: EURUSD M15 (never collected)',
      }),
    )
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    await screen.findByText(/never collected/i)
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
  })

  it('blocks again, without launching, when there is nothing to collect either', async () => {
    // ⚠️ Found reviewing this PR. A window wholly in the future is "all skipped" too, but the plan
    // has nothing to fetch: an empty plan used to launch at once, straight into the same refusal
    // as a 422, from a button the screen had just enabled with a promise to ask about collecting.
    const allSkipped = 'no candles in this window for: EURUSD M15 (never collected)'
    previewSweep.mockResolvedValue(
      preview({
        runs: 0,
        uncovered: [{ symbol: 'EURUSD', timeframe: 'M15', covers: null }],
        error: allSkipped,
      }),
    )
    planCollections.mockResolvedValue([])
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    await screen.findByText(/never collected/i)

    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    expect(await screen.findByText(/nothing to collect for this window either/i)).toBeInTheDocument()
    expect(createSweep).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: /run the sweep/i })).toBeDisabled()

    // Any edit asks again: the verdict was about that window.
    fireEvent.change(screen.getByLabelText('Initial capital'), { target: { value: '20000' } })
    expect(screen.queryByText(/nothing to collect for this window either/i)).not.toBeInTheDocument()
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
          // The market's own spread, started from the one the broker quoted (24/09).
          cost_model: {
            type: 'per_market',
            markets: { EURUSD: { spread_points: '8', commission_per_unit: '0' } },
          },
        }),
      )
    })
    // The id the server handed back reaches the address. Asserted as text rather than as "some
    // sweep screen rendered", which a navigation carrying the wrong field would also satisfy.
    expect(await screen.findByText('sweep screen for sweep-1')).toBeInTheDocument()
  })

  it('asks the plan about every market on every chart, and launches at once if nothing is missing', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    fireEvent.click(screen.getByLabelText('H4'))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })

    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    await waitFor(() => {
      expect(createSweep).toHaveBeenCalledWith(
        expect.objectContaining({ collect_missing: false }),
      )
    })
    expect(planCollections).toHaveBeenCalledWith(
      expect.objectContaining({ symbols: ['EURUSD'], timeframes: ['M15', 'H4'] }),
    )
  })

  it('asks before launching when a pair is missing, and collects on request', async () => {
    // ⚠️ A pair covered **in part** — the case the rehearsal's `uncovered` cannot see, and the
    // reason the click asks the plan. `in_window: true`: it has candles, just not all of them.
    planCollections.mockResolvedValue([
      { ...planned('EURUSD', 'M15'), covers: '2025-03-01 to 2025-12-31', in_window: true },
    ])
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })

    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    expect(await screen.findByRole('region', { name: 'missing data' })).toHaveTextContent(
      'EURUSD M15 — on disk 2025-03-01 to 2025-12-31; would fetch 2025',
    )
    expect(createSweep).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Collect and run' }))

    await waitFor(() => {
      expect(createSweep).toHaveBeenCalledWith(expect.objectContaining({ collect_missing: true }))
    })
  })

  it('offers running with what there is when one chart of a market has data', async () => {
    // ⚠️ By pair. EURUSD has no H4, and its M15 is not in the plan at all: keyed by symbol, the
    // empty H4 would read as EURUSD being empty and the run would be withdrawn.
    planCollections.mockResolvedValue([planned('EURUSD', 'H4')])
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    fireEvent.click(screen.getByLabelText('H4'))
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })

    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))
    fireEvent.click(await screen.findByRole('button', { name: 'Run with what there is' }))

    await waitFor(() => {
      expect(createSweep).toHaveBeenCalledWith(expect.objectContaining({ collect_missing: false }))
    })
  })

  it('withdraws running with what there is when every pair with a runnable point is empty', async () => {
    // ⚠️ The rehearsal's answer, not the plan's. The plan here mentions only H4, so by the plan
    // alone M15 would run — but the rehearsal says nothing runs (the DSL refuses M15 for this
    // entry, say), and "run" would be a button that 422s.
    previewSweep.mockResolvedValue(
      preview({
        runs: 0,
        uncovered: [{ symbol: 'EURUSD', timeframe: 'H4', covers: null }],
        error: 'no candles in this window for: EURUSD H4 (never collected)',
      }),
    )
    planCollections.mockResolvedValue([planned('EURUSD', 'H4')])
    renderWithProviders(<LaunchSweep />)
    await fillIn()
    fireEvent.click(screen.getByLabelText('H4'))
    await screen.findByText(/never collected/i)

    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    expect(await screen.findByText('Nothing would run until this is collected.')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Run with what there is' })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collect and run' })).toBeInTheDocument()
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


describe('LaunchSweep — costs per market', () => {
  it('starts each ticked market from its quoted spread and lets the reader correct it', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    const spread = await screen.findByLabelText('spread of EURUSD')
    expect(spread).toHaveValue('8')
    fireEvent.change(spread, { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('commission of EURUSD'), { target: { value: '3.5' } })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })
    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    await waitFor(() => {
      expect(createSweep).toHaveBeenCalledWith(
        expect.objectContaining({
          cost_model: {
            type: 'per_market',
            markets: { EURUSD: { spread_points: '5', commission_per_unit: '3.5' } },
          },
        }),
      )
    })
  })

  it('warns that every result is an upper bound when no market is charged', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    fireEvent.change(await screen.findByLabelText('spread of EURUSD'), { target: { value: '' } })

    expect(screen.getByText(/every result is an upper bound/)).toBeInTheDocument()
  })
})

describe('LaunchSweep — swap per market', () => {
  it('sends the buy and sell swap typed for a market, signed', async () => {
    renderWithProviders(<LaunchSweep />)
    await fillIn()

    fireEvent.change(await screen.findByLabelText('spread of EURUSD'), { target: { value: '5' } })
    fireEvent.change(screen.getByLabelText('buy swap of EURUSD'), { target: { value: '-5' } })
    fireEvent.change(screen.getByLabelText('sell swap of EURUSD'), { target: { value: '-5' } })
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /run the sweep/i })).toBeEnabled()
    })
    fireEvent.click(screen.getByRole('button', { name: /run the sweep/i }))

    await waitFor(() => {
      expect(createSweep).toHaveBeenCalledWith(
        expect.objectContaining({
          cost_model: {
            type: 'per_market',
            markets: {
              EURUSD: {
                spread_points: '5',
                commission_per_unit: '0',
                swap_long_per_lot: '-5',
                swap_short_per_lot: '-5',
              },
            },
          },
        }),
      )
    })
  })
})
