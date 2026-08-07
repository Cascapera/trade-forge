import { Link } from 'react-router-dom'

import type { BacktestListItem, BacktestStatus } from '../api/types'
import {
  MAX_COMPARED,
  type Seats,
  colorOf,
  compareLabel,
  costLabel,
  isFull,
  isSelected,
} from '../backtest/compare'
import { count, percent, ratio, sign, signedMoney } from '../format'

const badge: Record<BacktestStatus, string> = {
  queued: 'bg-slate-700 text-slate-200',
  running: 'bg-sky-900 text-sky-200',
  done: 'bg-emerald-900 text-emerald-200',
  failed: 'bg-red-900 text-red-200',
}

const toneClass = { up: 'text-emerald-400', down: 'text-red-400', flat: 'text-slate-100' } as const

/** The calendar day of an ISO instant — the granularity a window is actually read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

// The run log. Twelve columns is a lot, so they are grouped under a spanning header row that says
// what each group is *for*: what the run made, how it made it, and what it cost in risk to make.
// A reader scanning for a Sharpe is looking for the third group, not the ninth column.
//
// Only the P&L wears a status colour. A wall of red and green numbers makes none of them mean
// anything; the sign of the P&L is the one thing a glance should catch, and the rest stays in
// neutral ink so the eye can go looking rather than being shouted at.
export function RunTable(props: {
  runs: BacktestListItem[]
  seats: Seats
  onToggle: (id: string) => void
}): React.JSX.Element {
  const full = isFull(props.seats)

  return (
    // Twelve columns will not fit a narrow window, so the table scrolls inside its own box. The
    // page itself never scrolls sideways.
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full min-w-[68rem] border-collapse text-left text-sm">
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2" />
            <th scope="col" className="px-3 py-2" />
            <th scope="col" className="px-3 py-2" />
            <th scope="colgroup" colSpan={3} className="border-l border-slate-800 px-3 py-2">
              Essential
            </th>
            <th scope="colgroup" colSpan={3} className="border-l border-slate-800 px-3 py-2">
              Quality
            </th>
            <th scope="colgroup" colSpan={2} className="border-l border-slate-800 px-3 py-2">
              Risk-adjusted
            </th>
            <th scope="colgroup" className="border-l border-slate-800 px-3 py-2">
              Costs
            </th>
          </tr>
          <tr className="border-b border-slate-800 text-xs font-medium text-slate-400">
            <th scope="col" className="px-3 py-2">
              <span className="sr-only">Compare</span>
            </th>
            <th scope="col" className="px-3 py-2">
              Run
            </th>
            <th scope="col" className="px-3 py-2">
              Status
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2 text-right">
              Net P&L
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Trades
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Max DD
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2 text-right">
              Profit factor
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Win rate
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Payoff
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2 text-right">
              Sharpe
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Sortino
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2">
              Model
            </th>
          </tr>
        </thead>
        <tbody>
          {props.runs.map((run) => {
            const metrics = run.metrics
            // Only a finished run has a curve to draw or metrics to line up. Ticking a queued one
            // would put an empty series on the chart and a row of dashes in the comparison.
            const comparable = run.status === 'done' && metrics !== null
            const picked = isSelected(props.seats, run.id)
            const color = colorOf(props.seats, run.id)
            const blocked = !picked && full

            return (
              <tr
                key={run.id}
                className={`border-b border-slate-800/60 last:border-0 ${
                  picked ? 'bg-slate-800/40' : 'hover:bg-slate-900/60'
                }`}
              >
                <td className="px-3 py-2">
                  {/* The swatch is the tie between this row and its line on the chart above —
                      the same colour, on the control that put it there. */}
                  <span
                    className="block border-l-2 pl-2"
                    style={{ borderColor: color ?? 'transparent' }}
                  >
                    <input
                      type="checkbox"
                      checked={picked}
                      disabled={!comparable || blocked}
                      onChange={() => {
                        props.onToggle(run.id)
                      }}
                      aria-label={compareLabel(run)}
                      title={
                        !comparable
                          ? 'Only a finished run has a curve to compare'
                          : blocked
                            ? `Already comparing ${String(MAX_COMPARED)} runs — untick one first`
                            : undefined
                      }
                      className="size-4 accent-sky-500 disabled:opacity-30"
                    />
                  </span>
                </td>
                <td className="px-3 py-2">
                  <Link to={`/results/${run.id}`} className="font-medium text-sky-400 hover:text-sky-300">
                    {run.symbol} {run.timeframe}
                  </Link>
                  <div className="text-xs text-slate-400">
                    {run.strategy_name} v{run.strategy_version} · {day(run.date_from)} →{' '}
                    {day(run.date_to)}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className={`rounded px-2 py-1 text-xs font-medium ${badge[run.status]}`}>
                    {run.status}
                  </span>
                </td>
                <td
                  className={`border-l border-slate-800 px-3 py-2 text-right tabular-nums ${
                    metrics === null ? 'text-slate-100' : toneClass[sign(metrics.net_profit)]
                  }`}
                >
                  {metrics === null ? '—' : signedMoney(metrics.net_profit)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : count(metrics.total_trades)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : percent(metrics.max_drawdown_pct)}
                </td>
                <td className="border-l border-slate-800 px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.profit_factor)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : percent(metrics.win_rate)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.payoff)}
                </td>
                <td className="border-l border-slate-800 px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.sharpe)}
                </td>
                <td className="px-3 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.sortino)}
                </td>
                <td className="border-l border-slate-800 px-3 py-2 text-xs text-slate-400">
                  {costLabel(run.cost_model)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
