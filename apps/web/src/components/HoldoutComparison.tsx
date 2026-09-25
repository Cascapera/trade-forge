import { Link } from 'react-router-dom'

import { isHoldoutSettled, useHoldout } from '../api/hooks'
import type { HoldoutRank, HoldoutRow, HoldoutSide } from '../api/types'
import { percent } from '../format'
import { HoldoutMonteCarlo } from './HoldoutMonteCarlo'
import { HoldoutSlicings } from './HoldoutSlicings'

const METRIC_LABEL: Record<HoldoutRank, string> = {
  net_profit: 'net profit',
  profit_factor: 'profit factor',
  sharpe: 'Sharpe',
  expectancy: 'expectancy',
  net_r: 'net R',
  recovery_r: 'net R per R of drawdown',
  positive_years: 'share of years positive',
}

const CHART_ORDER = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']

function day(iso: string): string {
  return iso.slice(0, 10)
}

/** Charts from the shortest bar up, the way every other screen lists them. */
function byChart(left: string, right: string): number {
  return CHART_ORDER.indexOf(left) - CHART_ORDER.indexOf(right)
}

function tone(fraction: string | null): string {
  if (fraction === null) return 'text-slate-500'
  const value = Number(fraction)
  if (value > 0) return 'text-emerald-400'
  if (value < 0) return 'text-red-400'
  return 'text-slate-300'
}

function Result(props: { side: HoldoutSide | null }): React.JSX.Element {
  const { side } = props
  if (side === null) return <span className="text-slate-500">—</span>
  if (side.status !== 'done') return <span className="text-slate-500">{side.status}</span>
  return (
    <span className={tone(side.net_return)}>
      {percent(side.net_return)}{' '}
      <span className="text-slate-500">({String(side.total_trades ?? 0)} tr)</span>
    </span>
  )
}

function sortRows(rows: HoldoutRow[]): HoldoutRow[] {
  return [...rows].sort(
    (left, right) =>
      (left.entry_name ?? '').localeCompare(right.entry_name ?? '') ||
      byChart(left.timeframe, right.timeframe) ||
      left.symbol.localeCompare(right.symbol) ||
      left.label.localeCompare(right.label),
  )
}

/**
 * A reserved-window test beside the sweep it came from (24/09).
 *
 * ⚠️ **Medians first, and the share still positive beside them — never the best.** The best
 * out-of-sample run is again the best of several draws. Whether the method held is what the median
 * and the share of points still making money say; the rows below are there to be read after.
 */
export function HoldoutComparison(props: { sweepId: string }): React.JSX.Element {
  const holdout = useHoldout(props.sweepId)

  if (holdout.isPending) return <p className="text-slate-400">Loading the comparison…</p>
  if (holdout.isError) return <p className="text-red-400">Could not load the comparison.</p>

  const data = holdout.data
  const groups = [...data.groups].sort(
    (left, right) =>
      (left.entry_name ?? '').localeCompare(right.entry_name ?? '') ||
      byChart(left.timeframe, right.timeframe),
  )
  const floors = Object.entries(data.rule.min_trades)
    .sort(([left], [right]) => byChart(left, right))
    .map(([chart, floor]) => `${chart} ${String(floor)}`)
    .join(', ')

  return (
    <section aria-label="reserved-window test" className="space-y-4">
      <div className="rounded border border-slate-800 p-4 text-sm">
        <p>
          Reserved-window test of{' '}
          {data.holdout_of === null ? (
            <span className="text-slate-400">a sweep since deleted</span>
          ) : (
            <Link to={`/sweeps/${data.holdout_of}`} className="text-sky-400 hover:text-sky-300">
              the sweep it came from
            </Link>
          )}
          .
        </p>
        <p className="text-slate-400">
          Chosen on {data.searched_from === null ? '—' : day(data.searched_from)} →{' '}
          {data.searched_to === null ? '—' : day(data.searched_to)} · tested on{' '}
          {day(data.date_from)} → {day(data.date_to)} · best {String(data.rule.top_n)} per entry,
          chart and market by {METRIC_LABEL[data.rule.metric]} · fewest trades: {floors}
          {data.rule.max_drawdown_r !== undefined &&
            ` · drawdown at most ${data.rule.max_drawdown_r} R`}
          {data.rule.min_positive_year_share !== undefined &&
            ` · at least ${percent(data.rule.min_positive_year_share, 0)} of years positive`}
        </p>
      </div>

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">By entry and chart</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Entry
            </th>
            <th scope="col" className="px-3 py-2">
              Chart
            </th>
            <th scope="col" className="px-3 py-2">
              Where chosen (median)
            </th>
            <th scope="col" className="px-3 py-2">
              Reserved window (median)
            </th>
            <th scope="col" className="px-3 py-2">
              Still positive
            </th>
            <th scope="col" className="px-3 py-2">
              Done
            </th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <tr
              key={`${group.entry_id}-${group.timeframe}`}
              className="border-b border-slate-900"
            >
              <td className="px-3 py-2">{group.entry_name ?? '(removed entry)'}</td>
              <td className="px-3 py-2">{group.timeframe}</td>
              <td className={`px-3 py-2 ${tone(group.in_sample_median_return)}`}>
                {percent(group.in_sample_median_return)}
              </td>
              <td className={`px-3 py-2 ${tone(group.out_of_sample_median_return)}`}>
                {percent(group.out_of_sample_median_return)}
              </td>
              <td className="px-3 py-2">{percent(group.out_of_sample_positive, 0)}</td>
              <td className="px-3 py-2 text-slate-400">
                {String(group.done)} / {String(group.points)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">Point by point</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Entry
            </th>
            <th scope="col" className="px-3 py-2">
              Market
            </th>
            <th scope="col" className="px-3 py-2">
              Point
            </th>
            <th scope="col" className="px-3 py-2">
              Where chosen
            </th>
            <th scope="col" className="px-3 py-2">
              Reserved window
            </th>
          </tr>
        </thead>
        <tbody>
          {sortRows(data.rows).map((row) => (
            <tr key={row.out_of_sample.run_id} className="border-b border-slate-900">
              <td className="px-3 py-2">{row.entry_name ?? '(removed entry)'}</td>
              <td className="px-3 py-2">{row.symbol}</td>
              <td className="px-3 py-2 text-slate-300">{row.label}</td>
              <td className="px-3 py-2">
                <Result side={row.in_sample} />
              </td>
              <td className="px-3 py-2">
                <Link to={`/results/${row.out_of_sample.run_id}`} className="hover:underline">
                  <Result side={row.out_of_sample} />
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <HoldoutSlicings sweepId={data.id} settled={isHoldoutSettled(data)} />

      <HoldoutMonteCarlo sweepId={data.id} settled={isHoldoutSettled(data)} />
    </section>
  )
}
