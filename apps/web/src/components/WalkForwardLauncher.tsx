import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateWalkForward } from '../api/hooks'
import type { SelectionMetric, StudyOut } from '../api/types'
import { useSession } from '../store'
import { launchFailure } from '../study/settings'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

const METRICS: { value: SelectionMetric; label: string }[] = [
  { value: 'net_profit', label: 'Net profit' },
  { value: 'profit_factor', label: 'Profit factor' },
  { value: 'sharpe', label: 'Sharpe' },
  { value: 'expectancy', label: 'Expectancy' },
]

/**
 * Launch a walk-forward of the study on screen — the answer to what the study cannot say.
 *
 * ⚠️ **It takes the study, not a grid, and the button lives here for the same reason.** The
 * comparison a walk-forward produces is *"the heatmap said this, a blind choice got that"*, and
 * that only holds if both halves searched the same parameter space over the same market. A
 * separate launch screen would mean retyping a grid that could differ by one value while still
 * looking like the same experiment — and the reader would have no way to tell.
 *
 * The cost is stated before the button rather than after it. Every fold re-runs the entire grid,
 * so this is `folds × points` backtests: a number that surprises people, and one they should see
 * while they are still choosing the fold count.
 */
export function WalkForwardLauncher({ study }: { study: StudyOut }): React.JSX.Element {
  const [folds, setFolds] = useState(6)
  const [trainMultiple, setTrainMultiple] = useState(3)
  const [anchored, setAnchored] = useState(false)
  const [metric, setMetric] = useState<SelectionMetric>('net_profit')

  const navigate = useNavigate()
  const remember = useSession((state) => state.setWalkForward)
  const create = useCreateWalkForward()

  const points = study.aggregate.points_total
  const runs = points * folds

  function launch(event: React.FormEvent): void {
    event.preventDefault()
    create.mutate(
      {
        study_id: study.id,
        folds,
        train_multiple: trainMultiple,
        anchored,
        metric,
      },
      {
        onSuccess: (created) => {
          remember(created.id, `${study.symbol} ${study.timeframe} walk-forward`)
          void navigate(`/walkforwards/${created.id}`)
        },
      },
    )
  }

  return (
    <form onSubmit={launch} className="space-y-3 rounded-lg border border-slate-800 p-4">
      <div>
        <h3 className="font-medium">Test this grid out of sample</h3>
        <p className="mt-1 max-w-3xl text-xs text-slate-500">
          Every number above was scored on the same candles the grid searched, so the best cell is
          a description of this sample rather than evidence about the next one. A walk-forward
          cuts the period into folds, searches the whole grid inside each training window, and
          scores the point it picked on the window that follows — which the choice never saw.
        </p>
      </div>

      <div className="flex flex-wrap items-end gap-4">
        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Folds
          <input
            className={`${inputClass} w-24`}
            type="number"
            min={2}
            max={20}
            value={folds}
            onChange={(event) => {
              setFolds(Number(event.target.value))
            }}
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Training window
          <select
            className={inputClass}
            value={trainMultiple}
            onChange={(event) => {
              setTrainMultiple(Number(event.target.value))
            }}
          >
            {[1, 2, 3, 4, 5].map((multiple) => (
              <option key={multiple} value={multiple}>
                {multiple}× the test window
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-slate-300">
          Chosen by
          <select
            className={inputClass}
            value={metric}
            onChange={(event) => {
              setMetric(event.target.value as SelectionMetric)
            }}
          >
            {METRICS.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex items-center gap-2 pb-1 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={anchored}
            onChange={(event) => {
              setAnchored(event.target.checked)
            }}
          />
          Anchored training
        </label>
      </div>

      {/* Not a footnote: rolling and anchored answer different questions, and the reader is
          choosing between them with a checkbox. */}
      <p className="max-w-3xl text-xs text-slate-500">
        {anchored
          ? 'Anchored: every fold trains on all history before its test window, so later folds choose from more evidence than earlier ones — better use of the data, at the cost of the folds no longer being comparable to each other.'
          : 'Rolling: a fixed-length training window slides forward, so every fold chooses from a sample of the same size. That is what makes any claim about the parameters being stable mean something.'}
      </p>

      <div className="flex flex-wrap items-center gap-4">
        <button
          type="submit"
          disabled={create.isPending || points === 0}
          className="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-700"
        >
          {create.isPending ? 'Cutting the folds…' : 'Run the walk-forward'}
        </button>
        <p className="text-sm text-slate-300" role="status">
          {points} points over {folds} folds — {runs} backtests, a few minutes.
        </p>
      </div>

      {create.isError && (
        <p role="alert" className="text-sm text-red-400">
          {launchFailure(create.error)}
        </p>
      )}
    </form>
  )
}
