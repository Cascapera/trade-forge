import { Link } from 'react-router-dom'

import type { WalkForwardFold } from '../api/types'
import { count, percent, sign } from '../format'

const toneClass = {
  up: 'text-sky-400',
  down: 'text-red-400',
  flat: 'text-slate-300',
} as const

/** The calendar day of an ISO instant — the granularity a window is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

function Return({ fraction }: { fraction: string | null }): React.JSX.Element {
  if (fraction === null) return <span className="text-slate-600">—</span>
  return <span className={toneClass[sign(fraction)]}>{percent(fraction)}</span>
}

/**
 * Every fold, in the order they happened: the windows, the choice, and the two returns.
 *
 * ⚠️ **The training window is shown beside the test window on purpose, not as decoration.** The
 * single claim this whole feature rests on is that the two never overlap, and a reader who can
 * see both dates on one row can check it. A table showing only the result would be asking to be
 * trusted about the one thing worth verifying.
 *
 * The bar counts are there for the same reason. The windows were cut by *counting candles*, so
 * unequal calendar spans on this table are correct and expected — a fold containing a holiday
 * week covers more days for the same evidence. Without the counts, that looks like a bug.
 *
 * Each fold's training grid links to its own study, heatmap included. Fold 1's heatmap beside
 * fold 5's is what over-fitting looks like, and no number on this screen states it as plainly.
 */
export function WalkForwardFolds({
  folds,
}: {
  folds: readonly WalkForwardFold[]
}): React.JSX.Element {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[52rem] text-left text-sm">
        <caption className="sr-only">
          Each fold of the walk-forward: its training window, its test window, the parameters
          chosen and what they returned on each side
        </caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs font-medium text-slate-400">
            <th scope="col" className="px-3 py-2">
              Fold
            </th>
            <th scope="col" className="px-3 py-2">
              Trained on
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2">
              Tested on
            </th>
            <th scope="col" className="px-3 py-2">
              Chose
            </th>
            <th scope="col" className="border-l border-slate-800 px-3 py-2 text-right">
              In sample
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Out of sample
            </th>
            <th scope="col" className="px-3 py-2 text-right">
              Trades
            </th>
          </tr>
        </thead>
        <tbody>
          {folds.map((fold) => (
            <tr key={fold.index} className="border-b border-slate-800/60 last:border-0">
              <td className="px-3 py-2 tabular-nums">{fold.index + 1}</td>
              <td className="px-3 py-2 text-xs text-slate-400">
                <Link to={`/studies/${fold.study_id}`} className="text-sky-400 hover:underline">
                  {day(fold.train_from)} → {day(fold.train_to)}
                </Link>
                <div className="text-slate-500">{count(fold.train_bars)} bars</div>
              </td>
              <td className="border-l border-slate-800 px-3 py-2 text-xs text-slate-400">
                {fold.test_backtest_id === null ? (
                  <span>
                    {day(fold.test_from)} → {day(fold.test_to)}
                  </span>
                ) : (
                  <Link
                    to={`/results/${fold.test_backtest_id}`}
                    className="text-sky-400 hover:underline"
                  >
                    {day(fold.test_from)} → {day(fold.test_to)}
                  </Link>
                )}
                <div className="text-slate-500">{count(fold.test_bars)} bars</div>
              </td>
              <td className="px-3 py-2">
                {fold.chosen_label ?? (
                  // Not a dash: an undecided fold is an answer, and a blank cell reads as one
                  // that is still loading.
                  <span
                    className="text-amber-400"
                    title="Nothing in the grid traded this window, or nothing that traded had a defined score"
                  >
                    chose nothing
                  </span>
                )}
              </td>
              <td className="border-l border-slate-800 px-3 py-2 text-right tabular-nums">
                <Return fraction={fold.in_sample_return} />
              </td>
              <td className="px-3 py-2 text-right font-medium tabular-nums">
                <Return fraction={fold.out_of_sample_return} />
              </td>
              <td className="px-3 py-2 text-right tabular-nums text-slate-400">
                {fold.test_trades === null ? '—' : count(fold.test_trades)}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
