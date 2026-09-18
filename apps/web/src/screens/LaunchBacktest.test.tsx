import { fireEvent, screen } from '@testing-library/react'

import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

const { mutate, history, documents, gate } = vi.hoisted(() => {
  interface Read {
    data: { definition: Record<string, unknown> } | undefined
    isError: boolean
  }
  // What `GET /strategies/{id}` answers, per id. ⚠️ The chart is **H4**, never H1: H1 was this
  // screen's old default, and a fixture on the default could not tell "read off the document"
  // from "the field nobody touched".
  const reads: Record<string, Read> = {
    s1: { data: { definition: { timeframe: 'H4' } }, isError: false },
    blank: { data: { definition: {} }, isError: false },
    loading: { data: undefined, isError: false },
    broken: { data: undefined, isError: true },
  }
  const plan = {
    answer: [] as unknown[],
    hold: false,
    pending: null as null | (() => void),
    asked: vi.fn(),
  }
  return {
    mutate: vi.fn(),
    history: vi.fn(),
    documents: reads,
    gate: { plan, collect: vi.fn(), collectMode: 'take' },
  }
})

function listed(id: string, name: string) {
  return { id, name, version: 1, schema_version: '1', setup: null, runs: 0, created_at: '' }
}

vi.mock('../api/hooks', () => ({
  useInstruments: () => ({ data: [{ id: 'i1', symbol: 'EURUSD' }] }),
  // The symbol field is a combobox over the broker's catalogue now, so it fetches. Stubbed
  // with an empty snapshot: these tests type a ticker rather than picking from the list, which
  // is the path that matters to them, and a real query here would be a network call in jsdom.
  useSymbolSearch: () => ({ data: { symbols: [], snapshot: null } }),
  useSyncSymbols: () => ({ mutate: () => undefined, isPending: false }),
  // The symbol field now also asks how much history the pair has. Stubbed as "never probed",
  // which is the state these tests are in and the one that renders the least — and **recorded**,
  // because which chart it is asked about is the second place the document's timeframe travels.
  useSymbolHistory: (symbol: string, timeframe: string | undefined) => {
    history(symbol, timeframe)
    return { data: undefined, error: null }
  },
  useProbeSymbol: () => ({ mutate: () => undefined, isPending: false, isSuccess: false }),
  useCreateBacktest: () => ({ mutate, isPending: false, isError: false }),
  usePlanCollections: () => ({
    mutate: (request: unknown, options: { onSuccess: (items: unknown[]) => void }) => {
      gate.plan.asked(request)
      // A fresh array per answer, as a real response is.
      const answer = [...gate.plan.answer]
      const respond = (): void => {
        options.onSuccess(answer)
      }
      if (gate.plan.hold) gate.plan.pending = respond
      else respond()
    },
    // Like React Query: a reset mutation never calls the `onSuccess` it was given.
    reset: () => {
      gate.plan.pending = null
    },
    isPending: false,
    isError: false,
  }),
  useCollectMissing: () => ({
    // `take`: each request is reported as taken and the sending ends, as the real hook does.
    // `hold`: still sending. `refuse`: the sending ends with nothing taken.
    mutate: (
      variables: { bodies: unknown[]; onQueued: (body: unknown) => void },
      options?: { onSettled?: () => void },
    ) => {
      gate.collect(variables.bodies)
      if (gate.collectMode === 'hold') return
      if (gate.collectMode === 'take') for (const body of variables.bodies) variables.onQueued(body)
      options?.onSettled?.()
    },
    reset: () => undefined,
    isPending: false,
    isSuccess: false,
    isError: false,
    data: undefined,
  }),
  // The server's list, which is what the picker offers — not this tab's memory.
  useStrategies: () => ({
    data: {
      items: [
        listed('s1', 'MME9 breakout'),
        listed('blank', 'No chart'),
        listed('loading', 'Slow'),
        listed('broken', 'Broken'),
      ],
    },
    isPending: false,
    isError: false,
  }),
  useStrategy: (id: string | undefined) =>
    (id === undefined ? undefined : documents[id]) ?? { data: undefined, isError: false },
}))

import { LaunchBacktest } from './LaunchBacktest'

afterEach(() => {
  vi.clearAllMocks()
  useSession.getState().clear()
  gate.plan.answer = []
  gate.plan.hold = false
  gate.plan.pending = null
  gate.collectMode = 'take'
})

