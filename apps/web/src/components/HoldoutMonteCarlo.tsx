import { useState } from 'react'

import { apiFailure } from '../api/failure'
import { useCreateMonteCarlo, useMonteCarlos } from '../api/hooks'
import type { MonteCarloOut, MonteCarloPoint } from '../api/types'
import { percent } from '../format'

const CHART_ORDER = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']

function r(value: string): string {
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(1)} R`
}

function depth(value: string): string {
  return `${Number(value).toFixed(1)} R`
}

function sortPoints(points: MonteCarloPoint[]): MonteCarloPoint[] {
  return [...points].sort(
    (left, right) =>
      (left.entry_name ?? '').localeCompare(right.entry_name ?? '') ||
      CHART_ORDER.indexOf(left.timeframe) - CHART_ORDER.indexOf(right.timeframe) ||
      left.symbol.localeCompare(right.symbol) ||
      left.label.localeCompare(right.label),
  )
}

function Row(props: { point: MonteCarloPoint }): React.JSX.Element {
  const { point } = props
  const simulated = point.simulated
  const why = !point.trades_kept
    ? 'no trades kept'
    : `${String(point.trades)} trades — too few to resample`
  return (
    <tr className="border-b border-slate-900 align-top">
      <td className="px-3 py-2">{point.entry_name ?? '(removed entry)'}</td>
      <td className="px-3 py-2">
        {point.symbol} {point.timeframe}
      </td>
      <td className="px-3 py-2 text-slate-300">{point.label}</td>
      <td className="px-3 py-2">
        {r(point.observed_net_r)}
        <span className="block text-xs text-slate-500">
          fell {depth(point.observed_drawdown_r)} · {String(point.observed_losing_streak)} losses
          in a row
        </span>
      </td>
      {simulated === null ? (
        <td colSpan={3} className="px-3 py-2 text-slate-500">
          {why}
        </td>
      ) : (
        <>
          <td className="px-3 py-2">
            {depth(simulated.drawdown_r.p50)}
            <span className="block text-xs text-amber-300">
              95%: {depth(simulated.drawdown_r.p95)}
            </span>
          </td>
          <td className="px-3 py-2">
            {String(Number(simulated.losing_streak.p50))}
            <span className="block text-xs text-amber-300">
              95%: {String(Number(simulated.losing_streak.p95))}
            </span>
          </td>
          <td
            className={`px-3 py-2 ${Number(simulated.negative_share) >= 0.5 ? 'text-red-400' : 'text-slate-200'}`}
          >
            {percent(simulated.negative_share, 0)}
            <span className="block text-xs text-slate-500">
              {r(simulated.net_r.p5)} … {r(simulated.net_r.p95)}
            </span>
          </td>
        </>
      )}
    </tr>
  )
}

function Resampling(props: { run: MonteCarloOut }): React.JSX.Element {
  const { run } = props
  const title = `${String(run.paths)} paths per point · seed ${run.seed} · ${run.created_at.slice(0, 16).replace('T', ' ')}`
  return (
    <article aria-label={title} className="space-y-2 rounded border border-slate-800 p-4">
      <p className="text-sm text-slate-300">{title}</p>
      <table className="w-full border-collapse text-left text-sm">
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
              What happened
            </th>
            <th scope="col" className="px-3 py-2">
              Drawdown to expect
            </th>
            <th scope="col" className="px-3 py-2">
              Losing streak to expect
            </th>
            <th scope="col" className="px-3 py-2">
              Ends negative
            </th>
          </tr>
        </thead>
        <tbody>
          {sortPoints(run.points).map((point) => (
            <Row key={point.run_id} point={point} />
          ))}
        </tbody>
      </table>
    </article>
  )
}

/**
 * Resample a finished reserved-window test's points (25/09): each point's trades drawn with
 * replacement into many paths, beside the one path that happened.
 *
 * ⚠️ **The median and the 95% are shown, never the best path.** The question is how bad it can
 * get — the drawdown and the losing streak to be ready for — and how often the whole thing ends
 * below zero; a best case answers nothing anybody has to live through.
 *
 * Trades are assumed independent. Losses that cluster with the market's regime are understated
 * here; the cuts by year and by blocks above are where that shows.
 */
export function HoldoutMonteCarlo(props: { sweepId: string; settled: boolean }): React.JSX.Element {
  const { sweepId, settled } = props
  const runs = useMonteCarlos(sweepId)
  const create = useCreateMonteCarlo(sweepId)
  const [paths, setPaths] = useState(1000)
  const [seed, setSeed] = useState('')

  const pathsOk = Number.isInteger(paths) && paths >= 100 && paths <= 5000
  const why = !settled
    ? 'The test is still running — resample it once every point has landed.'
    : !pathsOk
      ? 'Between 100 and 5000 paths.'
      : null

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1'
  return (
    <section aria-label="resampled" className="space-y-4">
      <div className="space-y-3 rounded border border-slate-800 p-4">
        <div>
          <h3 className="font-semibold">Resample (Monte Carlo)</h3>
          <p className="text-sm text-slate-400">
            Draw each point&apos;s out-of-sample trades, with replacement, into many paths: the
            drawdown and the losing streak to be ready for, and how often the whole run ends below
            zero. Runs nothing, but takes a few seconds on a large test.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <label className="flex flex-col gap-1">
            Paths per point
            <input
              type="number"
              min={100}
              max={5000}
              value={paths}
              onChange={(event) => {
                setPaths(Number(event.target.value))
              }}
              className={`${input} w-28`}
            />
          </label>
          <label className="flex flex-col gap-1">
            Seed (optional)
            <input
              value={seed}
              placeholder="drawn and kept"
              maxLength={64}
              onChange={(event) => {
                setSeed(event.target.value)
              }}
              className={`${input} w-40`}
            />
          </label>
          <button
            type="button"
            disabled={why !== null || create.isPending}
            onClick={() => {
              create.mutate({ paths, ...(seed.trim() === '' ? {} : { seed: seed.trim() }) })
            }}
            className="rounded bg-sky-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-600 disabled:opacity-40"
          >
            {create.isPending ? 'Resampling…' : 'Resample'}
          </button>
        </div>
        {why !== null && <p className="text-xs text-amber-300">{why}</p>}
        {create.isError && (
          <p className="text-sm text-red-400">{apiFailure(create.error, 'Could not resample.')}</p>
        )}
      </div>

      {runs.isError ? (
        <p className="text-red-400">Could not load the resamplings.</p>
      ) : runs.data === undefined ? null : runs.data.length === 0 ? (
        <p className="text-sm text-slate-500">Not resampled yet.</p>
      ) : (
        runs.data.map((run) => <Resampling key={run.id} run={run} />)
      )}
    </section>
  )
}
