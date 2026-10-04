import { useState } from 'react'

import { apiFailure } from '../api/failure'
import { useCreateMonteCarlo, useMonteCarlos } from '../api/hooks'
import type { CreateMonteCarloRequest, MonteCarloOut, MonteCarloPoint, Simulated } from '../api/types'
import { percent } from '../format'
import { type RankKey, rankingOf } from '../sweep/ranking'
import { Pager, PagedList } from './Pager'
import { usePaged } from './paging'

const CHART_ORDER = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']

/** The most runs per entry a ranking's resampling takes — the server's `MONTECARLO_MAX_TOP_N`. */
const MAX_TOP_N = 20

/**
 * How much deeper the 95% drawdown in blocks must be than trade by trade before the row says the
 * losses come in runs (01/10). A reading aid, not a test: two draws of the same trades differ a
 * little by chance, and half again as deep is well past that.
 */
const CLUSTERED = 1.5

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

/** Whether the draw in blocks fell much deeper than the draw trade by trade (`CLUSTERED`). */
function clustered(point: MonteCarloPoint): boolean {
  const blocks = point.in_blocks
  const single = point.simulated
  if (blocks === undefined || blocks === null || single === null) return false
  const alone = Number(single.drawdown_r.p95)
  return alone > 0 && Number(blocks.drawdown_r.p95) >= CLUSTERED * alone
}

/** One draw: the drawdown and the streak to be ready for, and how often it ends below zero. */
function Draw(props: { simulated: Simulated; warn?: boolean }): React.JSX.Element {
  const { simulated } = props
  return (
    <td className="px-3 py-2">
      {simulated.block_trades !== undefined && simulated.block_trades !== null && (
        <span className="block text-xs text-slate-500">
          blocks of {String(simulated.block_trades)}
        </span>
      )}
      <span className={props.warn === true ? 'text-red-400' : undefined}>
        fell {depth(simulated.drawdown_r.p50)}
      </span>
      <span className="block text-xs text-amber-300">
        95%: {depth(simulated.drawdown_r.p95)}
      </span>
      <span className="block text-xs text-slate-300">
        {String(Number(simulated.losing_streak.p50))} losses in a row · 95%:{' '}
        {String(Number(simulated.losing_streak.p95))}
      </span>
      <span
        className={`block text-xs ${Number(simulated.negative_share) >= 0.5 ? 'text-red-400' : 'text-slate-300'}`}
      >
        ends negative {percent(simulated.negative_share, 0)}
      </span>
      <span className="block text-xs text-slate-500">
        {r(simulated.net_r.p5)} … {r(simulated.net_r.p95)}
      </span>
    </td>
  )
}

function Row(props: { point: MonteCarloPoint }): React.JSX.Element {
  const { point } = props
  const simulated = point.simulated
  const blocks = point.in_blocks
  const why = !point.trades_kept
    ? 'no trades kept'
    : `${String(point.trades)} trades — too few to resample`
  const runs = clustered(point)
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
        <td colSpan={2} className="px-3 py-2 text-slate-500">
          {why}
        </td>
      ) : (
        <>
          <Draw simulated={simulated} />
          {blocks === undefined || blocks === null ? (
            <td className="px-3 py-2 text-xs text-slate-500">not drawn in blocks (before 01/10)</td>
          ) : (
            <Draw simulated={blocks} warn={runs} />
          )}
        </>
      )}
      <td className="px-3 py-2 text-xs">
        {runs && <span className="text-red-400">Losses come in runs</span>}
      </td>
    </tr>
  )
}

function blockHeading(run: MonteCarloOut): string {
  return run.block_trades === undefined || run.block_trades === null
    ? 'In blocks of N (each point’s own)'
    : `In blocks of ${String(run.block_trades)}`
}

function Resampling(props: { run: MonteCarloOut }): React.JSX.Element {
  const { run } = props
  const { page, pager } = usePaged(sortPoints(run.points), 50)
  const which =
    run.ranking === undefined || run.ranking === null
      ? ''
      : ` · top ${String(run.ranking.top_n)} per entry by ${rankingOf(run.ranking.rank_by).label.toLowerCase()}`
  const title = `${String(run.paths)} paths per point${which} · seed ${run.seed} · ${run.created_at.slice(0, 16).replace('T', ' ')}`
  return (
    <article aria-label={title} className="space-y-2 rounded border border-slate-800 p-4">
      <p className="text-sm text-slate-300">{title}</p>
      <p className="text-xs text-slate-500">
        If the drawdown in blocks is much worse than trade by trade, the losses come in runs.
      </p>
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
              Trade by trade
            </th>
            <th scope="col" className="px-3 py-2">
              {blockHeading(run)}
            </th>
            <th scope="col" className="px-3 py-2">
              <span className="sr-only">Reading</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {page.map((point) => (
            <Row key={point.run_id} point={point} />
          ))}
        </tbody>
      </table>
      <Pager label="Point pages" {...pager} />
    </article>
  )
}

