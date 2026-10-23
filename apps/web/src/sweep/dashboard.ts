// The dashboard's two pieces of screen-side arithmetic: turning the days a person picked into
// instants, and counting runs per day. Everything else is the server's.

import type { DashboardSweep, LaunchWindow } from '../api/types'

/**
 * A `YYYY-MM-DD` from a date input, as local midnight — or null when the field is empty or not in
 * that shape. An impossible day in the right shape (`2026-02-31`) rolls over as `Date` does; a
 * date input never produces one.
 */
function localMidnight(day: string, plusDays = 0): Date | null {
  const found = /^(\d{4})-(\d{2})-(\d{2})$/.exec(day)
  if (found === null) return null
  const [, year, month, date] = found
  return new Date(Number(year), Number(month) - 1, Number(date) + plusDays)
}

/**
 * The launch window for two picked days, both included, in the reader's own clock.
 *
 * ⚠️ **The instants are made here, not on the server.** "Launched on the 15th" is a statement
 * about a local calendar: a sweep launched at 22:00 in São Paulo is already the 16th in UTC.
 * The browser is the one place that knows which zone the reader means.
 *
 * The last day is included, so the window ends at the **next** midnight — the server's end is
 * exclusive. Null when the days run backwards, which is a typo rather than a question.
 */
export function launchWindow(fromDay: string, toDay: string): LaunchWindow | null {
  const from = localMidnight(fromDay)
  const to = localMidnight(toDay, 1)
  if (from !== null && to !== null && to <= from) return null
  const launched: LaunchWindow = {}
  if (from !== null) launched.launched_from = from.toISOString()
  if (to !== null) launched.launched_to = to.toISOString()
  return launched
}

/** The reader's calendar day of an instant, `YYYY-MM-DD`. */
export function localDay(iso: string): string {
  const moment = new Date(iso)
  const pad = (value: number): string => String(value).padStart(2, '0')
  return `${String(moment.getFullYear())}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())}`
}

/** How many runs were launched on each local day, oldest first. Days with none are left out. */
export function runsPerDay(sweeps: readonly DashboardSweep[]): { day: string; runs: number }[] {
  const totals = new Map<string, number>()
  for (const sweep of sweeps) {
    const day = localDay(sweep.created_at)
    totals.set(day, (totals.get(day) ?? 0) + sweep.runs)
  }
  return [...totals.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([day, runs]) => ({ day, runs }))
}
