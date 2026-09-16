// How far along a sweep is, in one line: began, advanced, finished.
//
// ⚠️ **Counted from the runs' own statuses, not from the entries' aggregates.** An aggregate
// calls a point finished once it has metrics, so a run marked `done` an instant before its
// metrics row lands would be in neither column. Every run carries exactly one status, so a tally
// over statuses always adds up to the total — which is the whole promise of an X of Y counter.

import type { BacktestStatus, SweepRunOut } from '../api/types'

export interface Tally {
  total: number
  done: number
  running: number
  queued: number
  failed: number
}

const EMPTY: Tally = { total: 0, done: 0, running: 0, queued: 0, failed: 0 }

export function tally(rows: readonly SweepRunOut[]): Tally {
  const counted: Record<BacktestStatus, number> = { queued: 0, running: 0, done: 0, failed: 0 }
  for (const row of rows) {
    counted[row.run.status] += 1
  }
  return { ...EMPTY, ...counted, total: rows.length }
}

/**
 * The counter as a sentence: `8 of 10 done · 1 running · 1 queued`.
 *
 * ⚠️ **Always says how many are done, even when that is all of them.** The line this replaced
 * appeared only while runs were outstanding, so "it finished" and "it never started" both showed
 * as no line at all — and a sweep holding a run that will never execute (a worker that lost its
 * database connection leaves one `queued` for ever) looked exactly like a sweep that was done.
 *
 * The other counts appear only when they are not zero: a finished sweep reads `10 of 10 done`
 * rather than trailing three zeroes a reader has to check.
 */
export function summarise(counts: Tally): string {
  const parts = [`${String(counts.done)} of ${String(counts.total)} done`]
  if (counts.running > 0) parts.push(`${String(counts.running)} running`)
  if (counts.queued > 0) parts.push(`${String(counts.queued)} queued`)
  if (counts.failed > 0) parts.push(`${String(counts.failed)} failed`)
  return parts.join(' · ')
}

/** Nothing left in flight. A failed run is settled: it is not coming back on its own. */
export function settled(counts: Tally): boolean {
  return counts.running === 0 && counts.queued === 0
}
