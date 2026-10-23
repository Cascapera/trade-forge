import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useEquityCurves, useStudy } from '../api/hooks'
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
import { StudyHeatmap } from '../components/StudyHeatmap'
import { WalkForwardLauncher } from '../components/WalkForwardLauncher'
import { money } from '../format'

/** The calendar day of an ISO instant — the granularity a window is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

/**
 * One study: the shape of its result across the parameters it searched.
 *
 * The reading order here is deliberate and is the opposite of what an optimiser usually offers.
 * The dispersion comes first, led by the **median** — what a parameter set chosen without
 * hindsight would have returned. The heatmap comes second, to be read for the *shape* of the
 * good region: a broad plateau tolerates being slightly wrong about its parameters, a single
 * bright cell is the luckiest arrangement of the same noise. The best point is a row in a table
 * like every other row.
 *
 * ⚠️ **Nothing seats itself on the chart, unlike a basket, and that is the difference in scale.**
 * A basket is a handful of markets and seating each as it lands is a service; a study is up to
 * five hundred points, where the same rule would fill every seat with whichever combinations
 * happened to finish first. Here the reader picks the two or three worth comparing — the best
 * against the median is the comparison that answers whether the winner is really different.
 */
export function StudyResult(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  const study = useStudy(id)
  const [seats, setSeats] = useState(EMPTY_SEATS)

  const runs = useMemo(() => study.data?.runs ?? [], [study.data])
  const picked = useMemo(() => selectedIds(seats), [seats])
  const { curves } = useEquityCurves(picked)
  const series = useMemo(() => buildSeries(seats, runs, curves), [seats, runs, curves])

  function toggle(runId: string): void {
    setSeats((current) => toggleSeat(current, runId))
  }

  if (study.isPending) return <p className="text-slate-400">Loading the study…</p>
  if (study.isError) {
    return <p className="text-red-400">Could not load this study.</p>
  }

  const data = study.data
  const settled = data.aggregate.points_finished + data.aggregate.points_failed
  const pending = data.aggregate.points_total - settled

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">
          <span className="text-sky-400">{data.strategy_name}</span> over{' '}
          {data.aggregate.points_total} parameter combinations
        </h2>
        <p className="text-sm text-slate-400">
          {data.symbol} · {data.timeframe} · {day(data.date_from)} → {day(data.date_to)} ·{' '}
          {money(data.initial_capital)} per point
        </p>
      </div>

      {/* The poll is visible rather than silent: a screen showing dashes without saying why
          reads as a study that produced nothing. */}
      {pending > 0 && (
        <p role="status" className="text-sm text-sky-300">
          {pending} of {data.aggregate.points_total} points still running — this updates on its own.
        </p>
      )}

      <StudyDispersion aggregate={data.aggregate} />

      <StudyHeatmap study={data} />

      <section className="space-y-2">
        <h3 className="font-medium">
          Equity of the points you pick, as percent of starting capital
        </h3>
        <ComparisonChart series={series} />
        <p className="text-xs text-slate-500">
          {series.length === 0
            ? `Tick up to ${String(MAX_COMPARED)} points in the table below to compare their curves — the best against the median is the comparison worth making.`
            : `Up to ${String(MAX_COMPARED)} points stay tellable apart by colour; untick one to add another.`}
        </p>
      </section>

      <RunTable runs={runs} seats={seats} onToggle={toggle} />

      {/* Last on the screen, and that is the reading order the whole feature depends on: the
          dispersion, then the shape, then the runs — and only then the experiment that can say
          whether any of it survives being chosen without hindsight. */}
      <WalkForwardLauncher study={data} />
    </div>
  )
}
