import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

vi.mock('./client', () => ({
  api: {
    listInstruments: vi.fn(),
    listBacktests: vi.fn(),
    getBacktest: vi.fn(),
    getTrades: vi.fn(),
    getEquity: vi.fn(),
    createStrategy: vi.fn(),
    createBacktest: vi.fn(),
    createBasket: vi.fn(),
    getBasket: vi.fn(),
    listStrategies: vi.fn(),
    updateStrategy: vi.fn(),
  },
}))

import type { BasketOut } from './types'
import { api } from './client'
import {
  isSettled,
  isTerminal,
  useBasket,
  useCreateBasket,
  useBacktest,
  useBacktests,
  useCreateBacktest,
  useCreateStrategy,
  useSaveStrategy,
  useEquity,
  useEquityCurves,
  useInstruments,
  useTrades,
} from './hooks'

const mockedApi = vi.mocked(api)

function makeWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return function Wrapper({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={client}>{children}</QueryClientProvider>
  }
}

afterEach(() => {
  vi.clearAllMocks()
})

describe('isTerminal', () => {
  it('is true only for done and failed', () => {
    expect(isTerminal('done')).toBe(true)
    expect(isTerminal('failed')).toBe(true)
    expect(isTerminal('running')).toBe(false)
    expect(isTerminal('queued')).toBe(false)
    expect(isTerminal(undefined)).toBe(false)
  })
})

describe('isSettled', () => {
  function basket(...statuses: string[]): BasketOut {
    return { runs: statuses.map((status) => ({ status })) } as BasketOut
  }

  it('waits for the last run, not the first', () => {
    // A basket has no status of its own — only N runs finishing at their own pace. Stopping the
    // poll when the first one lands would freeze the screen with the rest still queued.
    expect(isSettled(basket('done', 'queued'))).toBe(false)
    expect(isSettled(basket('done', 'running'))).toBe(false)
    expect(isSettled(basket('done', 'done'))).toBe(true)
  })

  it('counts a failed run as settled, because it is not coming back', () => {
    expect(isSettled(basket('done', 'failed'))).toBe(true)
  })

  it('keeps polling while the basket has not arrived at all', () => {
    expect(isSettled(undefined)).toBe(false)
  })

  it('calls a basket with no runs settled rather than polling it forever', () => {
    // The API refuses fewer than two symbols so this cannot be created, but `every` is true of an
    // empty list — reading that as unsettled would be a query polling a shape that cannot happen.
    expect(isSettled(basket())).toBe(true)
  })
})

describe('useBasket', () => {
  it('reads a basket and stops polling once every run has landed', async () => {
    mockedApi.getBasket.mockResolvedValue({
      id: 'k1',
      runs: [{ status: 'done' }, { status: 'failed' }],
    } as never)

    const { result } = renderHook(() => useBasket('k1'), { wrapper: makeWrapper() })

    await waitFor(() => {
      expect(result.current.data?.id).toBe('k1')
    })
    expect(mockedApi.getBasket).toHaveBeenCalledWith('k1')
  })

  it('does not fetch without an id', () => {
    renderHook(() => useBasket(undefined), { wrapper: makeWrapper() })
    expect(mockedApi.getBasket).not.toHaveBeenCalled()
  })
})

describe('useCreateBasket', () => {
  it('posts the basket and returns the runs it created', async () => {
    mockedApi.createBasket.mockResolvedValue({ id: 'k1', runs: [{}, {}] } as never)
    const { result } = renderHook(() => useCreateBasket(), { wrapper: makeWrapper() })

    act(() => {
      result.current.mutate({
        strategy_id: 's1',
        symbols: ['EURUSD', 'GBPUSD'],
        timeframe: 'H1',
        date_from: '2024-01-01T00:00:00Z',
        date_to: '2024-12-31T00:00:00Z',
        initial_capital: '10000',
      })
    })

    await waitFor(() => {
      expect(result.current.data?.id).toBe('k1')
    })
  })
})

describe('useInstruments', () => {
  it('loads the instruments', async () => {
    mockedApi.listInstruments.mockResolvedValue([{ symbol: 'EURUSD' }] as never)
    const { result } = renderHook(() => useInstruments(), { wrapper: makeWrapper() })
    await waitFor(() => {
      expect(result.current.data).toEqual([{ symbol: 'EURUSD' }])
    })
  })
})

