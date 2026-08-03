// Did the run cover the period it was asked for?
//
// A backtest row carries two windows that are easy to mistake for one. `date_from`/`date_to`
// are the *request*; `first_candle`/`last_candle` are what the dataset actually had. Ask for
// two years over a dataset that opens eight months in and the run covers five months — with
// every metric computed honestly over those five, under a heading that says two years.
//
// No React here on purpose: the rule for when the two windows disagree is the part worth
// testing, and it should not need a rendered component to exercise.

import type { Backtest } from '../api/types'

export interface CoverageNotice {
  requestedFrom: string
  requestedTo: string
  actualFrom: string
  actualTo: string
  candles: number
}

/** The calendar day of an ISO instant, which is the granularity a user asked in. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

/**
 * A notice when the run read less than it was asked for, or `null` when there is nothing
 * worth saying.
 *
 * Null — not a notice claiming full coverage — when the run has no provenance at all. A run
 * that failed never read a candle, and rows written before the API recorded this genuinely do
 * not know. Inventing "covered it all" for those would be the same class of lie this whole
 * module exists to stop.
 */
export function coverageNotice(run: Backtest): CoverageNotice | null {
  if (run.candles_seen === null || run.first_candle === null || run.last_candle === null) {
    return null
  }

  const requestedFrom = day(run.date_from)
  const requestedTo = day(run.date_to)
  const actualFrom = day(run.first_candle)
  const actualTo = day(run.last_candle)

  // Compared as calendar days, not instants: a request for "2024-08-01" against a first bar
  // at 13:00 that same day is the whole day's data, not a gap. Flagging that would train the
  // reader to ignore the notice, which costs more than the notice is worth.
  if (actualFrom <= requestedFrom && actualTo >= requestedTo) {
    return null
  }

  return { requestedFrom, requestedTo, actualFrom, actualTo, candles: run.candles_seen }
}
