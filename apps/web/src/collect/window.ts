import type { Timeframe } from '@tradeforge/schema'

import type { SymbolHistory } from '../api/types'

/**
 * The window the collection form starts on — a **default the screen pre-fills, never a limit**.
 *
 * ## Why a budget of bars and not a span of years
 *
 * Guilherme picked "one year of M1" and "five years of M5" by hand, and measured on this broker
 * they are the same number: 368,083 and 368,525 bars. That is not a coincidence about those two
 * timeframes — it is the criterion underneath both, because a bar is what turns into a trade. A
 * span of calendar years would give D1 a few hundred bars and M1 a hundred million, and only one
 * of those is a backtest.
 *
 * The budget generalises on its own to the slower timeframes, which need more calendar time to
 * produce the same amount of signal.
 *
 * ## Why the probe's floor can override it
 *
 * ⚠️ More history is not more validation. Bars with no range and a stamped spread make a backtest
 * look *better* while validating it *less* — no stop is touched inside a bar that never moved,
 * and no cost is real when the broker typed one number across a whole year. So the start is the
 * **later** of the two: as much data as the budget allows, but never earlier than the point the
 * probe says the series stops lying.
 */

/**
 * Bars a year, measured on EURUSD 2024 on this broker. The whole rule rests on these numbers.
 *
 * ⚠️ Typed `Record<Timeframe, number>` against the union **generated from the DSL schema**, so a
 * timeframe added there and forgotten here fails `tsc` rather than silently taking the fallback
 * below. A runtime test could not catch it: the fallback rate is used on both sides of the
 * budget arithmetic, so a missing entry still yields exactly the budget in bars and the window
 * comes out wrong by two orders of magnitude while every assertion passes.
 */
export const BARS_PER_YEAR: Record<Timeframe, number> = {
  M1: 368083,
  M5: 73705,
  M15: 24582,
  // Not measured directly — interpolated between M15 and H1, which bracket it by construction.
  // Flagged rather than presented as a measurement: it is the one number here nobody counted.
  M30: 12291,
  H1: 6150,
  H4: 1541,
  D1: 259,
  W1: 52,
}

/**
 * How many bars one window may hold before it stops being a window and starts being an import.
 *
 * ⚠️ A budget, not a limit: nothing enforces it. It is the number the form opens on, and the
 * operator is free to widen it — the system simply does not choose badly by omission.
 */
export const BAR_BUDGET = 368500

export interface SuggestedWindow {
  from: Date
  to: Date
  /** What decided the start, so the screen can say it rather than leaving a date unexplained. */
  bound: 'budget' | 'probe'
  bars: number
}

/**
 * Where the form should open for this symbol and timeframe.
 *
 * `history` is what the probe found, or `undefined` when nobody has measured yet — in which case
 * the budget is the only thing there is to go on, and the screen says so.
 */
export function suggestedWindow(
  timeframe: string,
  history: SymbolHistory | undefined,
  now: Date,
): SuggestedWindow {
  // The index is widened deliberately: `timeframe` arrives from a form and from the API as a
  // string, so the fallback is for a value the type system never saw — not for a gap in the map
  // above, which `tsc` now refuses.
  const perYear = (BARS_PER_YEAR as Record<string, number>)[timeframe] ?? BARS_PER_YEAR.H1
  const budgetYears = BAR_BUDGET / perYear
  const budgetFrom = new Date(now.getTime() - budgetYears * MS_PER_YEAR)

  const usable = history?.usable_from === undefined ? null : history.usable_from
  const probeFrom = usable === null ? null : new Date(usable)

  // ⚠️ The **later** of the two. Taking the earlier would hand the budget back to exactly the
  // years the probe just finished arguing are worthless — and a window is only as trustworthy
  // as its weaker half.
  const takeProbe = probeFrom !== null && probeFrom > budgetFrom
  const from = takeProbe ? probeFrom : budgetFrom

  const years = (now.getTime() - from.getTime()) / MS_PER_YEAR
  return {
    from,
    to: now,
    bound: takeProbe ? 'probe' : 'budget',
    bars: Math.round(years * perYear),
  }
}

/** ⚠️ 365.25 days, not 365. Over the 17 years an H1 budget spans, the difference is four days —
 * small, and the kind of small that turns into "why does it start on the 28th". */
const MS_PER_YEAR = 365.25 * 24 * 60 * 60 * 1000

/** `YYYY-MM-DD`, which is what an `<input type="date">` reads and writes. */
export function asDateInput(when: Date): string {
  return when.toISOString().slice(0, 10)
}

/**
 * A date from the form as the instant the API requires.
 *
 * ⚠️ Midnight **UTC**, stated rather than inferred. `new Date('2020-01-01')` is already UTC and
 * `new Date(2020, 0, 1)` is local — the two differ by hours, and the API refuses a naive instant
 * precisely so that this choice has to be made somewhere a person can see it.
 */
export function asInstant(dateInput: string, endOfDay = false): string {
  return `${dateInput}T${endOfDay ? '23:59:59' : '00:00:00'}Z`
}

/**
 * How many calendar-year slices a window is cut into — the unit the work is actually done in.
 *
 * ⚠️ **Calendar years, not elapsed years.** This mirrors `collect.year_slices` on the server,
 * which cuts at year boundaries because that is how the Parquet is partitioned. Ten months that
 * straddle new year are two slices, not one; dividing elapsed days by 365 would say one and be
 * wrong by a whole download.
 *
 * A backwards or unfinished window is **no** work rather than negative or `NaN` work. The API
 * refuses both and the button is disabled for both, so this is only ever read mid-edit — and a
 * negative count rendered under the form is nonsense on screen.
 */
export function yearSlices(from: string, to: string): number {
  const start = Number(from.slice(0, 4))
  const end = Number(to.slice(0, 4))
  if (!Number.isFinite(start) || !Number.isFinite(end) || from === '' || to === '') return 0
  return Math.max(0, end - start + 1)
}

/**
 * The whole batch's work: one window per symbol, because each symbol is its own collection
 * walking its own years. Twenty symbols over seventeen years is 340 slices, and each cold one
 * is minutes of a terminal downloading — which is the number worth seeing before pressing a
 * button, rather than after.
 */
export function estimateSlices(symbols: number, from: string, to: string): number {
  return symbols * yearSlices(from, to)
}

/**
 * Of everything measured about the chosen symbols, the one whose floor binds the batch.
 *
 * ⚠️ **The latest floor wins, which is the same rule `suggestedWindow` already applies between
 * the budget and one probe.** Opening on the earliest floor would hand the batch exactly the
 * years the probe just finished arguing are worthless — for every symbol that starts later, a
 * stretch of invented spread and filler bars, and a set of backtests that look better for being
 * less validated. One window for all means the weakest half decides.
 *
 * `undefined` when nothing has been measured yet, which `suggestedWindow` reads as "the budget
 * is all there is to go on" and says so on screen.
 */
export function bindingFloor(
  histories: Iterable<SymbolHistory>,
): SymbolHistory | undefined {
  let binding: SymbolHistory | undefined
  for (const history of histories) {
    if (history.usable_from === null) continue
    if (binding?.usable_from == null || history.usable_from > binding.usable_from) {
      binding = history
    }
  }
  return binding
}

/**
 * How many symbols one batch may carry. Mirrors `MAX_COLLECTION_SYMBOLS` on the API, which is
 * the one that actually refuses — this only keeps the screen from offering what would be turned
 * away, and says so before the request rather than after it.
 */
export const MAX_BATCH_SYMBOLS = 20
