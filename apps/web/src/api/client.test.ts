import { api, ApiError } from './client'
import type { CreateBacktestRequest } from './types'

function mockFetch(status: number, body: unknown) {
  const text = body === undefined ? '' : JSON.stringify(body)
  return vi.fn().mockResolvedValue({
    ok: status >= 200 && status < 300,
    status,
    text: () => Promise.resolve(text),
  })
}

const A_BACKTEST: CreateBacktestRequest = {
  strategy_id: 's',
  symbol: 'EURUSD',
  timeframe: 'H1',
  date_from: '2024-01-01T00:00:00Z',
  date_to: '2024-02-01T00:00:00Z',
  initial_capital: '10000',
  cost_model: { type: 'none' },
}

describe('api client', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('GETs and parses the JSON body', async () => {
    vi.stubGlobal('fetch', mockFetch(200, [{ symbol: 'EURUSD' }]))
    await expect(api.listInstruments()).resolves.toEqual([{ symbol: 'EURUSD' }])
  })

  it('POSTs with JSON headers and a serialised body', async () => {
    const fetchMock = mockFetch(201, { id: '1' })
    vi.stubGlobal('fetch', fetchMock)

    await api.createStrategy({ name: 'x' })

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/strategies',
      expect.objectContaining({
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: 'x' }),
      }),
    )
  })

  it('throws an ApiError carrying the detail on a non-2xx', async () => {
    vi.stubGlobal('fetch', mockFetch(404, { detail: 'not found' }))
    await expect(api.getBacktest('x')).rejects.toMatchObject({ status: 404, detail: 'not found' })
  })

  it('falls back to the raw payload when there is no detail field', async () => {
    vi.stubGlobal('fetch', mockFetch(500, ['weird']))
    const error = await api.getBacktest('x').catch((caught: unknown) => caught)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).detail).toEqual(['weird'])
  })

  it('treats an empty body as null', async () => {
    vi.stubGlobal('fetch', mockFetch(200, undefined))
    await expect(api.getEquity('x')).resolves.toBeNull()
  })

  it('reaches the trades and backtest-creation endpoints', async () => {
    const fetchMock = mockFetch(200, { total: 0, items: [] })
    vi.stubGlobal('fetch', fetchMock)

    await api.getTrades('x')
    await api.createBacktest(A_BACKTEST)

    expect(fetchMock).toHaveBeenCalledWith('/api/backtests/x/trades?limit=100&offset=0', expect.anything())
    expect(fetchMock).toHaveBeenCalledWith('/api/backtests', expect.objectContaining({ method: 'POST' }))
  })
})
