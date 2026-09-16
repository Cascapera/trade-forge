// How far along a sweep is, in one line: began, advanced, finished.
//
// ⚠️ **Counted from the runs' own statuses, not from the entries' aggregates.** The line this
// replaced subtracted the aggregates (`points_total - points_finished - points_failed`), and an
// aggregate calls a point finished by its *metrics*, which the sweep read loads in a second
// statement after the statuses — so the two can describe slightly different moments. Every run
// carries exactly one of the four statuses, so a tally over them adds up to the total, which is
// the whole promise of an X of Y counter.

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
 * ⚠️ **Says how many are done, and keeps waiting apart from executing.** The line this replaced
 * read `1 of 10 backtests still running`: it never said how many had finished, it disappeared
 * once nothing was outstanding, and it counted a queued run as running. That last one is what hid
 * a real defect — a worker that took its job before Postgres accepted connections left a run
 * `queued` for ever, and the screen called it "still running" indefinitely. Now it reads
 * `9 of 10 done · 1 queued`.
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
