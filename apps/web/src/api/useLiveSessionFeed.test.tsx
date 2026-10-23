import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'

import type { LiveSession, SessionFrame } from './types'
import { useLiveSessionFeed } from './useLiveSessionFeed'

/**
 * A WebSocket that never leaves the process.
 *
 * jsdom ships no WebSocket, and a real one would make these tests about a server. What is under
 * test is the *lifecycle* — when the hook reconnects and when it must not — so the socket is a
 * handle the test can open, feed and close on demand.
 */
class FakeSocket {
  static opened: FakeSocket[] = []

  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  closedByClient = false

  constructor(readonly url: string) {
    FakeSocket.opened.push(this)
  }

  close(): void {
    this.closedByClient = true
  }

  connect(): void {
    act(() => {
      this.onopen?.()
    })
  }

  send(frame: SessionFrame): void {
    act(() => {
      this.onmessage?.({ data: JSON.stringify(frame) } as MessageEvent<string>)
    })
  }

  drop(): void {
    act(() => {
      this.onclose?.()
    })
  }
}

function session(over: Partial<LiveSession> = {}): LiveSession {
  return {
    id: 'sess-1',
    strategy_id: 's1',
    instrument_id: 'i1',
    symbol: 'EURUSD',
    timeframe: 'H1',
    mode: 'paper',
    status: 'running',
    initial_capital: '10000',
    engine_version: '0.1.0',
    warmup_bars: 120,
    started_at: '2026-09-01T10:00:00Z',
    stopped_at: null,
    heartbeat_at: '2026-09-01T12:00:00Z',
    last_bar_time: '2026-09-01T11:00:00Z',
    error: null,
    stale: false,
    silent_for_seconds: 4,
    ...over,
  }
}

const FILL: SessionFrame = {
  type: 'fill',
  client_id: 'zone-1',
  at: '2026-09-01T12:30:00Z',
  symbol: 'EURUSD',
  price: '1.10500',
  volume: '0.10',
  spread: '0.00002',
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>
}

beforeEach(() => {
  FakeSocket.opened = []
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.useFakeTimers()
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

test('it opens one socket for the session and reports the connection', () => {
  const { result } = renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })

  expect(FakeSocket.opened).toHaveLength(1)
  expect(FakeSocket.opened[0]?.url).toContain('/ws/live-sessions/sess-1')
  expect(result.current.connected).toBe(false)

  FakeSocket.opened[0]?.connect()

  expect(result.current.connected).toBe(true)
})

test('a fill reaches the ticker', () => {
  const { result } = renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })
  FakeSocket.opened[0]?.connect()

  FakeSocket.opened[0]?.send(FILL)

  expect(result.current.events).toHaveLength(1)
  expect(result.current.events[0]?.client_id).toBe('zone-1')
})

test('a socket that drops is reopened', () => {
  // ⚠️ The screen falls back to polling meanwhile, so this is not the difference between working
  // and not — it is the difference between a panel that comes back to life on its own and one
  // that stays degraded until somebody reloads the page.
  const { result } = renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })
  FakeSocket.opened[0]?.connect()

  FakeSocket.opened[0]?.drop()
  expect(result.current.connected).toBe(false)

  act(() => {
    vi.advanceTimersByTime(3000)
  })

  expect(FakeSocket.opened).toHaveLength(2)
})

test('a socket closed because the session ended is NOT reopened', () => {
  // ⚠️⚠️ **The bug this exists for is a loop nobody would notice for hours.** The server sends a
  // final state and closes when a session reaches a terminal status. Reconnecting then gets the
  // same state, gets closed again, and does it every three seconds for as long as the tab is
  // open — on a session that finished this morning.
  renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })
  const socket = FakeSocket.opened[0]
  socket?.connect()

  socket?.send({ type: 'state', session: session({ status: 'stopped' }) })
  socket?.drop()

  act(() => {
    vi.advanceTimersByTime(30_000)
  })

  expect(FakeSocket.opened).toHaveLength(1)
})

test('a session that is still running does not suppress the reconnect', () => {
  // The other half of the rule above: a `state` frame is not itself a reason to give up. Without
  // this case, "never reconnect after any state" would pass the test above and kill the feed on
  // the first bar of every healthy session.
  renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })
  const socket = FakeSocket.opened[0]
  socket?.connect()

  socket?.send({ type: 'state', session: session({ status: 'running' }) })
  socket?.drop()

  act(() => {
    vi.advanceTimersByTime(3000)
  })

  expect(FakeSocket.opened).toHaveLength(2)
})

test('unmounting closes the socket and schedules nothing', () => {
  const { unmount } = renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })
  FakeSocket.opened[0]?.connect()

  unmount()

  expect(FakeSocket.opened[0]?.closedByClient).toBe(true)
  act(() => {
    vi.advanceTimersByTime(30_000)
  })
  expect(FakeSocket.opened).toHaveLength(1)
})

test('switching session drops the previous session events', () => {
  // ⚠️ Not cosmetic: a fill from the session you were looking at a moment ago, shown under the
  // one you are looking at now, is a wrong fact about the wrong strategy — and it reads as
  // completely ordinary, because a fill is a fill.
  const { result, rerender } = renderHook(({ id }: { id: string }) => useLiveSessionFeed(id), {
    wrapper,
    initialProps: { id: 'sess-1' },
  })
  FakeSocket.opened[0]?.connect()
  FakeSocket.opened[0]?.send(FILL)
  expect(result.current.events).toHaveLength(1)

  rerender({ id: 'sess-2' })

  expect(result.current.events).toHaveLength(0)
})

test('a frame that is not JSON is ignored rather than thrown', () => {
  const { result } = renderHook(() => useLiveSessionFeed('sess-1'), { wrapper })
  const socket = FakeSocket.opened[0]
  socket?.connect()

  act(() => {
    socket?.onmessage?.({ data: 'not json' } as MessageEvent<string>)
  })

  expect(result.current.connected).toBe(true)
  expect(result.current.events).toHaveLength(0)
})
