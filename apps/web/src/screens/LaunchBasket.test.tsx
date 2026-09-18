import { fireEvent, screen } from '@testing-library/react'

import { ApiError } from '../api/client'
import type { CreateBasketRequest } from '../api/types'
import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

const { mutate, state, gate, navigate } = vi.hoisted(() => {
  // Annotated rather than asserted: the mutation's error is whatever the client threw, and the
  // test needs to put an `ApiError` in here later.
  const state: { isError: boolean; error: unknown } = { isError: false, error: null }
  const plan = {
    answer: [] as unknown[],
    hold: false,
    pending: null as null | (() => void),
    asked: vi.fn(),
  }
  return {
    mutate: vi.fn(),
    state,
    gate: { plan, collect: vi.fn(), collectMode: 'take' },
    navigate: vi.fn(),
  }
})

vi.mock('react-router-dom', async (original) => ({
  ...(await original<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

vi.mock('../api/hooks', () => ({
  useInstruments: () => ({
    data: [
      { id: 'i1', symbol: 'EURUSD', default_spread_points: '8.0000000000' },
      { id: 'i2', symbol: 'GBPUSD', default_spread_points: '9.0000000000' },
      { id: 'i3', symbol: 'US500', default_spread_points: null },
      { id: 'i4', symbol: 'XAUUSD', default_spread_points: null, asset_class: 'future' },
    ],
  }),
  useCreateBasket: () => ({ mutate, isPending: false, ...state }),
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
  // The strategy picker asks the server what exists. Empty here: these tests are about the
  // basket's own rules, and the picker has its own test.
  useStrategies: () => ({ data: { total: 0, limit: 200, offset: 0, items: [] }, isPending: false }),
}))

import { LaunchBasket } from './LaunchBasket'

function pick(name: string): void {
  fireEvent.click(screen.getByRole('checkbox', { name }))
}

beforeEach(() => {
  state.isError = false
  state.error = null
  gate.plan.answer = []
  gate.plan.hold = false
  gate.plan.pending = null
  gate.collectMode = 'take'
})

afterEach(() => {
  vi.clearAllMocks()
  useSession.getState().clear()
})

describe('LaunchBasket', () => {
  it('says a strategy has to be chosen rather than refusing to open at all', () => {
    // ⚠️ This screen used to render nothing but "build and run a strategy first" whenever the
    // store was empty — which a reload made true, for a database holding forty-five strategies.
    // Now the form is there and one field is missing, which is an ordinary thing for a form.
    renderWithProviders(<LaunchBasket />)

    expect(screen.getByText(/Before running: choose a strategy/)).toBeInTheDocument()
    expect(screen.getByLabelText(/Strategy/)).toBeInTheDocument()
    expect(screen.queryByText(/build and run a strategy first/i)).not.toBeInTheDocument()
  })

  it('refuses a basket of one market, saying why rather than disabling in silence', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')

    expect(screen.getByText(/one market is a backtest, not a basket/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run 1 market$/i })).toBeDisabled()
  })

  it('enqueues every chosen market in one call, with no cost model of its own', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    mutate.mockImplementation(
      (_payload: unknown, options: { onSuccess: (b: { id: string; runs: unknown[] }) => void }) => {
        options.onSuccess({ id: 'k1', runs: [{}, {}, {}] })
      },
    )
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')
    pick('GBPUSD, 9 ticks')
    pick('US500, no spread measured')
    fireEvent.change(screen.getByLabelText('timeframe'), { target: { value: 'H4' } })
    fireEvent.change(screen.getByLabelText('capital'), { target: { value: '5000' } })
    fireEvent.change(screen.getByLabelText('from'), { target: { value: '2023-01-01' } })
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2023-06-01' } })
    fireEvent.click(screen.getByRole('button', { name: /run 3 markets/i }))

    expect(mutate).toHaveBeenCalledTimes(1)
    const payload = mutate.mock.calls[0]?.[0] as CreateBasketRequest
    expect(payload.symbols).toEqual(['EURUSD', 'GBPUSD', 'US500'])
    expect(payload.timeframe).toBe('H4')
    expect(payload.initial_capital).toBe('5000')
    // ⚠️ One call for three markets, and no cost model in it: the server charges each instrument
    // its own measured spread. A figure sent from here would be applied across instruments whose
    // tick sizes differ by three orders of magnitude.
    expect(payload).not.toHaveProperty('cost_model')
  })

  it('remembers the basket it launched, so the nav can lead back to it', () => {
    useSession.getState().setStrategy('s1', 'Ponto Contínuo')
    mutate.mockImplementation(
      (_payload: unknown, options: { onSuccess: (b: { id: string; runs: unknown[] }) => void }) => {
        options.onSuccess({ id: 'k1', runs: [{}, {}] })
      },
    )
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')
    pick('GBPUSD, 9 ticks')
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    // There is no `GET /baskets`, so this thread is the only way back that is not a pasted URL.
    expect(useSession.getState().basketId).toBe('k1')
    expect(useSession.getState().basketLabel).toBe('Ponto Contínuo · 2 markets')
  })

  it('warns which chosen markets will run uncosted, by name', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')
    // Nothing to warn about yet: every chosen market has a measured spread.
    expect(screen.queryByText(/no measured spread/i)).not.toBeInTheDocument()

    pick('US500, no spread measured')

    // Named, not counted: the reader's next move is to go and catalogue that symbol, and "one of
    // them" does not say which.
    const warning = screen.getByRole('status')
    expect(warning).toHaveTextContent('US500')
    expect(warning).toHaveTextContent(/upper bound/i)
    expect(warning).not.toHaveTextContent('EURUSD')
  })

  it('reads as English when more than one market is uncosted', () => {
    // The singular and plural wordings are different sentences, not a suffix — "has"/"have",
    // "that run"/"those runs", "its result is an upper bound"/"their results are upper bounds".
    // A branch nobody renders is a branch nobody has read.
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)

    pick('US500, no spread measured')
    expect(screen.getByRole('status')).toHaveTextContent(
      /US500 has no measured spread, so that run will charge nothing/,
    )

    pick('XAUUSD, no spread measured')
    expect(screen.getByRole('status')).toHaveTextContent(
      /US500, XAUUSD have no measured spread, so those runs will charge nothing/,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/their results are upper bounds/)
  })

  it('shows every unknown symbol the API named, not a house message', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    state.isError = true
    state.error = new ApiError(422, 'unknown symbols: NOPE, ALSONOPE')
    renderWithProviders(<LaunchBasket />)

    expect(screen.getByText('unknown symbols: NOPE, ALSONOPE')).toBeInTheDocument()
  })
})

