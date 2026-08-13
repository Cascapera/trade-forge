// A thin typed wrapper over `fetch`. One place assembles URLs, sets JSON headers, and turns a
// non-2xx into a typed `ApiError` carrying the parsed `detail` — so a caller (and a test) can
// branch on the status and read the backend's message instead of a bare rejection.

import type {
  Backtest,
  BacktestFilters,
  BacktestsPage,
  BasketOut,
  CandlesResponse,
  CreateBacktestRequest,
  CreateBasketRequest,
  CreatedBacktest,
  CreatedBasket,
  EquityPoint,
  Instrument,
  OverlaysResponse,
  Snapshot,
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

/**
 * A query string from the fields that are actually set.
 *
 * An absent filter and an empty one are different requests: `?symbol=` asks the API for runs whose
 * symbol is the empty string, which matches nothing, while omitting it asks for every symbol. The
 * run log's "All" option produces `undefined`, so it has to disappear from the URL entirely.
 */
function query(params: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams()
  for (const [key, value] of Object.entries(params)) {
    if (value !== undefined && value !== '') search.set(key, String(value))
  }
  const text = search.toString()
  return text ? `?${text}` : ''
}

export const api = {
  listInstruments: (): Promise<Instrument[]> => request('GET', '/instruments'),
  listBacktests: (filters: BacktestFilters = {}): Promise<BacktestsPage> =>
    request('GET', `/backtests${query({ ...filters })}`),
  createStrategy: (definition: unknown): Promise<StrategyOut> =>
    request('POST', '/strategies', definition),
  // Editing is a new version, not an update: the API inserts the next version linked to this
  // parent. It is what makes iterating on a strategy possible at all — `POST` always writes
  // version 1, and name+version is unique, so re-saving under the same name can only conflict.
  updateStrategy: (id: string, definition: unknown): Promise<StrategyOut> =>
    request('PUT', `/strategies/${id}`, definition),
  createBacktest: (payload: CreateBacktestRequest): Promise<CreatedBacktest> =>
    request('POST', '/backtests', payload),
  getBacktest: (id: string): Promise<Backtest> => request('GET', `/backtests/${id}`),
  getTrades: (id: string, limit = 100, offset = 0): Promise<TradesPage> =>
    request('GET', `/backtests/${id}/trades?limit=${String(limit)}&offset=${String(offset)}`),
  getEquity: (id: string): Promise<EquityPoint[]> => request('GET', `/backtests/${id}/equity`),
  // No window parameters, and that is the endpoint's design rather than an omission: the run
  // already recorded which candles it read, so the chart cannot be asked for a period the run
  // did not execute over. A client that assembled the window itself could get it subtly wrong,
  // and a chart disagreeing with the trades drawn on it says nothing about the disagreement.
  getCandles: (id: string): Promise<CandlesResponse> => request('GET', `/backtests/${id}/candles`),
  // Recomputed per request rather than stored: an indicator series is derivable from the candles
  // and the parameters, and the engine is deterministic, so what comes back is what the run
  // computed. Separate from `getCandles` because a strategy can legitimately have no curves.
  getOverlays: (id: string): Promise<OverlaysResponse> =>
    request('GET', `/backtests/${id}/overlays`),
  // One snapshot at a time. The list deliberately does not carry them — fifty bars per trade
  // would be megabytes assembled to be thrown away — so this is the only way to get one.
  getTradeSnapshot: (id: string, tradeId: number): Promise<Snapshot> =>
    request('GET', `/backtests/${id}/trades/${String(tradeId)}/snapshot`),
  // One POST enqueues every run in the basket, or none of them: an unknown symbol is refused
  // whole rather than leaving half a comparison behind. The 422's `detail` names every bad
  // symbol at once, which is why `ApiError` carries it.
  createBasket: (payload: CreateBasketRequest): Promise<CreatedBasket> =>
    request('POST', '/baskets', payload),
  getBasket: (id: string): Promise<BasketOut> => request('GET', `/baskets/${id}`),
}