function fillIn(): void {
  fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: 'EURUSD' } })
  fireEvent.change(screen.getByLabelText('from'), { target: { value: '2023-01-01' } })
  fireEvent.change(screen.getByLabelText('to'), { target: { value: '2023-06-01' } })
}

const runButton = () => screen.getByRole('button', { name: /run backtest/i })

describe('LaunchBacktest', () => {
  it('offers the saved strategies rather than asking to build one first', () => {
    // ⚠️ Nothing in this tab's session: the state after a reload. It used to answer "build and
    // run a strategy first" here, with every saved strategy one click away on the server.
    renderWithProviders(<LaunchBacktest />)
    fillIn()

    expect(screen.queryByText(/build and run a strategy first/i)).not.toBeInTheDocument()
    expect(screen.getByRole('option', { name: /MME9 breakout/ })).toBeInTheDocument()
    expect(screen.getByText(/before running: choose a strategy/i)).toBeInTheDocument()
    expect(runButton()).toBeDisabled()
  })

  it('runs a picked strategy on the chart its document was written for', () => {
    mutate.mockImplementation(
      (_payload: unknown, options: { onSuccess: (b: { id: string }) => void }) => {
        options.onSuccess({ id: 'b1' })
      },
    )
    renderWithProviders(<LaunchBacktest />)

    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 's1' } })
    fillIn()

    // No second place to say the chart: a field here could disagree with the document.
    expect(screen.queryByLabelText('timeframe')).not.toBeInTheDocument()
    expect(screen.getByText('H4')).toBeInTheDocument()
    // ⚠️ And the history note asks about the same chart. The text above is rendered by this
    // screen and cannot see whether the settings below were handed the chart, a stale H1, or
    // nothing — this is the only assertion that can.
    expect(history).toHaveBeenLastCalledWith('EURUSD', 'H4')

    fireEvent.click(runButton())

    expect(mutate).toHaveBeenCalledTimes(1)
    const payload = mutate.mock.calls[0]?.[0] as { strategy_id: string; timeframe: string }
    expect(payload.strategy_id).toBe('s1')
    expect(payload.timeframe).toBe('H4')
    // And the choice is remembered, so Study and Basket open on it too.
    expect(useSession.getState().strategyId).toBe('s1')
  })

  it.each([
    ['blank', /does not say which chart/],
    ['loading', /reading the strategy/],
    ['broken', /could not be read/],
  ])('says why a %s document cannot run yet, as itself', (id, reason) => {
    // ⚠️ All three leave the chart unknown, and only one of them is about the strategy. Pooling
    // them would call a slow network a broken document, or a failed read a missing field.
    useSession.getState().setStrategy(id, id)
    renderWithProviders(<LaunchBacktest />)
    fillIn()

    expect(screen.getByText(reason)).toBeInTheDocument()
    expect(runButton()).toBeDisabled()
  })

  it('enqueues a backtest with a spread cost model', () => {
    // The strategy saved in this session is still preselected — the second run this screen
    // began as.
    useSession.getState().setStrategy('s1', 'MME9 breakout')
    renderWithProviders(<LaunchBacktest />)

    fillIn()
    fireEvent.change(screen.getByLabelText('capital'), { target: { value: '5000' } })
    fireEvent.change(screen.getByLabelText('cost model'), { target: { value: 'spread' } })
    fireEvent.change(screen.getByLabelText('spread points'), { target: { value: '15' } })
    fireEvent.click(runButton())

    expect(mutate).toHaveBeenCalledTimes(1)
    const payload = mutate.mock.calls[0]?.[0] as { symbol: string; cost_model: unknown }
    expect(payload.symbol).toBe('EURUSD')
    expect(payload.cost_model).toEqual({ type: 'spread', spread_points: 15 })
  })
})

