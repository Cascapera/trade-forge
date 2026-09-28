import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCreateSweepWalkForward, useSweepWalkForwards } from '../api/hooks'
import type { HoldoutRank, SweepOut } from '../api/types'

const METRICS: { value: HoldoutRank; label: string }[] = [
  { value: 'net_profit', label: 'Net profit' },
  { value: 'net_r', label: 'Net R' },
  { value: 'recovery_r', label: 'Net R per R of drawdown' },
  { value: 'positive_years', label: 'Share of years positive' },
  { value: 'profit_factor', label: 'Profit factor' },
  { value: 'expectancy', label: 'Expectancy' },
  { value: 'sharpe', label: 'Sharpe' },
]

/** The folds as years, the way the server cuts them (`sweep_walkforward.windows`). */
function planned(
  startYear: number,
  trainYears: number,
  testYears: number,
  folds: number,
  anchored: boolean,
): { train: string; test: string }[] {
  return Array.from({ length: folds }, (_, k) => {
    const testFrom = startYear + trainYears + k * testYears
    const trainFrom = anchored ? startYear : testFrom - trainYears
    return {
      train: `${String(trainFrom)}–${String(testFrom - 1)}`,
      test: `${String(testFrom)}–${String(testFrom + testYears - 1)}`,
    }
  })
}

/**
 * Walk a finished sweep forward (25/09, path B): at each fold the whole sweep runs again on a
 * training window, its best points are chosen by the rule below, and they run on the window after.
 *
 * ⚠️ **Each fold is the whole sweep again** — on the charts ticked (28/09). The cost is said
 * before the button — the sweep's runs times the folds — because the button that starts it is one
 * click and the queue it fills is hours long. With some charts only, the runs are the sweep's
 * share on those charts, estimated as an even split: the sweep does not count its runs per chart.
 *
 * The trade floor is per chart, as the reserved-window test's: blank keeps the sweep's own (60 on
 * M1 and M5), which no run of a quiet setup may reach — and then every fold is refused.
 */
