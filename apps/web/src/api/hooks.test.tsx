import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

vi.mock('./client', () => ({
  api: {
    listInstruments: vi.fn(),
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
  useCreateBacktest,
  useCreateStrategy,
  useEquity,
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