describe('LaunchBasket when data is missing', () => {
  function chosen(): void {
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)
    pick('EURUSD, 8 ticks')
    pick('GBPUSD, 9 ticks')
    fireEvent.change(screen.getByLabelText('timeframe'), { target: { value: 'H4' } })
  }

  it('asks the plan about every chosen market at the chosen chart', () => {
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    expect(gate.plan.asked).toHaveBeenCalledWith(
      expect.objectContaining({ symbols: ['EURUSD', 'GBPUSD'], timeframes: ['H4'] }),
    )
  })

  it('waits for an answer when something is missing, and runs with what there is on request', () => {
    gate.plan.answer = [
      {
        symbol: 'GBPUSD',
        timeframe: 'H4',
        covers: null,
        in_window: false,
        windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
      },
    ]
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    expect(screen.getByRole('region', { name: 'missing data' })).toHaveTextContent(
      'GBPUSD H4 — never collected; would fetch 2024',
    )
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(screen.getByRole('button', { name: 'Run with what there is' }))
    expect(mutate).toHaveBeenCalledTimes(1)
  })

  it('carries the markets the server left out to the result page', () => {
    // ⚠️ Only the launch's response knows them; the basket read back later does not.
    const skipped = [{ symbol: 'GBPUSD', timeframe: 'H4', covers: null }]
    mutate.mockImplementation(
      (
        _payload: unknown,
        options: { onSuccess: (b: { id: string; runs: unknown[]; skipped: unknown[] }) => void },
      ) => {
        options.onSuccess({ id: 'k9', runs: [{}], skipped })
      },
    )
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    expect(navigate).toHaveBeenCalledWith('/baskets/k9', { state: { skipped } })
  })

  it('queues a market the broker files outside the five classes with the class the catalogue holds', () => {
    // XAUUSD sits under Metals on this broker, a path the collection endpoint cannot classify —
    // without the catalogue's answer the whole request would be refused with a 409.
    gate.plan.answer = [
      {
        symbol: 'XAUUSD',
        timeframe: 'H1',
        covers: null,
        in_window: false,
        windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
      },
    ]
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)
    pick('EURUSD, 8 ticks')
    pick('XAUUSD, no spread measured')
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))

    expect(gate.collect).toHaveBeenCalledWith([
      expect.objectContaining({ items: [{ symbol: 'XAUUSD', asset_class: 'future' }] }),
    ])
  })

  it('still offers the run when only one market of the basket is empty', () => {
    // ⚠️ The basket's rule: the markets with data run, and the server names the one left out.
    gate.plan.answer = [
      {
        symbol: 'GBPUSD',
        timeframe: 'H4',
        covers: null,
        in_window: false,
        windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
      },
    ]
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    expect(screen.getByRole('button', { name: 'Run with what there is' })).toBeInTheDocument()
  })

  // ⚠️ Moved here from the backtest screen with PR-266: that screen now asks the server to
  // collect and run in one press, so queueing downloads from the prompt lives only here until
  // the basket's launch takes the same flag.
  const MISSING_GBP = [
    {
      symbol: 'GBPUSD',
      timeframe: 'H4',
      covers: null,
      in_window: false,
      windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
    },
  ]

  it('queues the collection when told to, and does not launch', () => {
    gate.plan.answer = MISSING_GBP
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))

    expect(gate.collect).toHaveBeenCalledWith([
      {
        items: [{ symbol: 'GBPUSD' }],
        rows: [{ timeframe: 'H4', ...MISSING_GBP[0]!.windows[0]! }],
      },
    ])
    expect(mutate).not.toHaveBeenCalled()
  })
  it('never queues the same window twice, however often it is pressed', () => {
    // ⚠️ The server does not merge identical requests: a second press would download the same
    // year again. Pressed twice before the screen re-renders, and once more after a new Run.
    gate.plan.answer = MISSING_GBP
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))
    const collect = screen.getByRole('button', { name: 'Collect what is missing' })
    fireEvent.click(collect)
    fireEvent.click(collect)
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    expect(gate.collect).toHaveBeenCalledTimes(1)
    expect(screen.getByRole('button', { name: 'Already queued' })).toBeDisabled()
  })
  it('sends nothing on a second press while the first is still sending', () => {
    gate.plan.answer = MISSING_GBP
    gate.collectMode = 'hold'
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))

    expect(gate.collect).toHaveBeenCalledTimes(1)
  })
  it('offers a refused window again', () => {
    // Nothing was taken, so nothing would be downloaded twice — and the person may have fixed
    // what the server refused.
    gate.plan.answer = MISSING_GBP
    gate.collectMode = 'refuse'
    chosen()
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))

    expect(gate.collect).toHaveBeenCalledTimes(2)
  })
})