describe('useBacktest', () => {
  it('does not fetch without an id', () => {
    const { result } = renderHook(() => useBacktest(undefined), { wrapper: makeWrapper() })
    expect(result.current.fetchStatus).toBe('idle')
    expect(mockedApi.getBacktest).not.toHaveBeenCalled()
  })

  it('fetches and stops polling once the run is done', async () => {
    mockedApi.getBacktest.mockResolvedValue({ status: 'done' } as never)
    const { result } = renderHook(() => useBacktest('b1'), { wrapper: makeWrapper() })
    await waitFor(() => {
      expect(result.current.data).toEqual({ status: 'done' })
    })
  })

  it('keeps polling while the run is still running', async () => {
    mockedApi.getBacktest.mockResolvedValue({ status: 'running' } as never)
    const { result } = renderHook(() => useBacktest('b2'), { wrapper: makeWrapper() })
    await waitFor(() => {
      expect(result.current.data).toEqual({ status: 'running' })
    })
  })
})

describe('useTrades and useEquity', () => {
  it('are idle until enabled, then fetch', async () => {
    const disabled = renderHook(() => useTrades('b1', false), { wrapper: makeWrapper() })
    expect(disabled.result.current.fetchStatus).toBe('idle')
    expect(mockedApi.getTrades).not.toHaveBeenCalled()

    mockedApi.getTrades.mockResolvedValue({ total: 0, items: [] } as never)
    mockedApi.getEquity.mockResolvedValue([] as never)
    const trades = renderHook(() => useTrades('b1', true), { wrapper: makeWrapper() })
    const equity = renderHook(() => useEquity('b1', true), { wrapper: makeWrapper() })
    await waitFor(() => {
      expect(trades.result.current.data).toEqual({ total: 0, items: [] })
      expect(equity.result.current.data).toEqual([])
    })
  })
})

describe('useBacktests', () => {
  it('passes the filters through to the API', async () => {
    mockedApi.listBacktests.mockResolvedValue({ total: 1, limit: 50, offset: 0, items: [] })
    const { result } = renderHook(() => useBacktests({ symbol: 'AAPL' }), {
      wrapper: makeWrapper(),
    })

    await waitFor(() => {
      expect(result.current.data?.total).toBe(1)
    })
    expect(mockedApi.listBacktests).toHaveBeenCalledWith({ symbol: 'AAPL' })
  })

  it('caches each set of filters separately', async () => {
    // Filters are part of the query key, so switching symbol is a different cached entry rather
    // than a refetch that blanks the table under the reader.
    mockedApi.listBacktests.mockResolvedValue({ total: 0, limit: 50, offset: 0, items: [] })
    const wrapper = makeWrapper()

    const first = renderHook(() => useBacktests({ symbol: 'AAPL' }), { wrapper })
    await waitFor(() => {
      expect(first.result.current.isSuccess).toBe(true)
    })
    const second = renderHook(() => useBacktests({ symbol: 'EURUSD' }), { wrapper })
    await waitFor(() => {
      expect(second.result.current.isSuccess).toBe(true)
    })

    expect(mockedApi.listBacktests).toHaveBeenCalledTimes(2)
  })
})

describe('useEquityCurves', () => {
  it('asks for nothing when nothing is selected', () => {
    const { result } = renderHook(() => useEquityCurves([]), { wrapper: makeWrapper() })
    expect(result.current.curves.size).toBe(0)
    expect(result.current.isPending).toBe(false)
    expect(mockedApi.getEquity).not.toHaveBeenCalled()
  })

  it('fetches one curve per run and keys them by id', async () => {
    // One request per run, not one for all of them: each gets its own cache entry, which is what
    // makes unticking and re-ticking a run free.
    mockedApi.getEquity.mockImplementation((id: string) =>
      Promise.resolve([{ time: '2024-08-01T13:00:00Z', equity: id === 'a' ? '11000' : '9000' }]),
    )

    const { result } = renderHook(() => useEquityCurves(['a', 'b']), { wrapper: makeWrapper() })

    await waitFor(() => {
      expect(result.current.curves.size).toBe(2)
    })
    expect(mockedApi.getEquity).toHaveBeenCalledTimes(2)
    expect(result.current.curves.get('a')?.[0]?.equity).toBe('11000')
    expect(result.current.curves.get('b')?.[0]?.equity).toBe('9000')
  })

  it('hands over the curves that arrived while others are still loading', async () => {
    // The chart grows a line as each curve lands. A run whose request has not resolved is simply
    // absent from the map rather than blocking the ones that did.
    let releaseB = (): void => undefined
    mockedApi.getEquity.mockImplementation((id: string) =>
      id === 'a'
        ? Promise.resolve([{ time: '2024-08-01T13:00:00Z', equity: '11000' }])
        : new Promise((resolve) => {
            releaseB = () => {
              resolve([{ time: '2024-08-01T13:00:00Z', equity: '9000' }])
            }
          }),
    )

    const { result } = renderHook(() => useEquityCurves(['a', 'b']), { wrapper: makeWrapper() })

    await waitFor(() => {
      expect(result.current.curves.has('a')).toBe(true)
    })
    expect(result.current.curves.has('b')).toBe(false)
    expect(result.current.isPending).toBe(true)

    act(() => {
      releaseB()
    })
    await waitFor(() => {
      expect(result.current.curves.has('b')).toBe(true)
    })
    expect(result.current.isPending).toBe(false)
  })

  it('reports an error without losing the curves that did load', async () => {
    mockedApi.getEquity.mockImplementation((id: string) =>
      id === 'a'
        ? Promise.resolve([{ time: '2024-08-01T13:00:00Z', equity: '11000' }])
        : Promise.reject(new Error('gone')),
    )

    const { result } = renderHook(() => useEquityCurves(['a', 'b']), { wrapper: makeWrapper() })

    await waitFor(() => {
      expect(result.current.isError).toBe(true)
    })
    expect(result.current.curves.get('a')).toHaveLength(1)
  })
})

