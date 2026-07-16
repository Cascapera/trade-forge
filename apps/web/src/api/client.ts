// A thin typed wrapper over `fetch`. One place assembles URLs, sets JSON headers, and turns a
// non-2xx into a typed `ApiError` carrying the parsed `detail` — so a caller (and a test) can
// branch on the status and read the backend's message instead of a bare rejection.

import type {
  Backtest,
  CreateBacktestRequest,
  CreatedBacktest,
  EquityPoint,
  Instrument,
  StrategyOut,
  TradesPage,
} from './types'

const BASE_URL = import.meta.env.VITE_API_URL ?? '/api'

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly detail: unknown,
  ) {
    super(`API error ${String(status)}`)
    this.name = 'ApiError'
  }
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  // Built up rather than spread with `undefined` values: under exactOptionalPropertyTypes a
  // present-but-undefined `body` is not the same as an absent one, and `fetch` wants it absent.
  const init: RequestInit = { method }
  if (body !== undefined) {
    init.headers = { 'Content-Type': 'application/json' }
    init.body = JSON.stringify(body)
  }
  const response = await fetch(`${BASE_URL}${path}`, init)
  const text = await response.text()
  const payload: unknown = text ? JSON.parse(text) : null
  if (!response.ok) {
    const detail =
      payload && typeof payload === 'object' && 'detail' in payload ? payload.detail : payload
    throw new ApiError(response.status, detail)
  }
  return payload as T
}

export const api = {
  listInstruments: (): Promise<Instrument[]> => request('GET', '/instruments'),
  createStrategy: (definition: unknown): Promise<StrategyOut> =>
    request('POST', '/strategies', definition),
  createBacktest: (payload: CreateBacktestRequest): Promise<CreatedBacktest> =>
    request('POST', '/backtests', payload),
  getBacktest: (id: string): Promise<Backtest> => request('GET', `/backtests/${id}`),
  getTrades: (id: string, limit = 100, offset = 0): Promise<TradesPage> =>
    request('GET', `/backtests/${id}/trades?limit=${String(limit)}&offset=${String(offset)}`),
  getEquity: (id: string): Promise<EquityPoint[]> => request('GET', `/backtests/${id}/equity`),
}
