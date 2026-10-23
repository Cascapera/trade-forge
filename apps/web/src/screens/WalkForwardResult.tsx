import { Link, useParams } from 'react-router-dom'

import { useWalkForward } from '../api/hooks'
import { WalkForwardFolds } from '../components/WalkForwardFolds'
import { WalkForwardSummary } from '../components/WalkForwardSummary'
import { count, money } from '../format'
import { pending } from '../walkforward/report'

const METRIC_LABEL: Record<string, string> = {
  net_profit: 'net profit',
  profit_factor: 'profit factor',
  sharpe: 'Sharpe',
  expectancy: 'expectancy',
}

/**
 * One walk-forward: what the grid promised, and what survived being chosen blind.
 *
 * This screen is the answer to the sentence at the bottom of the study screen. A heatmap is a
 * description of one sample, and its best cell is the least trustworthy number on it — chosen
 * for being the largest among many competing over the same noise. Here every out-of-sample
 * figure was produced by parameters picked on earlier candles only.
 *
 * ⚠️ **The poll reads the walk-forward's own status, not its runs**, and that distinction is not
 * cosmetic. A fold's out-of-sample run does not exist until its training grid has finished and a
 * winner has been chosen, so "every run I can see has landed" is true several times over the
 * course of one experiment — see `useWalkForward`.
 */
export function WalkForwardResult(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  const walkForward = useWalkForward(id)

  if (walkForward.isPending) return <p className="text-slate-400">Loading the walk-forward…</p>
  if (walkForward.isError) {
    return <p className="text-red-400">Could not load this walk-forward.</p>
  }

  const data = walkForward.data
  const axes = Object.keys(data.grid).length
  const still = pending(data.verdict, data.folds)

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">
          <span className="text-sky-400">{data.strategy_name}</span> tested out of sample over{' '}
          {count(data.folds_requested)} folds
        </h2>
        <p className="text-sm text-slate-400">
          {data.symbol} · {data.timeframe} · {money(data.initial_capital)} per fold ·{' '}
          {data.anchored ? 'anchored' : 'rolling'} training {data.train_multiple}× the test window
          · chosen by {METRIC_LABEL[data.metric] ?? data.metric}
        </p>
        <p className="mt-1 text-sm text-slate-500">
          Re-running{' '}
          <Link to={`/studies/${data.study_id}`} className="text-sky-400 hover:underline">
            the grid you searched
          </Link>{' '}
          ({count(axes)} {axes === 1 ? 'axis' : 'axes'}) once per fold, choosing inside each
          training window and scoring on the window that follows it.
        </p>
      </div>

      {/* The poll is visible rather than silent: a screen of dashes without a reason reads as an
          experiment that produced nothing. */}
      {still > 0 && (
        <p role="status" className="text-sm text-sky-300">
          {count(still)} of {count(data.verdict.folds_total)} folds still running — each one
          searches the whole grid before it can choose, so this takes minutes rather than seconds.
          It updates on its own.
        </p>
      )}

      {data.status === 'failed' && (
        <p role="alert" className="text-sm text-red-400">
          This walk-forward stopped early: {data.error ?? 'no reason was recorded'}. The folds that
          finished before it stopped are still shown below.
        </p>
      )}

      <WalkForwardSummary verdict={data.verdict} folds={data.folds} />

      <section className="space-y-2">
        <h3 className="font-medium">Fold by fold</h3>
        <WalkForwardFolds folds={data.folds} />
        <p className="text-xs text-slate-500">
          Each training window links to its own grid, with its own heatmap. Comparing the first
          fold&apos;s heatmap with the last one&apos;s is the plainest picture of over-fitting
          there is: if the bright region moves every fold, the grid was reading noise.
        </p>
      </section>
    </div>
  )
}
