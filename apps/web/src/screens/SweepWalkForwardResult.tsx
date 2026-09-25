import { Link, useParams } from 'react-router-dom'

import { useSweepWalkForward } from '../api/hooks'
import type { WalkForwardStage } from '../api/types'
import { percent } from '../format'

const STAGE_LABEL: Record<WalkForwardStage, string> = {
  training: 'training',
  testing: 'testing',
  done: 'done',
  failed: 'not tested',
}

function tone(value: string | null): string {
  if (value === null) return 'text-slate-600'
  return Number(value) > 0 ? 'text-emerald-400' : Number(value) < 0 ? 'text-red-400' : ''
}

function year(iso: string): string {
  return iso.slice(0, 4)
}

/** The last year a window covers: its end is the first instant after it. */
function lastYear(iso: string): string {
  return String(Number(iso.slice(0, 4)) - 1)
}

/**
 * A sweep's walk-forward read as one answer (25/09): each fold's windows and stage, and per entry
 * and chart the out-of-sample median of every fold side by side.
 *
 * ⚠️ **Medians and counts, never the best fold.** The question is whether the way of choosing held
 * fold after fold; the best fold is one more lucky draw. And "most chosen" says whether the same
 * point kept winning — a method whose winner changes every fold was choosing noise.
 */
export function SweepWalkForwardResult(): React.JSX.Element {
  const { id } = useParams()
  const walk = useSweepWalkForward(id)

  if (walk.isPending) return <p className="text-slate-400">Loading the walk-forward…</p>
  if (walk.isError) return <p className="text-red-400">Could not load the walk-forward.</p>

  const data = walk.data
  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Walk-forward</h2>
        <p className="mt-1 text-sm text-slate-400">
          {data.folds.length} folds from {data.start_year} · {data.train_years}y training (
          {data.anchored ? 'anchored' : 'rolling'}) · {data.test_years}y test · {data.status}
          {data.parent_sweep_id !== null && (
            <>
              {' · '}
              <Link to={`/sweeps/${data.parent_sweep_id}`} className="text-sky-400 hover:text-sky-300">
                the sweep it walks
              </Link>
            </>
          )}
        </p>
      </header>

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">Folds</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Fold
            </th>
            <th scope="col" className="px-3 py-2">
              Trained on
            </th>
            <th scope="col" className="px-3 py-2">
              Tested on
            </th>
            <th scope="col" className="px-3 py-2">
              Stage
            </th>
          </tr>
        </thead>
        <tbody>
          {data.folds.map((fold) => (
            <tr key={fold.index} className="border-b border-slate-900">
              <td className="px-3 py-2">{fold.index + 1}</td>
              <td className="px-3 py-2">
                {fold.train_sweep_id === null ? (
                  `${year(fold.train_from)}–${lastYear(fold.train_to)}`
                ) : (
                  <Link to={`/sweeps/${fold.train_sweep_id}`} className="hover:underline">
                    {year(fold.train_from)}–{lastYear(fold.train_to)}
                  </Link>
                )}
              </td>
              <td className="px-3 py-2">
                {fold.test_sweep_id === null ? (
                  `${year(fold.test_from)}–${lastYear(fold.test_to)}`
                ) : (
                  <Link to={`/sweeps/${fold.test_sweep_id}`} className="hover:underline">
                    {year(fold.test_from)}–{lastYear(fold.test_to)}
                  </Link>
                )}
              </td>
              <td className={`px-3 py-2 ${fold.stage === 'failed' ? 'text-amber-300' : ''}`}>
                {STAGE_LABEL[fold.stage]}
                {fold.error !== null && (
                  <span className="block text-xs text-slate-500">{fold.error}</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">
          Out of sample, by entry and chart — each fold&apos;s median
        </caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Entry
            </th>
            <th scope="col" className="px-3 py-2">
              Chart
            </th>
            {data.folds.map((fold) => (
              <th key={fold.index} scope="col" className="px-3 py-2">
                {year(fold.test_from)}–{lastYear(fold.test_to)}
              </th>
            ))}
            <th scope="col" className="px-3 py-2">
              Folds positive
            </th>
            <th scope="col" className="px-3 py-2">
              Chosen most
            </th>
          </tr>
        </thead>
        <tbody>
          {data.groups.length === 0 ? (
            <tr>
              <td colSpan={4 + data.folds.length} className="px-3 py-2 text-slate-500">
                No fold has been tested yet.
              </td>
            </tr>
          ) : (
            data.groups.map((group) => (
              <tr key={`${group.entry_id}-${group.timeframe}`} className="border-b border-slate-900">
                <td className="px-3 py-2">{group.entry_name ?? '(removed entry)'}</td>
                <td className="px-3 py-2">{group.timeframe}</td>
                {group.medians.map((median, k) => (
                  <td key={k} className={`px-3 py-2 ${tone(median)}`}>
                    {median === null ? '—' : percent(median)}
                  </td>
                ))}
                <td className="px-3 py-2">
                  {group.positive_folds} of {group.folds}
                </td>
                <td className="px-3 py-2 text-slate-300">
                  {group.most_chosen === null
                    ? '—'
                    : `${group.most_chosen} (${String(group.most_chosen_folds)} of ${String(group.folds)})`}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>
    </section>
  )
}
