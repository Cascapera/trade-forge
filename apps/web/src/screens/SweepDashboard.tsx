import { useId, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { useSweepDashboard } from '../api/hooks'
import type { DashboardRatio, DashboardSlice, DashboardSweep, SweepDashboard as Body } from '../api/types'
import { count, percent, ratio, sign } from '../format'
import { launchWindow, runsPerDay } from '../sweep/dashboard'
import { summarise } from '../sweep/progress'

// The sweep page's poles: blue gains, red losses (see `StudyDispersion`). The neutral is a lighter
// slate so it separates from the blue by lightness; checked with the dataviz validator on this
// screen's surface — the only failures left are the neutral's own, which is gray by design.
const TONE = { up: 'text-sky-400', down: 'text-red-400', flat: 'text-slate-100' } as const
const SEGMENT = { winners: 'bg-sky-600', flat: 'bg-slate-400', losers: 'bg-red-600' } as const

function share(part: number, whole: number): string {
  return whole === 0 ? '—' : `${String(Math.round((part / whole) * 100))}%`
}

/**
 * A return that may not exist. Null is a dash, never 0% — nothing has landed.
 *
 * ⚠️ Two decimals, and a value that rounds to zero is shown as zero, neutral. A median of
 * −0.003 % printed as `-0.0%` in the loss colour reads as a loss nobody can see the size of.
 */
function Return(props: { value: string | null }): React.JSX.Element {
  const shown = percent(props.value, 2)
  // Decided on the printed digits, so the rule and the rounding can never disagree.
  const zero = shown.replace('-', '') === '0.00%'
  const tone = props.value === null || zero ? 'flat' : sign(props.value)
  return <span className={`tabular-nums ${TONE[tone]}`}>{zero ? '0.00%' : shown}</span>
}

function Tile(props: {
  label: string
  children: React.ReactNode
  note?: string | undefined
}): React.JSX.Element {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <div className="text-xs tracking-wide text-slate-400 uppercase">{props.label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{props.children}</div>
      {props.note !== undefined && <div className="mt-1 text-xs text-slate-400">{props.note}</div>}
    </div>
  )
}

function RatioTile(props: {
  label: string
  value: DashboardRatio
  render: (value: string | null) => string
  note: string
}): React.JSX.Element {
  return (
    <Tile label={props.label} note={`${props.note} · over ${count(props.value.runs)} runs`}>
      {props.render(props.value.median)}
    </Tile>
  )
}

/**
 * Winners, flat and losers as one bar.
 *
 * ⚠️ Never colour alone: the legend under it names each segment with its count and share, and the
 * bar's accessible name says the same — the bar only makes the proportion visible at a glance.
 */
function SplitBar({ slice }: { slice: DashboardSlice }): React.JSX.Element {
  const parts = [
    { key: 'winners', label: 'Winners', value: slice.winners },
    { key: 'flat', label: 'Flat', value: slice.flat },
    { key: 'losers', label: 'Losers', value: slice.losers },
  ] as const
  const described = parts
    .map((part) => `${part.label} ${count(part.value)} (${share(part.value, slice.finished)})`)
    .join(', ')
  return (
    <div className="space-y-2">
      {slice.finished > 0 && (
        <div role="img" aria-label={described} className="flex h-3 gap-[2px]">
          {parts
            .filter((part) => part.value > 0)
            .map((part) => (
              <div
                key={part.key}
                title={`${part.label}: ${count(part.value)} (${share(part.value, slice.finished)})`}
                className={`${SEGMENT[part.key]} rounded-sm first:rounded-l last:rounded-r`}
                style={{ flexGrow: part.value, flexBasis: 0 }}
              />
            ))}
        </div>
      )}
      <ul className="flex flex-wrap gap-x-5 gap-y-1 text-sm text-slate-300">
        {parts.map((part) => (
          <li key={part.key} className="flex items-center gap-2">
            <span aria-hidden className={`inline-block h-2.5 w-2.5 rounded-sm ${SEGMENT[part.key]}`} />
            {part.label} <span className="tabular-nums">{count(part.value)}</span>
            <span className="text-slate-500">{share(part.value, slice.finished)}</span>
          </li>
        ))}
      </ul>
    </div>
  )
}

/** One table for every breakdown: by strategy, by market, by chart. */
function SliceTable(props: {
  caption: string
  first: string
  slices: DashboardSlice[]
}): React.JSX.Element {
  const head = 'px-3 py-2 text-right font-medium'
  const cell = 'px-3 py-2 text-right tabular-nums'
  const heading = useId()
  return (
    <section className="space-y-2">
      <h3 id={heading} className="font-medium">
        {props.caption}
      </h3>
      <div className="overflow-x-auto rounded-lg border border-slate-800">
        <table aria-labelledby={heading} className="w-full text-sm">
          <thead className="bg-slate-900 text-xs text-slate-400">
            <tr>
              <th scope="col" className="px-3 py-2 text-left font-medium">
                {props.first}
              </th>
              <th scope="col" className={head}>Runs</th>
              <th scope="col" className={head}>Winners</th>
              <th scope="col" className={head}>Losers</th>
              <th scope="col" className={head}>Flat</th>
              <th scope="col" className={head}>Median</th>
              <th scope="col" className={head}>Mean</th>
              <th scope="col" className={head}>Worst</th>
              <th scope="col" className={head}>Best</th>
              <th scope="col" className={head}>Median DD</th>
              <th scope="col" className={head}>Worst DD</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-800">
            {props.slices.map((slice) => (
              <tr key={slice.key}>
                <th scope="row" className="px-3 py-2 text-left font-normal">
                  {slice.label ?? <span className="text-slate-500">Removed entry</span>}
                </th>
                <td className={cell}>
                  {count(slice.finished)}
                  {slice.finished !== slice.runs && (
                    <span className="text-slate-500"> / {count(slice.runs)}</span>
                  )}
                </td>
                <td className={cell}>
                  {count(slice.winners)}{' '}
                  <span className="text-slate-500">{share(slice.winners, slice.finished)}</span>
                </td>
                <td className={cell}>{count(slice.losers)}</td>
                <td className={cell}>{count(slice.flat)}</td>
                <td className={cell}>
                  <Return value={slice.median_return} />
                </td>
                <td className={cell}>
                  <Return value={slice.mean_return} />
                </td>
                <td className={cell}>
                  <Return value={slice.worst_return} />
                </td>
                <td className={cell}>
                  <Return value={slice.best_return} />
                </td>
                <td className={cell}>{percent(slice.median_drawdown)}</td>
                <td className={cell}>{percent(slice.worst_drawdown)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function launchedAt(iso: string): string {
  return new Date(iso).toLocaleString('en-GB', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function Timeline({ sweeps }: { sweeps: DashboardSweep[] }): React.JSX.Element {
  const days = runsPerDay(sweeps)
  return (
    <section className="grid gap-6 lg:grid-cols-[3fr_1fr]">
      <div className="space-y-2">
        <h3 className="font-medium">Sweeps, oldest first</h3>
        <p className="text-xs text-slate-500">
          A sweep's median pools its strategies: it says how that search went, not whether a method
          works — the table above answers that.
        </p>
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full text-sm">
            <thead className="bg-slate-900 text-xs text-slate-400">
              <tr>
                <th scope="col" className="px-3 py-2 text-left font-medium">Launched</th>
                <th scope="col" className="px-3 py-2 text-left font-medium">Strategies</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Runs</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Winners</th>
                <th scope="col" className="px-3 py-2 text-right font-medium">Median</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-800">
              {sweeps.map((sweep) => (
                <tr key={sweep.id}>
                  <td className="px-3 py-2 whitespace-nowrap">
                    <Link to={`/sweeps/${sweep.id}`} className="text-sky-400 hover:text-sky-300">
                      {launchedAt(sweep.created_at)}
                    </Link>
                  </td>
                  <td className="px-3 py-2">
                    {sweep.entry_names.map((name) => name ?? 'removed entry').join(', ')}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {count(sweep.finished)}
                    {sweep.finished !== sweep.runs && (
                      <span className="text-slate-500"> / {count(sweep.runs)}</span>
                    )}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {count(sweep.winners)}{' '}
                    <span className="text-slate-500">{share(sweep.winners, sweep.finished)}</span>
                  </td>
                  <td className="px-3 py-2 text-right">
                    <Return value={sweep.median_return} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>
      <div className="space-y-2">
        <h3 className="font-medium">Runs launched per day</h3>
        <table className="w-full text-sm">
          <tbody className="divide-y divide-slate-800">
            {days.map((one) => (
              <tr key={one.day}>
                <th scope="row" className="py-1.5 text-left font-normal text-slate-300">
                  {one.day}
                </th>
                <td className="py-1.5 text-right tabular-nums">{count(one.runs)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}

function Report({ body }: { body: Body }): React.JSX.Element {
  const { totals, overall } = body
  if (totals.sweeps === 0) {
    return (
      <p className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-sm text-slate-400">
        No sweep was launched in this period.
      </p>
    )
  }
  return (
    <div className="space-y-8">
      <section className="space-y-3">
        <h3 className="text-xs tracking-wide text-slate-500 uppercase">What ran</h3>
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Tile label="Sweeps">{count(totals.sweeps)}</Tile>
          <Tile label="Strategies">{count(totals.entries)}</Tile>
          <Tile
            label="Runs launched"
            note={`${summarise(totals.runs)} · ${count(totals.measurements)} distinct measurements`}
          >
            {count(totals.runs.total)}
          </Tile>
          <Tile
            label="Trades"
            note={`${count(totals.runs_without_trades)} finished runs never traded`}
          >
            {count(totals.trades)}
          </Tile>
        </div>
        <p className="text-sm text-slate-400">
          Markets: {totals.symbols.join(', ')} · Charts: {totals.timeframes.join(', ')}
        </p>
      </section>

      <section className="space-y-3">
        <h3 className="text-xs tracking-wide text-slate-500 uppercase">How it went</h3>
        <SplitBar slice={overall} />
        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          <Tile label="Median run" note="What a run picked without hindsight returned">
            <Return value={overall.median_return} />
          </Tile>
          <Tile label="Mean run" note="Pulled by the extremes; read it after the median">
            <Return value={overall.mean_return} />
          </Tile>
          <Tile label="Worst run">
            <Return value={overall.worst_return} />
          </Tile>
          <Tile label="Best run" note="The best of every run here — the least trustworthy number">
            <Return value={overall.best_return} />
          </Tile>
          <RatioTile
            label="Median win rate"
            value={body.win_rate}
            render={(value) => percent(value)}
            note="Runs that traded"
          />
          <RatioTile
            label="Median profit factor"
            value={body.profit_factor}
            render={(value) => ratio(value)}
            note="Runs with a losing trade"
          />
          <RatioTile
            label="Median expectancy"
            value={body.expectancy}
            render={(value) => percent(value, 3)}
            note="Per trade, of capital"
          />
          <Tile label="Median drawdown" note={`Worst: ${percent(overall.worst_drawdown)}`}>
            {percent(overall.median_drawdown)}
          </Tile>
        </div>
      </section>

      <p className="rounded border border-amber-800 bg-amber-950/40 p-3 text-sm text-amber-200">
        Every number here is <strong>in-sample</strong>: each run was scored on the data its grid
        searched. Read the medians and the share of winners first; the best run is the best of
        every run on this page. A run repeated by a later sweep is the same measurement and counts
        once. Returns cover each run&apos;s whole window, and those windows may differ in length.
      </p>

      <SliceTable caption="By strategy, best median first" first="Strategy" slices={body.by_entry} />
      <div className="grid gap-6 xl:grid-cols-2">
        <SliceTable caption="By market" first="Market" slices={body.by_symbol} />
        <SliceTable caption="By chart" first="Chart" slices={body.by_timeframe} />
      </div>
      <Timeline sweeps={body.sweeps} />
    </div>
  )
}

/**
 * Every sweep launched in a period, summarised at once.
 *
 * The period is the **launch** date, in the reader's own calendar, both days included. Only
 * sweeps count: their runs are the ones with a strategy a person named on the shelf.
 */
export function SweepDashboard(): React.JSX.Element {
  const [fromDay, setFromDay] = useState('')
  const [toDay, setToDay] = useState('')
  const launched = useMemo(() => launchWindow(fromDay, toDay), [fromDay, toDay])
  const dashboard = useSweepDashboard(launched)

  const field = 'rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100'
  const button = 'rounded border border-slate-700 px-3 py-1.5 text-sm hover:bg-slate-800 disabled:opacity-40'

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h2 className="text-xl font-semibold">Sweep dashboard</h2>
          <p className="text-sm text-slate-400">
            Every sweep launched in the period, summarised. Leave a day empty for no limit.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-3">
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Launched from
            <input
              type="date"
              value={fromDay}
              onChange={(event) => {
                setFromDay(event.target.value)
              }}
              className={field}
            />
          </label>
          <label className="flex flex-col gap-1 text-xs text-slate-400">
            Launched to (included)
            <input
              type="date"
              value={toDay}
              onChange={(event) => {
                setToDay(event.target.value)
              }}
              className={field}
            />
          </label>
          <button
            type="button"
            disabled={fromDay === '' && toDay === ''}
            onClick={() => {
              setFromDay('')
              setToDay('')
            }}
            className={button}
          >
            All time
          </button>
          <button
            type="button"
            disabled={launched === null || dashboard.isFetching}
            onClick={() => {
              void dashboard.refetch()
            }}
            className={button}
          >
            {dashboard.isFetching && !dashboard.isPending ? 'Refreshing…' : 'Refresh'}
          </button>
        </div>
      </div>

      {launched === null ? (
        <p role="alert" className="text-sm text-red-400">
          The last day comes before the first one.
        </p>
      ) : dashboard.isPending ? (
        <p className="text-slate-400">Loading the dashboard…</p>
      ) : dashboard.isError ? (
        <p className="text-red-400">Could not load the dashboard.</p>
      ) : (
        <Report body={dashboard.data} />
      )}
    </div>
  )
}