export function SweepWalkForwardLauncher(props: { sweep: SweepOut }): React.JSX.Element {
  const { sweep } = props
  const navigate = useNavigate()
  const create = useCreateSweepWalkForward(sweep.id)
  const existing = useSweepWalkForwards(sweep.id)
  const firstYear = Number(sweep.date_from.slice(0, 4))
  const [startYear, setStartYear] = useState(firstYear)
  const [trainYears, setTrainYears] = useState(6)
  const [testYears, setTestYears] = useState(2)
  const [folds, setFolds] = useState(4)
  const [anchored, setAnchored] = useState(true)
  const [topN, setTopN] = useState(3)
  const [metric, setMetric] = useState<HoldoutRank>('net_profit')
  const [understood, setUnderstood] = useState(false)
  const [charts, setCharts] = useState<string[]>(sweep.timeframes)
  const [floors, setFloors] = useState<Record<string, string>>({})

  const all = sweep.counts?.total ?? sweep.runs.length
  const some = charts.length < sweep.timeframes.length
  const runs = some ? Math.round((all * charts.length) / sweep.timeframes.length) : all
  const cost = runs * folds
  const floorsValid = charts.every(
    (chart) => (floors[chart] ?? '') === '' || /^[1-9]\d*$/.test(floors[chart] ?? ''),
  )
  const windows = planned(startYear, trainYears, testYears, folds, anchored)
  const lastTest = startYear + trainYears + folds * testYears - 1
  const thisYear = new Date().getUTCFullYear()
  const why =
    !(Number.isInteger(folds) && folds >= 2 && folds <= 20)
      ? 'Between 2 and 20 folds.'
      : trainYears < 1 || testYears < 1
        ? 'Each window is at least a year.'
        : startYear + trainYears > thisYear
          ? 'The first test would start in the future.'
          : charts.length === 0
            ? 'Tick at least one chart.'
            : !floorsValid
              ? 'A trade floor is a whole number of at least 1.'
              : !understood
                ? 'Confirm the cost first.'
                : null

  const launch = (): void => {
    const minTrades: Record<string, number> = {}
    for (const chart of charts) {
      const value = floors[chart] ?? ''
      if (value !== '') minTrades[chart] = Number(value)
    }
    create.mutate(
      {
        start_year: startYear,
        train_years: trainYears,
        test_years: testYears,
        folds,
        anchored,
        top_n: topN,
        metric,
        ...(Object.keys(minTrades).length > 0 ? { min_trades: minTrades } : {}),
        // In the sweep's order; left out when every chart walks, as before 28/09.
        ...(some ? { timeframes: sweep.timeframes.filter((one) => charts.includes(one)) } : {}),
      },
      {
        onSuccess: (made) => {
          void navigate(`/sweep-walkforwards/${made.id}`)
        },
      },
    )
  }

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1'
  const number = (label: string, value: number, set: (value: number) => void) => (
    <label className="flex flex-col gap-1">
      {label}
      <input
        type="number"
        value={value}
        onChange={(event) => {
          set(Number(event.target.value))
          setUnderstood(false)
        }}
        className={`${input} w-24`}
      />
    </label>
  )

  return (
    <section
      aria-label="walk forward"
      className="space-y-3 rounded border border-slate-800 p-4"
    >
      <div>
        <h3 className="font-semibold">Walk forward (re-choose every fold)</h3>
        <p className="text-sm text-slate-400">
          At each fold the whole sweep runs again on its training years, its best points are chosen
          by the rule below, and those run on the years right after. It judges the way of choosing,
          not one choice.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4 text-sm">
        {number('First training year', startYear, setStartYear)}
        {number('Training years', trainYears, setTrainYears)}
        {number('Test years', testYears, setTestYears)}
        {number('Folds', folds, setFolds)}
        <label className="flex items-center gap-2">
          <input
            type="checkbox"
            checked={anchored}
            onChange={(event) => {
              setAnchored(event.target.checked)
            }}
          />
          Anchored (training grows from the first year)
        </label>
        {number('Best per chart', topN, setTopN)}
        <label className="flex flex-col gap-1">
          Ranked by
          <select
            value={metric}
            onChange={(event) => {
              setMetric(event.target.value as HoldoutRank)
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
          Charts to walk, and the fewest trades to be chosen on each — blank keeps the sweep&apos;s
          floor
        </legend>
        <div className="flex flex-wrap gap-4">
          {sweep.timeframes.map((chart) => (
            <span key={chart} className="flex items-center gap-2">
              <label className="flex items-center gap-1">
                <input
                  type="checkbox"
                  checked={charts.includes(chart)}
                  onChange={(event) => {
                    const on = event.target.checked
                    setCharts((current) =>
                      on ? [...current, chart] : current.filter((one) => one !== chart),
                    )
                    setUnderstood(false)
                  }}
                />
                {chart}
              </label>
              <input
                aria-label={`fewest trades on ${chart}`}
                inputMode="numeric"
                placeholder="default"
                disabled={!charts.includes(chart)}
                value={floors[chart] ?? ''}
                onChange={(event) => {
                  setFloors((current) => ({ ...current, [chart]: event.target.value }))
                }}
                className={`${input} w-20 disabled:opacity-40`}
              />
            </span>
          ))}
        </div>
      </fieldset>

      <p className="text-xs text-slate-400">
        {windows.map((one, k) => `fold ${String(k + 1)}: train ${one.train} → test ${one.test}`).join(' · ')}
        {lastTest > thisYear && ` · the last test reaches ${String(lastTest)} and stops at today`}
      </p>

      <label className="flex items-center gap-2 text-sm text-amber-300">
        <input
          type="checkbox"
          checked={understood}
          onChange={(event) => {
            setUnderstood(event.target.checked)
          }}
        />
        Queues about {cost.toLocaleString('en-US')} training runs ({String(runs)} × {String(folds)}{' '}
        folds{some && `, on ${charts.join(', ')} only — an even share of the sweep's ${String(all)}`}
        ), plus each fold&apos;s tests.
      </label>

      {why !== null && <p className="text-xs text-amber-300">{why}</p>}
      {create.isError && (
        <p role="alert" className="text-sm text-red-400">
          {apiFailure(create.error, 'Could not start the walk-forward.')}
        </p>
      )}
      <button
        type="button"
        disabled={why !== null || create.isPending}
        onClick={launch}
        className="rounded bg-sky-600 px-4 py-2 text-sm font-medium disabled:opacity-40"
      >
        {create.isPending ? 'Starting…' : 'Walk forward'}
      </button>

      {existing.data !== undefined && existing.data.length > 0 && (
        <ul className="text-sm">
          {existing.data.map((one) => (
            <li key={one.id}>
              <Link to={`/sweep-walkforwards/${one.id}`} className="text-sky-400 hover:text-sky-300">
                {one.folds.length} folds from {one.start_year}, {one.train_years}y train /{' '}
                {one.test_years}y test
              </Link>{' '}
              <span className="text-slate-500">· {one.status}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
