import { useMemo, useState } from 'react'

import { useBacktests, useEquityCurves, useInstruments } from '../api/hooks'
import type { BacktestFilters, BacktestStatus } from '../api/types'
import {
  EMPTY_SEATS,
  MAX_COMPARED,
  buildSeries,
  costlessAmong,
  isFull,
  runLabel,
  selectedIds,
  toggleSeat,
} from '../backtest/compare'
import { ComparisonChart } from '../components/ComparisonChart'
import { RunTable } from '../components/RunTable'
import { count } from '../format'
import { TIMEFRAMES } from '../strategy/builder'

const STATUSES: readonly BacktestStatus[] = ['queued', 'running', 'done', 'failed']

// "All" has to reach the API as an *absent* filter, not an empty one: `?symbol=` asks for runs
// whose symbol is the empty string and matches nothing. The select's own value is `''`, so this
// is where the two are told apart.
function chosen(value: string): string | undefined {
  return value === '' ? undefined : value
}

function Field(props: {
  label: string
  value: string
  onChange: (value: string) => void
  options: readonly string[]
}): React.JSX.Element {
  return (
    <label className="flex flex-col gap-1 text-xs text-slate-400">
      {props.label}
      <select
        value={props.value}
        onChange={(event) => {
          props.onChange(event.target.value)
        }}
        className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
      >
        <option value="">All</option>
        {props.options.map((option) => (
          <option key={option} value={option}>
            {option}
          </option>
        ))}
      </select>
    </label>
  )
}

/**
 * Every run that has been launched, and a comparison of the ones picked out of it.
 *
 * The screen this project needed once there were thirty-six runs rather than three: opening them
 * one at a time by id told you about one experiment, and the question being asked — does this
 * method work, and on which instrument — is a question about the *set* of them.
 */
export function RunLog(): React.JSX.Element {
  const [symbol, setSymbol] = useState('')
  const [timeframe, setTimeframe] = useState('')
  const [status, setStatus] = useState('')
  const [seats, setSeats] = useState(EMPTY_SEATS)

  const filters: BacktestFilters = useMemo(() => {
    const next: BacktestFilters = {}
    const s = chosen(symbol)
    const t = chosen(timeframe)
    const st = chosen(status)
    if (s !== undefined) next.symbol = s
    if (t !== undefined) next.timeframe = t
    if (st !== undefined) next.status = st as BacktestStatus
    return next
  }, [symbol, timeframe, status])

  const page = useBacktests(filters)
  const instruments = useInstruments()

  // The ids, not the runs: this is the `useQueries` key list, and it must be stable across a
  // refetch of the page or every curve would be requested again.
  const picked = useMemo(() => selectedIds(seats), [seats])
  const { curves, isPending: curvesPending } = useEquityCurves(picked)

  const runs = useMemo(() => page.data?.items ?? [], [page.data])
  const series = useMemo(() => buildSeries(seats, runs, curves), [seats, runs, curves])
  const costless = useMemo(() => costlessAmong(seats, runs), [seats, runs])

  function toggle(id: string): void {
    setSeats((current) => toggleSeat(current, id))
  }

  if (page.isPending) {
    return <p className="text-slate-400">Loading the run log…</p>
  }
  if (page.isError) {
    return <p className="text-red-400">Could not load the run log.</p>
  }

  const total = page.data.total

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">Run log</h2>
          <p className="text-sm text-slate-400">
            {count(total)} run{total === 1 ? '' : 's'}, newest first. Tick up to{' '}
            {String(MAX_COMPARED)} to compare their equity curves.
          </p>
        </div>
        {/* Filters in one row above the data they filter. */}
        <div className="flex flex-wrap gap-3">
          <Field
            label="Symbol"
            value={symbol}
            onChange={setSymbol}
            options={(instruments.data ?? []).map((one) => one.symbol)}
          />
          <Field label="Timeframe" value={timeframe} onChange={setTimeframe} options={TIMEFRAMES} />
          <Field label="Status" value={status} onChange={setStatus} options={STATUSES} />
        </div>
      </div>

      {costless.length > 0 && (
        <p
          role="status"
          className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
        >
          {costless.length === picked.length ? 'Every run' : `${String(costless.length)} of these`}{' '}
          charged <strong>no spread and no commission</strong> —{' '}
          {costless.map((run) => runLabel(run)).join(', ')}. Those curves are an upper bound, not a
          P&amp;L, and they are not comparable with a run that paid costs. It matters least on a
          wide stop and most on a short intraday one, where a pip of spread can be a real fraction
          of the R.
        </p>
      )}

      <section className="space-y-2">
        <h3 className="font-medium">
          Equity, indexed to percent of starting capital
          {series.length > 1 && <span className="text-slate-400"> — aligned on real dates</span>}
        </h3>
        {curvesPending && picked.length > 0 && (
          <p className="text-sm text-slate-400">Loading {String(picked.length)} curve(s)…</p>
        )}
        <ComparisonChart series={series} />
        {isFull(seats) && (
          <p className="text-xs text-slate-500">
            Comparing {String(MAX_COMPARED)} runs, the most that stay tellable apart by colour.
            Untick one to add another.
          </p>
        )}
      </section>

      {runs.length === 0 ? (
        <p className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-sm text-slate-400">
          No runs match these filters.
        </p>
      ) : (
        <RunTable runs={runs} seats={seats} onToggle={toggle} />
      )}
    </div>
  )
}