/**
 * Resample a finished sweep's points (25/09): each point's trades drawn into many paths, beside
 * the one path that happened. A reserved-window test resamples every point it tested; an ordinary
 * sweep, since 01/10, the top `top n` runs of each entry by `rankBy` — the screen's own ranking.
 *
 * ⚠️ **Two draws side by side** (01/10). Trade by trade assumes the trades independent; in blocks
 * deals runs of trades in a row whole, so losses that cluster with the market's regime stay
 * together. A drawdown much deeper in blocks says they do (`CLUSTERED`).
 *
 * ⚠️ **The median and the 95% are shown, never the best path.** The question is how bad it can
 * get — the drawdown and the losing streak to be ready for — and how often the whole thing ends
 * below zero; a best case answers nothing anybody has to live through.
 */
export function MonteCarlo(props: {
  sweepId: string
  settled: boolean
  /** Set on an ordinary sweep: the ranking whose top is resampled. */
  rankBy?: RankKey
}): React.JSX.Element {
  const { sweepId, settled, rankBy } = props
  const runs = useMonteCarlos(sweepId)
  const create = useCreateMonteCarlo(sweepId)
  const [paths, setPaths] = useState(1000)
  const [seed, setSeed] = useState('')
  const [block, setBlock] = useState('')
  const [topN, setTopN] = useState(10)

  const pathsOk = Number.isInteger(paths) && paths >= 100 && paths <= 5000
  const blockNumber = Number(block)
  const blockOk =
    block.trim() === '' || (Number.isInteger(blockNumber) && blockNumber >= 2 && blockNumber <= 50)
  const topOk = Number.isInteger(topN) && topN >= 1 && topN <= MAX_TOP_N
  const why = !settled
    ? rankBy === undefined
      ? 'The test is still running — resample it once every point has landed.'
      : 'The sweep is still running — resample its ranking once every run has landed.'
    : !pathsOk
      ? 'Between 100 and 5000 paths.'
      : !blockOk
        ? 'A block of 2 to 50 trades, or blank for each point’s own.'
        : rankBy !== undefined && !topOk
          ? `Between 1 and ${String(MAX_TOP_N)} runs per entry.`
          : null

  function payload(): CreateMonteCarloRequest {
    return {
      paths,
      ...(seed.trim() === '' ? {} : { seed: seed.trim() }),
      ...(block.trim() === '' ? {} : { block_trades: blockNumber }),
      ...(rankBy === undefined ? {} : { ranking: { rank_by: rankBy, top_n: topN } }),
    }
  }

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1'
  return (
    <section aria-label="resampled" className="space-y-4">
      <div className="space-y-3 rounded border border-slate-800 p-4">
        <div>
          <h3 className="font-semibold">
            {rankBy === undefined ? 'Resample (Monte Carlo)' : 'Resample the ranking (Monte Carlo)'}
          </h3>
          <p className="text-sm text-slate-400">
            {rankBy === undefined
              ? 'Draw each point’s out-of-sample trades, with replacement, into many paths'
              : `Draw the trades of the best runs of each entry, by ${rankingOf(rankBy).label.toLowerCase()} as ranked below, into many paths`}{' '}
            — trade by trade, and in blocks of trades in a row: the drawdown and the losing streak
            to be ready for, and how often the whole run ends below zero. Runs nothing, but takes a
            few seconds.
            {rankBy !== undefined &&
              ' Only runs that kept their trades — the ones over the bar for keeping them — can be drawn.'}
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-4 text-sm">
          {rankBy !== undefined && (
            <label className="flex flex-col gap-1">
              Runs per entry
              <input
                type="number"
                min={1}
                max={MAX_TOP_N}
                value={topN}
                onChange={(event) => {
                  setTopN(Number(event.target.value))
                }}
                className={`${input} w-24`}
              />
            </label>
          )}
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
            Block (trades in a row)
            <input
              type="number"
              min={2}
              max={50}
              value={block}
              placeholder="each point’s own"
              onChange={(event) => {
                setBlock(event.target.value)
              }}
              className={`${input} w-40`}
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
              create.mutate(payload())
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
        <PagedList items={runs.data} limit={3} label="Resampling pages" className="space-y-4">
          {(run) => <Resampling key={run.id} run={run} />}
        </PagedList>
      )}
    </section>
  )
}
