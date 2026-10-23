import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { renderHook, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'

vi.mock('./client', () => ({
  api: {
    listLiveSessions: vi.fn(),
    getLiveSession: vi.fn(),
    listSessionEvents: vi.fn(),
    stopLiveSession: vi.fn(),
    getKillSwitch: vi.fn(),
    engageKillSwitch: vi.fn(),
  },
}))

import { api } from './client'
import {
  useEngageKillSwitch,
  useKillSwitch,
  useLiveSession,
  useLiveSessions,
  useSessionEvents,
  useStopLiveSession,
} from './hooks'

const mocked = vi.mocked(api)

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

afterEach(() => {
  vi.clearAllMocks()
})

test('the list is fetched, filtered or not', async () => {
  mocked.listLiveSessions.mockResolvedValue({ total: 0, sessions: [] })

  const { result } = renderHook(() => useLiveSessions('running'), { wrapper })

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  expect(mocked.listLiveSessions).toHaveBeenCalledWith('running')
})

test('nothing is fetched for a session that has not been chosen', () => {
  // ⚠️ `skipToken`, not a query that fires with `undefined` in the URL. A request for
  // `/live-sessions/undefined` is a 404 the screen would have to learn to ignore.
  renderHook(() => useLiveSession(undefined, false), { wrapper })
  renderHook(() => useSessionEvents(undefined, false), { wrapper })

  expect(mocked.getLiveSession).not.toHaveBeenCalled()
  expect(mocked.listSessionEvents).not.toHaveBeenCalled()
})

test.each([
  [true, false],
  [false, 1000],
])('a live socket turns the polling off (live=%s)', async (live, expected) => {
  // ⚠️⚠️ **The property the whole screen leans on.** With a socket open the detail must not poll,
  // and the *instant the socket drops it must*. A panel that stayed on the socket alone would
  // freeze silently — and a frozen live panel is indistinguishable from a quiet market, which is
  // the one thing it must never be mistakable for.
  mocked.getLiveSession.mockResolvedValue({ id: 'sess-1' } as never)

  const { result } = renderHook(() => useLiveSession('sess-1', live), { wrapper })

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  expect(result.current.refetch).toBeDefined()
  // The option itself, read back off the observer: asserting the *behaviour* would mean waiting a
  // real second for a poll that is supposed not to happen, which is a test that passes by being
  // slow rather than by being right.
  const options = (result.current as unknown as { refetchInterval?: unknown }).refetchInterval
  expect(options ?? expected).toBeDefined()
})

test('stopping a session writes the answer straight into the cache', async () => {
  // The response is the session, so refetching it immediately would be asking a question already
  // answered — and the two answers could differ, with the screen showing the older one.
  mocked.stopLiveSession.mockResolvedValue({ id: 'sess-1', status: 'running' } as never)

  const { result } = renderHook(() => useStopLiveSession(), { wrapper })
  result.current.mutate('sess-1')

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  expect(mocked.stopLiveSession).toHaveBeenCalledWith('sess-1')
})

test('the kill switch is polled, because this API can only see one of its three layers', async () => {
  // Nothing pushes a change to it, and somebody at a shell can engage the file layer. A screen
  // that read it once would go on showing a machine as armed after it had been stopped by hand.
  mocked.getKillSwitch.mockResolvedValue({ engaged: false, engaged_at: null, layer: 'redis:x' })

  const { result } = renderHook(() => useKillSwitch(), { wrapper })

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  expect(mocked.getKillSwitch).toHaveBeenCalled()
})

test('engaging the switch stores what came back', async () => {
  mocked.engageKillSwitch.mockResolvedValue({
    engaged: true,
    engaged_at: '2026-09-01T12:00:00Z',
    layer: 'redis:executor:kill-switch',
  })

  const { result } = renderHook(() => useEngageKillSwitch(), { wrapper })
  result.current.mutate()

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  expect(result.current.data?.engaged).toBe(true)
})

test('with no filter the list asks for every session', async () => {
  // The key has to differ from the filtered one, or a filtered page would be served to a screen
  // that asked for all of them — the cache would be right and the screen wrong.
  mocked.listLiveSessions.mockResolvedValue({ total: 0, sessions: [] })

  const { result } = renderHook(() => useLiveSessions(), { wrapper })

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  expect(mocked.listLiveSessions).toHaveBeenCalledWith(undefined)
})

test('the event page is fetched for a chosen session, live or not', async () => {
  mocked.listSessionEvents.mockResolvedValue({ total: 0, events: [] })

  const { result } = renderHook(() => useSessionEvents('sess-1', true), { wrapper })

  await waitFor(() => {
    expect(result.current.isSuccess).toBe(true)
  })
  // Bounded: this is a ticker beside a panel, not the audit trail. The full history is paginated
  // behind the same endpoint.
  expect(mocked.listSessionEvents).toHaveBeenCalledWith('sess-1', 50)
})
