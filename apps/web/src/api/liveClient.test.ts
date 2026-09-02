import { api, socketUrl } from './client'

function mockFetch(status: number, body: unknown) {
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(body === undefined ? '' : JSON.stringify(body)),
  })
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

describe('live session endpoints', () => {
  it('lists every session when no status is asked for', async () => {
    const fetchMock = mockFetch(200, { total: 0, sessions: [] })
    vi.stubGlobal('fetch', fetchMock)

    await api.listLiveSessions()

    // ⚠️ No `?status=`, not `?status=`. An empty filter asks the API for sessions whose status is
    // the empty string, which matches nothing; an absent one asks for all of them. The same
    // distinction the run log already had to learn.
    expect(fetchMock).toHaveBeenCalledWith('/api/live-sessions', expect.anything())
  })

  it('passes a status through when one is asked for', async () => {
    const fetchMock = mockFetch(200, { total: 0, sessions: [] })
    vi.stubGlobal('fetch', fetchMock)

    await api.listLiveSessions('running')

    expect(fetchMock).toHaveBeenCalledWith('/api/live-sessions?status=running', expect.anything())
  })

  it('reads one session', async () => {
    vi.stubGlobal('fetch', mockFetch(200, { id: 'sess-1' }))
    await expect(api.getLiveSession('sess-1')).resolves.toEqual({ id: 'sess-1' })
  })

  it('reads a page of events with a bound', async () => {
    const fetchMock = mockFetch(200, { total: 0, events: [] })
    vi.stubGlobal('fetch', fetchMock)

    await api.listSessionEvents('sess-1', 50)

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/live-sessions/sess-1/events?limit=50',
      expect.anything(),
    )
  })

  it('asks a session to stop with a POST and no body', async () => {
    const fetchMock = mockFetch(200, { id: 'sess-1' })
    vi.stubGlobal('fetch', fetchMock)

    await api.stopLiveSession('sess-1')

    // No body: the id is in the path and there is nothing else to say. A body would be a second
    // place for the target to be named, free to disagree with the first.
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/live-sessions/sess-1/stop',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetchMock.mock.calls[0]?.[1]).not.toHaveProperty('body')
  })

  it('reads and engages the kill switch, and offers nothing that releases it', async () => {
    const fetchMock = mockFetch(200, { engaged: true, engaged_at: null, layer: 'redis:x' })
    vi.stubGlobal('fetch', fetchMock)

    await api.getKillSwitch()
    await api.engageKillSwitch()

    expect(fetchMock).toHaveBeenNthCalledWith(1, '/api/executor/kill-switch', { method: 'GET' })
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/executor/kill-switch', { method: 'POST' })
    // ⚠️ The absence is the decision. An endpoint that can un-kill is an endpoint a retry can
    // un-kill, so there is no client method for it either — releasing is a shell command.
    expect(api).not.toHaveProperty('releaseKillSwitch')
  })
})

describe('socketUrl', () => {
  it('turns the API prefix into a ws:// address', () => {
    vi.stubGlobal('location', new URL('http://localhost:5173/live'))

    expect(socketUrl('/ws/live-sessions/sess-1')).toBe(
      'ws://localhost:5173/api/ws/live-sessions/sess-1',
    )
  })

  it('uses wss:// on a secure page', () => {
    // ⚠️ Not a nicety: a browser refuses a plain `ws://` from an `https://` page outright, and the
    // refusal arrives as a socket that closed with no reason attached — the same shape of silent
    // failure as a proxy that does not speak upgrade.
    vi.stubGlobal('location', new URL('https://tradeforge.example/live'))

    expect(socketUrl('/ws/live-sessions/sess-1')).toBe(
      'wss://tradeforge.example/api/ws/live-sessions/sess-1',
    )
  })
})
