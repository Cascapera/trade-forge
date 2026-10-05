import { Link } from 'react-router-dom'

import { useBestCell } from '../api/hooks'
import type { BestMetric, BestPointOut } from '../api/types'
import { fillFor } from '../backtest/study'
import { formatValue, METRICS } from '../best/map'
import { count, inR, percent, ratio } from '../format'

export interface ChosenCell {
  symbol: string
  entryId: string
  entryName: string
  timeframe: string
}

/**
 * One cell's top runs, beside the map: as a reserved-window test would choose them — no clone and
 * no point whose entries nest in a better one's (#370/#371) — so the ten are ten different bets.
 *
 * ⚠️ **Nothing here is validated.** Each point says what its reserved-window tests found, or that
 * none was run; the map's best is the best of `ranked` draws over one window.
 */
export function BestCellPanel({
  cell,
  metric,
  everyRun,
  onClose,
}: {
  cell: ChosenCell
  metric: BestMetric
  everyRun: boolean
  onClose: () => void
}): React.JSX.Element {
  const top = useBestCell({ ...cell, metric, everyRun })
  const metricLabel = METRICS.find((one) => one.key === metric)?.label ?? metric

  return (
    <aside
      aria-label={`Best runs of ${cell.entryName} on ${cell.symbol} ${cell.timeframe}`}
      className="fixed inset-y-0 right-0 z-20 w-full max-w-2xl overflow-y-auto border-l border-slate-800 bg-slate-950 p-6 shadow-2xl"
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">
            {cell.symbol} · {cell.timeframe}
          </h2>
          <p className="text-sm text-slate-400">
            {cell.entryName} — top by {metricLabel}, without clones or near-clones
          </p>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="rounded border border-slate-700 px-2 py-1 text-sm text-slate-300 hover:border-slate-500"
        >
          Close
        </button>
      </div>

      {top.isPending && <p className="mt-6 text-sm text-slate-400">Loading…</p>}
      {top.isError && (
        <p role="alert" className="mt-6 text-sm text-amber-300">
          Could not read this cell: {top.error.message}
        </p>
      )}
      {top.data !== undefined && (
        <>
          <p className="mt-4 text-xs text-slate-400">
            {count(top.data.ranked)} run{top.data.ranked === 1 ? '' : 's'} ranked in this cell — the
            best of them is partly the luck of the search. In sample, not validated.
          </p>
          {top.data.points.length === 0 ? (
            <p className="mt-6 text-sm text-slate-400">No run of this cell can be ranked.</p>
          ) : (
            <ol className="mt-4 space-y-3">
              {top.data.points.map((point, index) => (
                <Point key={point.run_id} point={point} place={index + 1} metric={metric} />
              ))}
            </ol>
          )}
        </>
      )}
    </aside>
  )
}

function Point({
  point,
  place,
  metric,
}: {
  point: BestPointOut
  place: number
  metric: BestMetric
}): React.JSX.Element {
  return (
    <li className="rounded border border-slate-800 p-3">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-sm text-slate-100">
          <span className="text-slate-500">#{place}</span>{' '}
          <span className="font-medium tabular-nums">
            {formatValue(point.value, metric, point.unbounded)}
          </span>
        </span>
        <span className="flex gap-3 text-xs">
          <Link to={`/results/${point.run_id}`} className="text-sky-400 hover:text-sky-300">
            Run
          </Link>
          {point.sweep_id !== null && (
            <Link to={`/sweeps/${point.sweep_id}`} className="text-sky-400 hover:text-sky-300">
              Sweep
            </Link>
          )}
        </span>
      </div>
      <p className="mt-1 break-words text-xs text-slate-400">{point.label || 'no parameters'}</p>
      <dl className="mt-2 grid grid-cols-3 gap-x-4 gap-y-1 text-xs tabular-nums sm:grid-cols-6">
        <Measure term="Net R" value={inR(point.net_r)} />
        <Measure term="R / year" value={inR(point.net_r_per_year)} />
        <Measure
          term="R / DD"
          value={point.unbounded && metric === 'recovery_r' ? '∞' : ratio(point.recovery_r, 1)}
        />
        <Measure term="Years +" value={percent(point.positive_year_share, 0)} />
        <Measure term="Months +" value={percent(point.positive_month_share ?? null, 0)} />
        <Measure
          term="DD"
          value={point.max_drawdown_r === null ? '—' : `${ratio(point.max_drawdown_r, 1)} R`}
        />
        <Measure term="Trades" value={count(point.total_trades)} />
        <Measure term="Return" value={percent(point.return_pct ?? null, 1)} />
        <Measure term="CAGR" value={percent(point.cagr ?? null, 1)} />
        <Measure
          term="PF"
          value={
            point.unbounded && metric === 'profit_factor' ? '∞' : ratio(point.profit_factor, 2)
          }
        />
        <Measure term="Win rate" value={percent(point.win_rate ?? null, 0)} />
        <Measure term="Sharpe" value={ratio(point.sharpe ?? null, 1)} />
        <Measure term="Worst year" value={inR(point.worst_year_r ?? null)} />
      </dl>
      <Years yearly={point.yearly_r} />
      <Tests point={point} />
    </li>
  )
}

function Measure({ term, value }: { term: string; value: string }): React.JSX.Element {
  return (
    <div>
      <dt className="text-slate-500">{term}</dt>
      <dd className="text-slate-200">{value}</dd>
    </div>
  )
}

/** R by year as a strip of cells, coloured on the map's scale, each with its number on hover and
 *  for a screen reader — colour is never the only encoding. */
function Years({ yearly }: { yearly: Record<string, string> }): React.JSX.Element | null {
  const years = Object.keys(yearly).sort()
  if (years.length === 0) return null
  const extent = Math.max(...years.map((year) => Math.abs(Number(yearly[year]))), 0)
  return (
    <div className="mt-2">
      <p className="sr-only">R by year</p>
      <ul className="flex flex-wrap gap-0.5">
        {years.map((year) => {
          const value = yearly[year] ?? '0'
          return (
            <li
              key={year}
              title={`${year}: ${inR(value)}`}
              className="flex h-6 min-w-9 items-center justify-center rounded-sm px-1 text-[10px] text-slate-100 tabular-nums"
              style={{ backgroundColor: fillFor(Number(value), extent) }}
            >
              <span>{year.slice(2)}</span>
              <span className="sr-only">: {inR(value)}</span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}

/** What the reserved-window tests found for this point — the only validation the page names. */
function Tests({ point }: { point: BestPointOut }): React.JSX.Element {
  if (point.tests.length === 0) {
    return <p className="mt-2 text-xs text-slate-500">Not validated: never tested out of sample.</p>
  }
  return (
    <ul className="mt-2 space-y-0.5 text-xs text-slate-300">
      {point.tests.map((test) => (
        <li key={`${test.sweep_id ?? ''}-${test.date_from}`}>
          Tested {test.date_from.slice(0, 10)} → {test.date_to.slice(0, 10)}:{' '}
          {test.net_r === null
            ? test.status
            : `${inR(test.net_r)} over ${count(test.total_trades ?? 0)} trades`}
          {test.sweep_id !== null && (
            <>
              {' '}
              <Link to={`/sweeps/${test.sweep_id}`} className="text-sky-400 hover:text-sky-300">
                comparison
              </Link>
            </>
          )}
        </li>
      ))}
    </ul>
  )
}
