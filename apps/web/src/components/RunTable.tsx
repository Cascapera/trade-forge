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
import { YearBars } from './YearBars'

const badge: Record<BacktestStatus, string> = {
  queued: 'bg-slate-700 text-slate-200',
  running: 'bg-sky-900 text-sky-200',
  done: 'bg-emerald-900 text-emerald-200',
  failed: 'bg-red-900 text-red-200',
}

/** What a sweep's run kept, in the words the row shows — see `Recorded`. */
const KEPT = {
  trades: 'kept: metrics and trades',
  metrics: 'kept: metrics only',
} as const

const toneClass = { up: 'text-emerald-400', down: 'text-red-400', flat: 'text-slate-100' } as const

/** Up to this many years a run's R by year is a column per year; past it, a row of small bars —
 *  eleven signed numbers per row read as a wall (01/10), and eight columns pushed the table past
 *  the page into a sideways scroll that hid them (09/10). */
export const MAX_YEAR_COLUMNS = 4

/** An R figure in a cell under an "R" header: `+1.25`, `-0.50`. The sign is the marker. */
function signedR(value: string): string {
  return `${sign(value) === 'up' ? '+' : ''}${ratio(value)}`
}

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
  /** How many clones each run stands for, by run id (01/10) — a sweep's ranked page hides the
   *  runs that made exactly another's trades, and says so on the one it kept. */
  clones?: ReadonlyMap<string, number>
  /** The years of the window, for R by year of entry (01/10) — a sweep's ranked page, where every
   *  run shares one window. Absent: no such columns. */
  years?: readonly number[]
}): React.JSX.Element {
  const full = isFull(props.seats)
  const years = props.years ?? []
  const asBars = years.length > MAX_YEAR_COLUMNS
  const yearColumns = years.length === 0 ? 0 : asBars ? 1 : years.length

  return (
    // ⚠️ No minimum width (09/10): one forced the table past the page, so every sweep showed a
    // sideways scroll and its years sat off-screen. The table fits the page and its cells wrap;
    // the box still scrolls, inside itself, on a window too narrow even for that.
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="w-full border-collapse text-left text-sm">
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
            {yearColumns > 0 && (
              <th
                scope="colgroup"
                colSpan={yearColumns}
                className="border-l border-slate-800 px-3 py-2"
                title="R of the trades that entered in each year, wherever they left"
              >
                R by year of entry
              </th>
            )}
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
            <th scope="col" className="border-l border-slate-800 px-2 py-2 text-right">
              Net P&L
            </th>
            <th scope="col" className="px-2 py-2 text-right">
              Trades
            </th>
            <th scope="col" className="px-2 py-2 text-right">
              Max DD
            </th>
            <th scope="col" className="border-l border-slate-800 px-2 py-2 text-right">
              Profit factor
            </th>
            <th scope="col" className="px-2 py-2 text-right">
              Win rate
            </th>
            <th scope="col" className="px-2 py-2 text-right">
              Payoff
            </th>
            <th scope="col" className="border-l border-slate-800 px-2 py-2 text-right">
              Sharpe
            </th>
            <th scope="col" className="px-2 py-2 text-right">
              Sortino
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2">
              Model
            </th>
            {asBars ? (
              <th
                scope="col"
                className="border-l border-slate-800 px-3 py-2"
                title="One bar per year, scaled to the run's own largest year — hover a bar for its R"
              >
                {String(years[0])}–{String(years[years.length - 1])}
              </th>
            ) : (
              years.map((year, index) => (
                <th
                  key={year}
                  scope="col"
                  className={`px-3 py-2 text-right ${index === 0 ? 'border-l border-slate-800' : ''}`}
                >
                  {year}
                </th>
              ))
            )}
          </tr>
        </thead>
        <tbody>
          {props.runs.map((run) => {
            const metrics = run.metrics
            // Only a finished run has a curve to draw or metrics to line up. Ticking a queued one
            // would put an empty series on the chart and a row of dashes in the comparison. And
            // only one that kept its curve: a sweep's run did not (`Recorded`), and ticking it
            // would draw nothing while looking like a run that went nowhere.
            const comparable = run.status === 'done' && metrics !== null && run.recorded === 'full'
            const picked = isSelected(props.seats, run.id)
            const color = colorOf(props.seats, run.id)
            const blocked = !picked && full
            const clones = props.clones?.get(run.id) ?? 0

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
                        run.status === 'done' && run.recorded !== 'full'
                          ? 'This run kept no equity curve — open it to run the point again'
                          : !comparable
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
                  <div className="max-w-[18rem] break-words text-xs text-slate-400">
                    {run.strategy_name} v{run.strategy_version} · {day(run.date_from)} →{' '}
                    {day(run.date_to)}
                  </div>
                  {run.recorded !== 'full' && (
                    <div className="text-xs text-slate-500">{KEPT[run.recorded]}</div>
                  )}
                  {typeof run.reused_from === 'string' && (
                    <div className="text-xs text-sky-300">copy of an earlier run</div>
                  )}
                  {clones > 0 && (
                    <div
                      className="text-xs text-slate-500"
                      title="Runs of this entry, chart and market launched after this one that made exactly its trades: they differ only in a setting no trade reached"
                    >
                      +{String(clones)} clone{clones === 1 ? '' : 's'}
                    </div>
                  )}
                </td>
                <td className="px-3 py-2">
                  <span className={`rounded px-2 py-1 text-xs font-medium ${badge[run.status]}`}>
                    {run.status}
                  </span>
                </td>
                <td
                  className={`border-l border-slate-800 px-2 py-2 text-right tabular-nums ${
                    metrics === null ? 'text-slate-100' : toneClass[sign(metrics.net_profit)]
                  }`}
                >
                  {metrics === null ? '—' : signedMoney(metrics.net_profit)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : count(metrics.total_trades)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : percent(metrics.max_drawdown_pct)}
                </td>
                <td className="border-l border-slate-800 px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.profit_factor)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : percent(metrics.win_rate)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.payoff)}
                </td>
                <td className="border-l border-slate-800 px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.sharpe)}
                </td>
                <td className="px-2 py-2 text-right tabular-nums">
                  {metrics === null ? '—' : ratio(metrics.sortino)}
                </td>
                <td className="max-w-[8rem] break-words border-l border-slate-800 px-2 py-2 text-xs text-slate-400">
                  {costLabel(run.cost_model)}
                </td>
                {yearColumns > 0 && <YearCells years={years} asBars={asBars} yearly={metrics?.yearly_r} />}
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/**
 * One row's R by year of entry: a cell per year, or one cell of bars past `MAX_YEAR_COLUMNS`.
 * ⚠️ A dash is "not measured" — unfinished, or recorded before R by year existed; a dot is "no trade
 * entered that year", a measured nothing that must not read as a missing number.
 */
function YearCells(props: {
  years: readonly number[]
  asBars: boolean
  yearly: Record<string, string> | null | undefined
}): React.JSX.Element {
  const { yearly } = props
  if (yearly === null || yearly === undefined) {
    return (
      <td
        colSpan={props.asBars ? 1 : props.years.length}
        className="border-l border-slate-800 px-3 py-2 text-right text-slate-500"
      >
        —
      </td>
    )
  }
  if (props.asBars) {
    return (
      <td className="border-l border-slate-800 px-3 py-2">
        <YearBars years={props.years} yearly={yearly} />
      </td>
    )
  }
  return (
    <>
      {props.years.map((year, index) => {
        const r = yearly[String(year)]
        const edge = index === 0 ? 'border-l border-slate-800' : ''
        return r === undefined ? (
          <td
            key={year}
            title={`No trade entered in ${String(year)}`}
            className={`${edge} px-3 py-2 text-right text-slate-500`}
          >
            ·
          </td>
        ) : (
          <td key={year} className={`${edge} px-2 py-2 text-right tabular-nums`}>
            {signedR(r)}
          </td>
        )
      })}
    </>
  )
}
