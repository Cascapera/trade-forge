import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

vi.mock('./client', () => ({
  api: {
    listLiveSetups: vi.fn(),
    watchBacktest: vi.fn(),
    registerLiveSetup: vi.fn(),
    editLiveSetup: vi.fn(),
    changeLiveSetup: vi.fn(),
    removeLiveSetup: vi.fn(),
    addLiveMarket: vi.fn(),
    changeLiveMarket: vi.fn(),
    removeLiveMarket: vi.fn(),
    listLiveSignals: vi.fn(),
  },
}))

import { api } from './client'
import {
  useChangeLive,
  useEditLiveSetup,
  useLiveSetups,
  useLiveSignals,
  useRegisterLiveSetup,
  useWatchBacktest,
} from './hooks'

function wrapper(): (props: { children: ReactNode }) => React.JSX.Element {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  })
  return ({ children }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

describe('live setup hooks', () => {
  it('lists the setups, and one setup’s signals only when asked for', async () => {
    vi.mocked(api.listLiveSetups).mockResolvedValue([])
    vi.mocked(api.listLiveSignals).mockResolvedValue([])

    const setups = renderHook(() => useLiveSetups(), { wrapper: wrapper() })
    const none = renderHook(() => useLiveSignals(undefined), {
      wrapper: wrapper(),
    })
    const one = renderHook(() => useLiveSignals('s1'), { wrapper: wrapper() })

    await waitFor(() => {
      expect(setups.result.current.isSuccess).toBe(true)
    })
    await waitFor(() => {
      expect(one.result.current.isSuccess).toBe(true)
    })
    expect(none.result.current.fetchStatus).toBe('idle')
    expect(api.listLiveSignals).toHaveBeenCalledWith('s1')
  })

  it('watches a run, registers, and edits a setup', async () => {
    const created = { id: 's1' } as never
    vi.mocked(api.watchBacktest).mockResolvedValue(created)
    vi.mocked(api.registerLiveSetup).mockResolvedValue(created)
    vi.mocked(api.editLiveSetup).mockResolvedValue(created)

    const watch = renderHook(() => useWatchBacktest(), { wrapper: wrapper() })
    const register = renderHook(() => useRegisterLiveSetup(), {
      wrapper: wrapper(),
    })
    const edit = renderHook(() => useEditLiveSetup(), { wrapper: wrapper() })
    await act(async () => {
      await watch.result.current.mutateAsync('b1')
      await register.result.current.mutateAsync({
        definition: {},
        timeframe: 'H1',
        instrument_ids: ['i1'],
      })
      await edit.result.current.mutateAsync({
        id: 's1',
        definition: { name: 'x' },
      })
    })

    expect(api.watchBacktest).toHaveBeenCalledWith('b1')
    expect(api.registerLiveSetup).toHaveBeenCalledWith({
      definition: {},
      timeframe: 'H1',
      instrument_ids: ['i1'],
    })
    expect(api.editLiveSetup).toHaveBeenCalledWith('s1', { name: 'x' })
  })

  it('routes every kind of change to its own call', async () => {
    for (const fn of [
      api.changeLiveSetup,
      api.removeLiveSetup,
      api.addLiveMarket,
      api.changeLiveMarket,
      api.removeLiveMarket,
    ]) {
      vi.mocked(fn).mockResolvedValue(null as never)
    }
    const change = renderHook(() => useChangeLive(), { wrapper: wrapper() })

    await act(async () => {
      await change.result.current.mutateAsync({
        kind: 'setup',
        id: 's',
        patch: { active: false },
      })
      await change.result.current.mutateAsync({
        kind: 'remove-setup',
        id: 's',
      })
      await change.result.current.mutateAsync({
        kind: 'add-market',
        id: 's',
        instrumentId: 'i',
      })
      await change.result.current.mutateAsync({
        kind: 'market',
        id: 's',
        instrumentId: 'i',
        active: true,
      })
      await change.result.current.mutateAsync({
        kind: 'remove-market',
        id: 's',
        instrumentId: 'i',
      })
    })

    expect(api.changeLiveSetup).toHaveBeenCalledWith('s', { active: false })
    expect(api.removeLiveSetup).toHaveBeenCalledWith('s')
    expect(api.addLiveMarket).toHaveBeenCalledWith('s', 'i')
    expect(api.changeLiveMarket).toHaveBeenCalledWith('s', 'i', true)
    expect(api.removeLiveMarket).toHaveBeenCalledWith('s', 'i')
  })
})
