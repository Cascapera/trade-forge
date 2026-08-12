import { useEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'

import { useBasket, useEquityCurves } from '../api/hooks'
import {
  EMPTY_SEATS,
  MAX_COMPARED,
  buildSeries,
  costlessAmong,
  runLabel,
  selectedIds,
  toggleSeat,
} from '../backtest/compare'
import { newlyComparable } from '../basket/settings'
import { BasketDispersion } from '../components/BasketDispersion'
import { ComparisonChart } from '../components/ComparisonChart'
import { RunTable } from '../components/RunTable'
import { money } from '../format'

/** The calendar day of an ISO instant — the granularity a window is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

/**
 * One basket: how far apart its markets landed, and their curves on one axis.
 *
 * The table and the chart are the run log's own components, unchanged, because the API hands
 * back the same row shape from the same builder. A basket that grew its own idea of a run would
 * drift from the log's the first time a column was added, and the two screens would then
 * disagree about what a run *is* while both looking right.
 *
 * ⚠️ The curves are drawn but never **added**. Every run started with the whole capital, so a
 * summed line would describe an account nobody has — see `BasketDispersion`.
 */
export function BasketResult(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  const basket = useBasket(id)
  const [seats, setSeats] = useState(EMPTY_SEATS)

  // Every run this screen has already put on the chart once. A ref, not state: changing it must
  // not re-render, and it is read inside the effect that changes the seats.
  const offered = useRef(new Set<string>())

  const runs = useMemo(() => basket.data?.runs ?? [], [basket.data])

  useEffect(() => {
    const fresh = newlyComparable(runs, offered.current)
    if (fresh.length === 0) return
    for (const run of fresh) offered.current.add(run.id)
    setSeats((current) => fresh.reduce((acc, run) => toggleSeat(acc, run.id), current))
  }, [runs])

  const picked = useMemo(() => selectedIds(seats), [seats])
  const { curves } = useEquityCurves(picked)
  const series = useMemo(() => buildSeries(seats, runs, curves), [seats, runs, curves])
  const costless = useMemo(() => costlessAmong(seats, runs), [seats, runs])

  function toggle(runId: string): void {
    setSeats((current) => toggleSeat(current, runId))
  }

  if (basket.isPending) {
    return <p className="text-slate-400">Loading the basket…</p>
  }
  if (basket.isError) {
    return <p className="text-red-400">Could not load this basket.</p>
  }

  const data = basket.data
  const pending = data.aggregate.runs_total - data.aggregate.runs_finished - data.aggregate.runs_failed

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">
          <span className="text-sky-400">{data.strategy_name}</span> v{data.strategy_version} across{' '}
          {data.aggregate.runs_total} markets
        </h2>
        <p className="text-sm text-slate-400">
          {data.timeframe} · {day(data.date_from)} → {day(data.date_to)} · {money(data.initial_capital)}{' '}
          per market
        </p>
      </div>

      {/* The poll is visible rather than silent: a screen that showed dashes without saying why
          reads as a basket that produced nothing. */}
      {pending > 0 && (
        <p role="status" className="text-sm text-sky-300">
          {pending} of {data.aggregate.runs_total} markets still running — this updates on its own.
        </p>
      )}

      <BasketDispersion aggregate={data.aggregate} />

      {costless.length > 0 && (
        <p
          role="status"
          className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
        >
          {costless.length === picked.length
            ? 'Every market on this chart'
            : `${String(costless.length)} of these`}{' '}
          charged <strong>no spread and no commission</strong> —{' '}
          {costless.map((run) => runLabel(run)).join(', ')}. Nobody has measured a spread for those
          instruments, so those curves are an upper bound and are not comparable with the markets
          in this basket that did pay one.
        </p>
      )}

      <section className="space-y-2">
        <h3 className="font-medium">
          Equity by market, indexed to percent of starting capital
          {series.length > 1 && <span className="text-slate-400"> — aligned on real dates</span>}
        </h3>
        <ComparisonChart series={series} />
        {data.aggregate.runs_total > MAX_COMPARED && (
          <p className="text-xs text-slate-500">
            This basket holds more markets than the {MAX_COMPARED} that stay tellable apart by
            colour. The first {MAX_COMPARED} to finish are on the chart; untick one to add another.
          </p>
        )}
      </section>

      <RunTable runs={runs} seats={seats} onToggle={toggle} />
    </div>
  )
}
