// What a basket needs beyond the strategy: which markets to try it on, over what window, from
// what starting balance. Kept free of React, like the backtest form and the strategy builder's
// core, so the folding — a set of ticked symbols to a request body, a catalogue row to what that
// market will be charged — is proven here rather than inferred from what a screen renders.
//
// ⚠️ **There is no cost field, and the absence is the design.** A single backtest asks the user
// what to charge; a basket cannot, because there is no one number to charge. Each run is charged
// its own instrument's measured spread, decided by the server. What this module owes the screen
// is therefore not a control but a *preview*: what each ticked symbol is about to pay, and which
// ones nobody has measured.

import { ApiError } from '../api/client'
import type { BacktestListItem, CreateBasketRequest, Instrument } from '../api/types'
import { toIso } from '../backtest/settings'

/** A basket of one is a backtest, and the screen that already does that is better at it. */
export const MIN_SYMBOLS = 2

/** Mirrors the API's ceiling. Twenty is past what a human reads off one screen. */
export const MAX_SYMBOLS = 20

export interface BasketForm {
  /**
   * The ticked symbols, in the order they were ticked.
   *
   * An array rather than a Set because the order is meaningful on the wire: the API returns the
   * runs in the order they were asked for, so the confirmation the user reads back matches the
   * list they built. The picker itself renders the catalogue's order — a stable grid of
   * checkboxes that never reshuffles under the cursor — and only this list follows the clicks.
   */
  symbols: string[]
  dateFrom: string
  dateTo: string
  capital: string
}

export function emptyBasketForm(): BasketForm {
  return { symbols: [], dateFrom: '2024-01-01', dateTo: '2024-12-31', capital: '10000' }
}

/**
 * Tick or untick a market.
 *
 * Ticking appends; unticking removes and leaves the rest in place. Nothing is sorted on the way
 * in, so unticking the first market cannot reorder the others — the same reason the run
 * comparator seats its runs instead of ranking them, one screen over.
 */
export function toggleSymbol(form: BasketForm, symbol: string): BasketForm {
  const chosen = form.symbols.includes(symbol)
  return {
    ...form,
    symbols: chosen ? form.symbols.filter((one) => one !== symbol) : [...form.symbols, symbol],
  }
}

/**
 * The measured spread as a person reads it, or `null` when nobody measured this symbol.
 *
 * ⚠️ `null` and `0` are different claims and this is the function that keeps them apart. A
 * Decimal column arrives as `8.0000000000`; `8` is the same number and the only one worth
 * showing. A missing measurement is not a small spread — it is no spread, and the screen has to
 * say which of the two it is looking at.
 */
export function measuredSpread(instrument: Instrument | undefined): string | null {
  const spread = instrument?.default_spread_points
  if (spread === undefined || spread === null) return null
  return String(Number(spread))
}

/**
 * The ticked markets nobody has measured a spread for — what the launch warning is about.
 *
 * Returned as names, not as a count, because the reader's next move is to go and catalogue those
 * symbols and "two of them" does not say which. A basket that mixed costed and uncosted runs
 * without saying so would present them side by side as one comparison, and the uncosted ones
 * would be flattered by exactly the amount nobody knows.
 */
export function uncostedAmong(
  symbols: readonly string[],
  instruments: readonly Instrument[] | undefined,
): string[] {
  const byName = new Map((instruments ?? []).map((one) => [one.symbol, one]))
  return symbols.filter((symbol) => measuredSpread(byName.get(symbol)) === null)
}

/** Why the basket cannot be launched yet, or `null` if it can. */
export function whyNotLaunchable(form: BasketForm): string | null {
  if (form.symbols.length < MIN_SYMBOLS) {
    return `choose at least ${String(MIN_SYMBOLS)} markets — one market is a backtest, not a basket`
  }
  if (form.symbols.length > MAX_SYMBOLS) {
    return `choose at most ${String(MAX_SYMBOLS)} markets`
  }
  if (form.dateFrom === '' || form.dateTo === '') return 'set the backtest window'
  if (form.dateFrom >= form.dateTo) return 'the window ends before it starts'
  if (Number(form.capital) <= 0) return 'initial capital must be positive'
  return null
}

/**
 * The request body, given the strategy this basket runs and the timeframe it was written for.
 *
 * `initial_capital` stays a string all the way to the wire, for the same reason a single
 * backtest's does: it is money, and routing it through a JavaScript number first is how a
 * balance picks up binary dust that then compounds through every trade of every run.
 */
export function toBasketRequest(
  form: BasketForm,
  strategyId: string,
  timeframe: string,
): CreateBasketRequest {
  return {
    strategy_id: strategyId,
    symbols: [...form.symbols],
    timeframe,
    date_from: toIso(form.dateFrom),
    date_to: toIso(form.dateTo),
    initial_capital: form.capital,
  }
}

/**
 * The runs that should be put on the chart now, without anyone ticking a box.
 *
 * A basket is launched to be read as a whole, so its curves appear on their own as the runs land
 * — unlike the run log, where the reader picks a handful out of hundreds. The runs finish at
 * different moments, so this is asked again on every poll.
 *
 * ⚠️ **`offered` is what makes unticking stick, and it is the whole reason this is a function
 * rather than a filter written inline.** Without it, "every finished run that is not currently
 * seated" would re-seat a run the moment the reader unticked it: the next poll would find it
 * finished and unseated and put it straight back. The reader would be unable to remove a line
 * from the chart, and nothing would look broken — the checkbox would simply refuse to stay off.
 * A run is therefore offered a seat exactly once, whatever it does afterwards.
 *
 * Only a finished run with metrics is offered: a queued one has no curve, and seating it would
 * spend a colour slot on an empty line.
 */
export function newlyComparable(
  runs: readonly BacktestListItem[],
  offered: ReadonlySet<string>,
): BacktestListItem[] {
  return runs.filter(
    (run) => run.status === 'done' && run.metrics !== null && !offered.has(run.id),
  )
}

/**
 * What to tell the user when a launch is refused.
 *
 * The API's `detail` is shown verbatim when there is one, and that is the point rather than
 * laziness: a basket naming two unknown symbols is refused with **both** of them listed, and
 * replacing that with a house message would throw away the only part the reader can act on and
 * send them back to fixing a typo list one round trip at a time.
 */
export function launchFailure(error: unknown): string {
  if (error instanceof ApiError && typeof error.detail === 'string') return error.detail
  return 'Could not enqueue the basket. Check the fields.'
}

/**
 * What names a basket in the navigation once it has been launched.
 *
 * The strategy and how many markets it was tried on — the two things that tell one basket from
 * another at a glance. Not the symbols themselves: at twenty they would not fit, and the count
 * is what the reader is actually keeping track of between screens.
 */
export function basketLabel(strategyName: string, symbolCount: number): string {
  return `${strategyName} · ${String(symbolCount)} markets`
}
