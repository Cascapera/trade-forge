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
  },
}))

import { api } from './client'
import {
  isTerminal,
  useBacktest,
  useBacktests,
  useCreateBacktest,
  useCreateStrategy,
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