describe('LaunchBacktest when data is missing', () => {
  // Collected, and the window's tail is on disk: part of this run would read candles.
  const MISSING = [
    {
      symbol: 'EURUSD',
      timeframe: 'H4',
      covers: '2023-03-01 to 2026-09-10',
      in_window: true,
      windows: [{ date_from: '2023-01-01T00:00:00Z', date_to: '2023-12-31T23:59:59.999999Z' }],
    },
  ]

  function ready(): void {
    useSession.getState().setStrategy('s1', 'MME9 breakout')
    renderWithProviders(<LaunchBacktest />)
    fillIn()
  }

  it("asks the plan about the market, the document's chart and the window before launching", () => {
    ready()
    fireEvent.click(runButton())

    expect(gate.plan.asked).toHaveBeenCalledWith({
      symbols: ['EURUSD'],
      timeframes: ['H4'],
      date_from: '2023-01-01T00:00:00Z',
      date_to: '2023-06-01T00:00:00Z',
    })
    // Nothing missing: straight through, as before.
    expect(mutate).toHaveBeenCalledTimes(1)
  })

  it('says what is missing and waits instead of launching', () => {
    gate.plan.answer = MISSING
    ready()
    fireEvent.click(runButton())

    const prompt = screen.getByRole('region', { name: 'missing data' })
    expect(prompt).toHaveTextContent(
      'EURUSD H4 — on disk 2023-03-01 to 2026-09-10; would fetch 2023',
    )
    expect(mutate).not.toHaveBeenCalled()
  })

  it('runs with what there is when told to', () => {
    gate.plan.answer = MISSING
    ready()
    fireEvent.click(runButton())
    fireEvent.click(screen.getByRole('button', { name: 'Run with what there is' }))

    expect(mutate).toHaveBeenCalledTimes(1)
    expect((mutate.mock.calls[0]?.[0] as { symbol: string }).symbol).toBe('EURUSD')
  })


  it('closes the prompt when the form changes, since it answered the old form', () => {
    gate.plan.answer = MISSING
    ready()
    fireEvent.click(runButton())
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2023-09-01' } })

    expect(screen.queryByRole('region', { name: 'missing data' })).not.toBeInTheDocument()
  })

  it('drops an answer that arrives after the form changed', () => {
    // ⚠️ Nothing missing, so this answer would launch — with the dates as they were when the
    // question was asked, not as they are on screen now.
    gate.plan.hold = true
    ready()
    fireEvent.click(runButton())
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2023-09-01' } })
    gate.plan.pending?.()

    expect(mutate).not.toHaveBeenCalled()
  })

  it('launches on an answer that arrives with the form unchanged', () => {
    // The pair of the test above: the same late answer, no edit in between.
    gate.plan.hold = true
    ready()
    fireEvent.click(runButton())
    gate.plan.pending?.()

    expect(mutate).toHaveBeenCalledTimes(1)
  })


  it('does not offer to run a market that was never collected', () => {
    // ⚠️ His case on 17/09: an H1 strategy over a pair collected only at M15 and H4. The launch
    // answered "no candles in this window for EURUSD H1 (never collected)" — a refusal the screen
    // already knew about and should not have invited.
    gate.plan.answer = [
      {
        symbol: 'EURUSD',
        timeframe: 'H4',
        covers: null,
        in_window: false,
        windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
      },
    ]
    ready()
    fireEvent.click(runButton())

    expect(screen.queryByRole('button', { name: 'Run with what there is' })).not.toBeInTheDocument()
    expect(screen.getByText('Nothing would run until this is collected.')).toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()
  })

  it('collects and runs in one press, letting the server plan the windows', () => {
    // ⚠️ The screen sends no windows of its own: a client-chosen window could be part of a year,
    // and the collector replaces whole year partitions.
    gate.plan.answer = MISSING
    ready()
    fireEvent.click(runButton())
    fireEvent.click(screen.getByRole('button', { name: 'Collect and run' }))

    expect(gate.collect).not.toHaveBeenCalled()
    expect(mutate).toHaveBeenCalledTimes(1)
    expect((mutate.mock.calls[0]?.[0] as { collect_missing: boolean }).collect_missing).toBe(true)
  })

  it('runs with what there is without asking for a collection', () => {
    gate.plan.answer = MISSING
    ready()
    fireEvent.click(runButton())
    fireEvent.click(screen.getByRole('button', { name: 'Run with what there is' }))

    expect((mutate.mock.calls[0]?.[0] as { collect_missing: boolean }).collect_missing).toBe(false)
  })

  it('an ordinary launch asks for no collection either', () => {
    ready()
    fireEvent.click(runButton())

    expect((mutate.mock.calls[0]?.[0] as { collect_missing: boolean }).collect_missing).toBe(false)
  })
})
