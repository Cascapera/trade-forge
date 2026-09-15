import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useEquityCurves, useSweep } from '../api/hooks'
import {
  EMPTY_SEATS,
  MAX_COMPARED,
  buildSeries,
  selectedIds,
  toggleSeat,
} from '../backtest/compare'
import { ComparisonChart } from '../components/ComparisonChart'
import { RunTable } from '../components/RunTable'
import { StudyDispersion } from '../components/StudyDispersion'
import { money } from '../format'

/** The calendar day of an ISO instant — the granularity a window is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

function backtests(count: number): string {
  return `${String(count)} backtest${count === 1 ? '' : 's'}`
}

/**
 * One sweep read back: every entry it ran, each summarised on its own.
 *
 * ⚠️ **A section per entry, and never one summary for the whole sweep.** The entries of a sweep
 * are alternatives — `9.1 sem filtro` beside `choch com filtro H4` — so a median across them is the
 * median of two methods and describes neither. The server summarises each entry separately for
 * that reason, and this screen keeps the separation visible instead of adding the sections up.
 *
 * Inside a section the reading order is the study's, for the study's reason: the dispersion comes
 * first, led by the median, and the best run is one row in a table like every other. A sweep
 * searches more than a study does, so its best is the best of more draws, and leading with it
 * would present the luckiest corner of a larger search as the result.
 *
 * ⚠️ **One chart for the whole screen, above the sections, rather than one per entry.** The
 * comparison worth making often crosses entries — the median run of one method against the
 * median of the other — and a chart inside each section could never hold both. Nothing seats
 * itself, as on a study: at this scale whichever runs finished first would fill every seat.
 */
export function SweepResult(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  const sweep = useSweep(id)
  const [seats, setSeats] = useState(EMPTY_SEATS)

  const runs = useMemo(() => (sweep.data?.runs ?? []).map((row) => row.run), [sweep.data])
  const picked = useMemo(() => selectedIds(seats), [seats])
  const { curves } = useEquityCurves(picked)
  const series = useMemo(() => buildSeries(seats, runs, curves), [seats, runs, curves])

  function toggle(runId: string): void {
    setSeats((current) => toggleSeat(current, runId))
  }

  if (sweep.isPending) return <p className="text-slate-400">Loading the sweep…</p>
  if (sweep.isError) {
    return <p className="text-red-400">Could not load this sweep.</p>
  }

  const data = sweep.data
  const pending = data.entries.reduce(
    (sum, { aggregate }) =>
      sum + aggregate.points_total - aggregate.points_finished - aggregate.points_failed,
    0,
  )

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">
          {data.entries.length} {data.entries.length === 1 ? 'entry' : 'entries'} over{' '}
          {backtests(data.runs.length)}
        </h2>
        <p className="text-sm text-slate-400">
          {data.symbols.join(', ')} · {data.timeframes.join(', ')} · {day(data.date_from)} →{' '}
          {day(data.date_to)} · {money(data.initial_capital)} per run
        </p>
      </div>

      {/* The poll is visible rather than silent: sections of dashes without a word about why
          read as a sweep that produced nothing. */}
      {pending > 0 && (
        <p role="status" className="text-sm text-sky-300">
          {pending} of {backtests(data.runs.length)} still running — this updates on its own.
        </p>
      )}

      <section className="space-y-2">
        <h3 className="font-medium">Equity of the runs you pick, as percent of starting capital</h3>
        <ComparisonChart series={series} />
        <p className="text-xs text-slate-500">
          {series.length === 0
            ? `Tick up to ${String(MAX_COMPARED)} runs in the tables below to compare their curves — across entries too.`
            : `Up to ${String(MAX_COMPARED)} runs stay tellable apart by colour; untick one to add another.`}
        </p>
      </section>

      {data.entries.map((entry) => {
        // By id, from the coordinates the server wrote at launch — never by matching the entry's
        // name against a strategy name, which breaks the day one entry's name prefixes another's.
        const mine = data.runs.filter((row) => row.entry_id === entry.entry_id).map((row) => row.run)
        const heading = `sweep-entry-${entry.entry_id}`
        return (
          <section
            key={entry.entry_id}
            aria-labelledby={heading}
            className="space-y-3 border-t border-slate-800 pt-5"
          >
            <div>
              <h3 id={heading} className="text-lg font-medium text-sky-400">
                {entry.entry_name ?? 'An entry since removed from the shelf'}
              </h3>
              <p className="text-sm text-slate-400">{backtests(mine.length)}</p>
            </div>
            <StudyDispersion aggregate={entry.aggregate} />
            <RunTable runs={mine} seats={seats} onToggle={toggle} />
          </section>
        )
      })}
    </div>
  )
}
