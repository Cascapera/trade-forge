import type { SymbolHistory } from '../api/types'

import { asDateInput, estimateSlices, suggestedWindow, yearSlices } from './window'

/** A timeframe row as the form holds it: dates as `YYYY-MM-DD`, the way the inputs read them. */
export interface DraftRow {
  /** Stable across re-orders and removals, so React keys never collide after a delete. */
  id: string
  timeframe: string
  from: string
  to: string
  /**
   * Whether a person has edited these dates.
   *
   * ⚠️ Until they have, the window is a **suggestion that keeps following the
   * measurements** — a row opened before any symbol was chosen has no floor to stand on, and
   * would otherwise sit on the raw bar budget for ever, offering years the probe later calls
   * filler. After they have, nothing may move it: re-deriving would snap the field back under
   * the cursor of the very person a measurement is arriving for.
   */
  touched: boolean
}

/**
 * How many timeframe rows one batch may carry, and the total collections it may produce.
 *
 * ⚠️ Two ceilings for two different things, mirroring the API. `MAX_BATCH_SYMBOLS` is about a
 * list a person reads on one screen; `MAX_COLLECTIONS` is about a queue that drains one job at
 * a time. Neither subsumes the other — twenty-one symbols on one row is under the collections
 * ceiling and still refused, and ten symbols across five timeframes is under the symbol ceiling
 * and still refused.
 */
export const MAX_COLLECTIONS = 40

/** Every timeframe the DSL defines, in the order a person thinks about them. */
export const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'] as const

/**
 * A new row, opened on the window its timeframe deserves.
 *
 * ⚠️ **The suggestion is per timeframe, and that is the whole reason rows exist.** Picking M1
 * suggests about a year; picking H1 suggests seventeen — the same budget of bars over spans two
 * orders of magnitude apart. A row that opened on the previous row's dates would hand somebody
 * seventeen years of M1, which this broker's terminal will not even serve.
 *
 * `floor` is the binding measurement among the chosen symbols, so a row never opens on years
 * the probe says are filler and typed costs.
 */
export function newRow(
  timeframe: string,
  floor: SymbolHistory | undefined,
  now: Date,
  id: string,
): DraftRow {
  const suggested = suggestedWindow(timeframe, floor, now)
  return {
    id,
    timeframe,
    from: asDateInput(suggested.from),
    to: asDateInput(suggested.to),
    touched: false,
  }
}


/**
 * The rows as they should render: untouched ones following the current binding floor.
 *
 * ⚠️ **Derived on every render rather than written into state.** A row is opened before
 * any symbol is chosen, so its first window comes from the bar budget alone; the measurements
 * arrive seconds later and the row has to follow them. Writing the new dates into state would
 * work too, right up until it fought somebody mid-edit — which is what `touched` prevents.
 */
export function withSuggestedWindows(
  rows: readonly DraftRow[],
  floor: SymbolHistory | undefined,
  now: Date,
): DraftRow[] {
  return rows.map((row) =>
    row.touched ? { ...row } : newRow(row.timeframe, floor, now, row.id),
  )
}

/**
 * The first timeframe not already taken, so the add button never opens a duplicate row.
 *
 * ⚠️ The API refuses a repeated timeframe, because two collections of the same series overwrite
 * each other's year partitions — the symptom of which is a *missing* year, not a duplicate one.
 * Offering a duplicate and then refusing it would be the screen setting a trap it knows about.
 */
export function nextFreeTimeframe(taken: readonly string[]): string | undefined {
  return TIMEFRAMES.find((timeframe) => !taken.includes(timeframe))
}

/** Total collections a batch would create: one per symbol per row. */
export function totalCollections(symbols: number, rows: readonly DraftRow[]): number {
  return symbols * rows.length
}

/**
 * The work in the unit it is actually done in — year slices, summed across rows.
 *
 * ⚠️ **Not the collection count.** Forty H1 collections and forty of M1 are the same number and
 * nothing like the same wait: a year of M1 is sixty times the bars. The count is the guardrail;
 * this is the information.
 */
export function totalSlices(symbols: number, rows: readonly DraftRow[]): number {
  return rows.reduce((sum, row) => sum + estimateSlices(symbols, row.from, row.to), 0)
}

/** Why the batch cannot be sent, or `null` when it can. */
export function blockedReason(args: {
  symbols: number
  rows: readonly DraftRow[]
  unanswered: readonly string[]
}): string | null {
  const { symbols, rows, unanswered } = args
  if (symbols === 0) return 'Choose at least one symbol.'
  if (rows.length === 0) return 'Add at least one timeframe.'

  const incomplete = rows.find((row) => row.from === '' || row.to === '')
  if (incomplete !== undefined) return `Set both ends of the ${incomplete.timeframe} window.`

  const backwards = rows.find((row) => row.to < row.from)
  if (backwards !== undefined) return `The ${backwards.timeframe} window runs backwards.`

  // ⚠️ Checked here as well as on the server, because the server's refusal would arrive after
  // the form is gone. Same rule, said early.
  const total = totalCollections(symbols, rows)
  if (total > MAX_COLLECTIONS) {
    return `${String(symbols)} symbols across ${String(rows.length)} timeframes is ${String(
      total,
    )} collections — at most ${String(MAX_COLLECTIONS)} at once.`
  }

  if (unanswered.length > 0) {
    return `Say what ${unanswered.join(', ')} ${unanswered.length === 1 ? 'is' : 'are'}.`
  }
  return null
}

/** Rows that ask for a year the measurement says is empty, per row. See `shortWindows`. */
export function rowsWorthWarningAbout(rows: readonly DraftRow[]): readonly DraftRow[] {
  return rows.filter((row) => yearSlices(row.from, row.to) > 0)
}
