import type { TargetOutcome, TargetRung } from '../api/types'
import { count, ratio, sign } from '../format'

const toneClass = { up: 'text-sky-400', down: 'text-red-400', flat: 'text-slate-100' } as const

/** An R figure with its sign spelled out: `+0.30 R`, `-1.20 R`. A dash when there is none. */
function inR(value: string | null): string {
  if (value === null) return '—'
  const plain = ratio(value)
  return `${sign(value) === 'up' ? '+' : ''}${plain} R`
}

const head = 'px-3 py-2 text-right text-xs font-medium text-slate-400'
const cell = 'px-3 py-2 text-right tabular-nums'

/**
 * One run's target ladder: what its trades would have made at each target, in R net of costs.
 *
 * Scored by the worker from how far each trade went (its MFE), so a sweep runs once, without a
 * target, and every target is read here. ⚠️ Per trade: a target closes a trade earlier, and a real
 * run with it could have taken an entry this one never saw — the rows are these trades under each
 * target, not a rerun at each target.
 */
export function RunTargets(props: {
  targets: Record<string, TargetOutcome | null>
}): React.JSX.Element {
  const rows = Object.entries(props.targets)
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table aria-label="Result by target" className="w-full min-w-[36rem] border-collapse text-sm">
        <thead>
          <tr className="border-b border-slate-800">
            <th scope="col" className={`${head} text-left`}>
              Target
            </th>
            <th scope="col" className={head}>
              Hits
            </th>
            <th scope="col" className={head}>
              Net
            </th>
            <th scope="col" className={head}>
              Per trade
            </th>
            <th scope="col" className={head}>
              Max drawdown
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map(([rung, outcome]) => (
            <tr key={rung} className="border-b border-slate-800/60 last:border-0">
              <th scope="row" className="px-3 py-2 text-left font-medium">
                {rung} R
              </th>
              {outcome === null ? (
                <td colSpan={4} className="px-3 py-2 text-right text-xs text-slate-500">
                  Not scored — a trade here had a target of its own below this one
                </td>
              ) : (
                <>
                  <td className={cell}>
                    {count(outcome.hits)} of {count(outcome.trades)}
                  </td>
                  <td className={`${cell} ${toneClass[sign(outcome.net_r)]}`}>
                    {inR(outcome.net_r)}
                  </td>
                  <td className={cell}>{inR(outcome.expectancy_r)}</td>
                  <td className={cell}>{ratio(outcome.max_drawdown_r)} R</td>
                </>
              )}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/**
 * The ladder across one sweep entry's runs. The median comes first because the best run at any
 * target is the best of many draws; it is shown beside the median, never instead of it.
 */
export function SweepTargets(props: { rungs: TargetRung[] }): React.JSX.Element | null {
  if (props.rungs.every((rung) => rung.runs_scored === 0)) return null
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table
        aria-label="Result by target across runs"
        className="w-full min-w-[44rem] border-collapse text-sm"
      >
        <thead>
          <tr className="border-b border-slate-800">
            <th scope="col" className={`${head} text-left`}>
              Target
            </th>
            <th scope="col" className={head}>
              Runs positive
            </th>
            <th scope="col" className={head}>
              Median per trade
            </th>
            <th scope="col" className={`${head} text-left`}>
              Best run
            </th>
          </tr>
        </thead>
        <tbody>
          {props.rungs.map((rung) => (
            <tr key={rung.rung} className="border-b border-slate-800/60 last:border-0">
              <th scope="row" className="px-3 py-2 text-left font-medium">
                {rung.rung} R
              </th>
              <td className={cell}>
                {count(rung.runs_positive)} of {count(rung.runs_scored)}
              </td>
              <td
                className={`${cell} ${
                  rung.median_expectancy_r === null
                    ? 'text-slate-500'
                    : toneClass[sign(rung.median_expectancy_r)]
                }`}
              >
                {inR(rung.median_expectancy_r)}
              </td>
              <td className="px-3 py-2 text-xs text-slate-400">
                {rung.best_label === null ? '—' : `${rung.best_label} · ${inR(rung.best_net_r)}`}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