describe('mutations', () => {
  it('create a strategy and a backtest', async () => {
    mockedApi.createStrategy.mockResolvedValue({ id: 's1', name: 'x' } as never)
    mockedApi.createBacktest.mockResolvedValue({ id: 'b1', status: 'queued' } as never)

    const strategy = renderHook(() => useCreateStrategy(), { wrapper: makeWrapper() })
    act(() => {
      strategy.result.current.mutate({ name: 'x' } as never)
    })
    await waitFor(() => {
      expect(strategy.result.current.data).toEqual({ id: 's1', name: 'x' })
    })

    const backtest = renderHook(() => useCreateBacktest(), { wrapper: makeWrapper() })
    act(() => {
      backtest.result.current.mutate({ symbol: 'EURUSD' } as never)
    })
    await waitFor(() => {
      expect(backtest.result.current.data).toEqual({ id: 'b1', status: 'queued' })
    })
  })
})

// --------------------------------------------------------------------------- #
// `useSaveStrategy` — the 409's actual cause, and its fix                       #
// --------------------------------------------------------------------------- #

function page(items: { id: string; name: string }[]) {
  return {
    total: items.length,
    limit: 50,
    offset: 0,
    items: items.map((item) => ({
      ...item,
      version: 1,
      schema_version: '1.0',
      setup: 'mme9_breakout',
      runs: 0,
      created_at: '2024-01-01T00:00:00Z',
    })),
  }
}

const document = { schema_version: '1.0', name: 'MME9' } as unknown as Parameters<
  ReturnType<typeof useSaveStrategy>['mutate']
>[0]['definition']

describe('useSaveStrategy', () => {
  it('creates a lineage when the name is free', async () => {
    vi.mocked(api.listStrategies).mockResolvedValue(page([]))
    vi.mocked(api.createStrategy).mockResolvedValue({ id: 'new' } as never)

    const { result } = renderHook(() => useSaveStrategy(), { wrapper: makeWrapper() })
    act(() => {
      result.current.mutate({ definition: document })
    })

    await waitFor(() => {
      expect(api.createStrategy).toHaveBeenCalledWith(document)
    })
    expect(api.updateStrategy).not.toHaveBeenCalled()
  })

  it('adds a version when the name is taken, whoever took it', async () => {
    // ⚠️ **This is the 409.** The old rule compared the typed name against the one *this tab*
    // had created, so a strategy saved in another tab — or before a reload — was invisible, and
    // saving under its name was a `POST` onto a name that already had a version 1. Nothing
    // about this test involves the session store, because nothing about the decision does any
    // more.
    vi.mocked(api.listStrategies).mockResolvedValue(page([{ id: 'from-another-tab', name: 'MME9' }]))
    vi.mocked(api.updateStrategy).mockResolvedValue({ id: 'v2' } as never)

    const { result } = renderHook(() => useSaveStrategy(), { wrapper: makeWrapper() })
    act(() => {
      result.current.mutate({ definition: document })
    })

    await waitFor(() => {
      expect(api.updateStrategy).toHaveBeenCalledWith('from-another-tab', document)
    })
    expect(api.createStrategy).not.toHaveBeenCalled()
  })

  it('asks about generated names too, because hidden is not free', async () => {
    // A grid's points are left out of the picker, but their names are taken in the database
    // just as hard. A lookup that inherited the picker's default would report a name as free
    // and put the 409 straight back for exactly those names.
    vi.mocked(api.listStrategies).mockResolvedValue(page([]))
    vi.mocked(api.createStrategy).mockResolvedValue({ id: 'new' } as never)

    const { result } = renderHook(() => useSaveStrategy(), { wrapper: makeWrapper() })
    act(() => {
      result.current.mutate({ definition: document })
    })

    await waitFor(() => {
      expect(api.listStrategies).toHaveBeenCalledWith({
        name: 'MME9',
        include_generated: true,
      })
    })
  })
})
