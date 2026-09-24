import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCreateHoldout } from '../api/hooks'
import type { SelectionMetric, SweepOut } from '../api/types'

const METRICS: { value: SelectionMetric; label: string }[] = [
  { value: 'net_profit', label: 'Net profit' },
  { value: 'profit_factor', label: 'Profit factor' },
  { value: 'sharpe', label: 'Sharpe' },
  { value: 'expectancy', label: 'Expectancy' },
]

/** The calendar day of an ISO instant, which is what a date input holds. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

/** The instant a date input's day starts, in UTC — how the sweep's own windows are written. */
function startOf(value: string): string {
  return `${value}T00:00:00Z`
}

/**
 * Test a finished sweep's best points on a window none of them was chosen on (24/09).
 *
 * ⚠️ **The window starts where the search ended, by default, and the server refuses any overlap.**
 * A test that shares bars with the search is partly the search again. The default is only where
 * the first honest window can begin; the reader moves the end to however much data exists.
 *
 * The trade floor is per chart and blank means the sweep's own — 30 on the short charts, 1 above
 * them, which is too low to rank on D1 and W1 and is why the field exists. The floor actually used
 * is shown on the test once it runs, so a blank here never hides what was applied.
 */
export function HoldoutLauncher(props: { sweep: SweepOut }): React.JSX.Element {
  const { sweep } = props
  const navigate = useNavigate()
  const create = useCreateHoldout(sweep.id)
  const [dateFrom, setDateFrom] = useState(day(sweep.date_to))
  const [dateTo, setDateTo] = useState(day(new Date().toISOString()))
  const [topN, setTopN] = useState(3)
  const [metric, setMetric] = useState<SelectionMetric>('net_profit')
  const [floors, setFloors] = useState<Record<string, string>>({})

  const overlaps = dateFrom < day(sweep.date_to) && day(sweep.date_from) < dateTo
  const backwards = dateTo <= dateFrom
  const floorsValid = Object.values(floors).every(
    (value) => value === '' || (/^\d+$/.test(value) && Number(value) >= 1),
  )
  const blocked = overlaps || backwards || !floorsValid || topN < 1 || create.isPending

  function launch(): void {
    const minTrades: Record<string, number> = {}
    for (const [timeframe, value] of Object.entries(floors)) {
      if (value !== '') minTrades[timeframe] = Number(value)
    }
    create.mutate(
      {
        date_from: startOf(dateFrom),
        date_to: startOf(dateTo),
        top_n: topN,
        metric,
        min_trades: minTrades,
      },
      {
        onSuccess: (created) => {
          void navigate(`/sweeps/${created.id}`)
        },
      },
    )
  }

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1'
  return (
    <section
      aria-label="test on a reserved window"
      className="space-y-3 rounded border border-slate-800 p-4"
    >
      <div>
        <h3 className="font-semibold">Test on a reserved window</h3>
        <p className="text-sm text-slate-400">
          Run the best points of each entry, chart and market again over a window none of them was
          chosen on. The only second opinion a sweep can get.
        </p>
      </div>
      <div className="flex flex-wrap items-end gap-4 text-sm">
        <label className="flex flex-col gap-1">
          From
          <input
            type="date"
            value={dateFrom}
            onChange={(event) => {
              setDateFrom(event.target.value)
            }}
            className={input}
          />
        </label>
        <label className="flex flex-col gap-1">
          To
          <input
            type="date"
            value={dateTo}
            onChange={(event) => {
              setDateTo(event.target.value)
            }}
            className={input}
          />
        </label>
        <label className="flex flex-col gap-1">
          Best per chart
          <input
            type="number"
            min={1}
            max={100}
            value={topN}
            onChange={(event) => {
              setTopN(Number(event.target.value))
            }}
            className={`${input} w-20`}
          />
        </label>
        <label className="flex flex-col gap-1">
          Ranked by
          <select
            value={metric}
            onChange={(event) => {
              setMetric(event.target.value as SelectionMetric)
            }}
            className={input}
          >
            {METRICS.map((one) => (
              <option key={one.value} value={one.value}>
                {one.label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <fieldset className="text-sm">
        <legend className="mb-1 text-slate-400">
          Fewest trades to be ranked, per chart — blank keeps the sweep&apos;s floor
        </legend>
        <div className="flex flex-wrap gap-3">
          {sweep.timeframes.map((timeframe) => (
            <label key={timeframe} className="flex items-center gap-1">
              {timeframe}
              <input
                aria-label={`fewest trades on ${timeframe}`}
                inputMode="numeric"
                placeholder="default"
                value={floors[timeframe] ?? ''}
                onChange={(event) => {
                  setFloors((current) => ({ ...current, [timeframe]: event.target.value }))
                }}
                className={`${input} w-20`}
              />
            </label>
          ))}
        </div>
      </fieldset>
      {overlaps && (
        <p role="alert" className="text-sm text-amber-300">
          This window shares bars with the one searched ({day(sweep.date_from)} →{' '}
          {day(sweep.date_to)}): it would not be out of sample.
        </p>
      )}
      {backwards && (
        <p role="alert" className="text-sm text-amber-300">
          The window has to end after it starts.
        </p>
      )}
      {!floorsValid && (
        <p role="alert" className="text-sm text-amber-300">
          A trade floor is a whole number of at least 1.
        </p>
      )}
      {create.isError && (
        <p role="alert" className="text-sm text-red-400">
          {apiFailure(create.error, 'Could not launch the test.')}
        </p>
      )}
      <button
        type="button"
        disabled={blocked}
        onClick={launch}
        className="rounded bg-sky-600 px-4 py-2 text-sm font-medium disabled:opacity-40"
      >
        {create.isPending ? 'Launching…' : 'Run the test'}
      </button>
    </section>
  )
}
