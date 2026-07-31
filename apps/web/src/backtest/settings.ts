// What a backtest needs beyond the strategy itself: an instrument, a window, a starting balance
// and a cost model. Kept free of React, like the strategy builder's core, so the folding — dates
// to instants, a cost choice to a cost model, strings to the numbers the API wants — is proven
// here rather than inferred from what a screen renders.
//
// The timeframe is deliberately *not* part of this. It belongs to the strategy document, and a
// screen that asked for it twice could run an H1 strategy over M5 candles and report the result
// without a word. It is passed in by whoever owns the strategy.

import type { CreateBacktestRequest } from '../api/types'

export type CostKind = 'none' | 'spread'

export interface BacktestForm {
  symbol: string
  dateFrom: string
  dateTo: string
  capital: string
  cost: CostKind
  spreadPoints: string
}

/** A date input gives `YYYY-MM-DD`; the API wants an instant. Midnight UTC is the honest reading
 *  of "this day" for a backtest window. */
export function toIso(date: string): string {
  return `${date}T00:00:00Z`
}

/** A year of H1 on a round balance — a window the collector actually has data for. */
export function emptyBacktestForm(): BacktestForm {
  return {
    symbol: '',
    dateFrom: '2024-01-01',
    dateTo: '2024-12-31',
    capital: '10000',
    cost: 'none',
    spreadPoints: '10',
  }
}

export function costModel(form: BacktestForm): CreateBacktestRequest['cost_model'] {
  return form.cost === 'spread'
    ? { type: 'spread', spread_points: Number(form.spreadPoints) }
    : { type: 'none' }
}

/**
 * The request body, given the strategy this run belongs to and the timeframe it was written for.
 *
 * `initial_capital` stays a string all the way to the wire: it is money, and the API parses it
 * into a `Decimal`. Routing it through a JavaScript number first is how a balance picks up binary
 * dust that then compounds through every trade in the run.
 */
export function toBacktestRequest(
  form: BacktestForm,
  strategyId: string,
  timeframe: string,
): CreateBacktestRequest {
  return {
    strategy_id: strategyId,
    symbol: form.symbol,
    timeframe,
    date_from: toIso(form.dateFrom),
    date_to: toIso(form.dateTo),
    initial_capital: form.capital,
    cost_model: costModel(form),
  }
}

/** Why the run cannot start yet, or `null` if it can. The screen shows this instead of a button
 *  that is disabled for reasons the user has to guess. */
export function whyNotRunnable(form: BacktestForm): string | null {
  if (form.symbol === '') return 'choose an instrument'
  if (form.dateFrom === '' || form.dateTo === '') return 'set the backtest window'
  if (form.dateFrom >= form.dateTo) return 'the window ends before it starts'
  if (Number(form.capital) <= 0) return 'initial capital must be positive'
  if (form.cost === 'spread' && Number(form.spreadPoints) < 0) {
    return 'a spread is a magnitude, not a direction'
  }
  return null
}
