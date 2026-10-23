import type { WalkForwardFold, WalkForwardVerdict } from '../api/types'
import { count, percent, sign } from '../format'
import { retained, stability, survival } from '../walkforward/report'

const toneClass = {
  up: 'text-sky-400',
  down: 'text-red-400',
  flat: 'text-slate-100',
} as const

function Tile(props: {
  label: string
  value: string
  note?: string | undefined
  tone?: keyof typeof toneClass | undefined
}): React.JSX.Element {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <div className="text-xs tracking-wide text-slate-400 uppercase">{props.label}</div>
      <div
        className={`mt-1 text-2xl font-semibold tabular-nums ${toneClass[props.tone ?? 'flat']}`}
      >
        {props.value}
      </div>
      {props.note !== undefined && <div className="mt-1 text-xs text-slate-400">{props.note}</div>}
    </div>
  )
}

/** A return that may not exist yet. A dash, never 0% — nothing has landed is not no profit. */
function ret(fraction: string | null): { value: string; tone: keyof typeof toneClass } {
  if (fraction === null) return { value: '—', tone: 'flat' }
  return { value: percent(fraction), tone: sign(fraction) }
}

const STABILITY_NOTE: Record<ReturnType<typeof stability>, string> = {
  stable: 'Every fold chose the same parameters',
  partly: 'The folds disagreed about the parameters',
  unstable: 'A different winner in every fold',
  unknown: 'Not enough decided folds to say',
}

/**
 * What a walk-forward adds up to — **the promise beside the delivery, in that order.**
 *
 * The reading order is the argument. The study screen leads with dispersion because a grid's
 * best point is its least trustworthy number; this screen leads with the *pair*, because the
 * single most useful fact about an optimised strategy is how much of its backtest survived
 * being chosen without hindsight. A walk-forward that reported only its out-of-sample return
 * would be a better number with the same missing context.
 *
 * The second row is the half a return cannot say. Stability answers "is there a parameter set to
 * trade at all?" — a different winner every fold means the grid was reading noise, and that is
 * true even when the out-of-sample number is positive, which is exactly when a reader stops
 * reading. `folds_profitable` answers "was this one lucky window or a pattern?".
 */
export function WalkForwardSummary({
  verdict,
  folds,
}: {
  verdict: WalkForwardVerdict
  folds: readonly WalkForwardFold[]
}): React.JSX.Element {
  const promise = ret(verdict.in_sample_median)
  const delivery = ret(verdict.out_of_sample_median)
  const compounded = ret(verdict.compounded)
  const kept = retained(verdict)
  const how = stability(verdict)
  const undecided = verdict.folds_total - verdict.folds_decided

  return (
    <div className="space-y-3">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {/* "Median in sample", not "In sample", and the extra word is doing work: these two
            tiles are middle values *across folds*, while the columns of the table below carry
            one fold's own return. Same words for the two would leave a reader unable to say
            which of the two things a number was — and would give the screen two elements with
            one accessible name. */}
        <Tile
          label="Median in sample"
          value={promise.value}
          tone={promise.tone}
          note="What the fold's chosen point returned on the window that chose it"
        />
        <Tile
          label="Median out of sample"
          value={delivery.value}
          tone={delivery.tone}
          note="What it returned on the window it had never seen"
        />
        <Tile
          label="Survived"
          value={kept === null ? '—' : percent(String(kept), 0)}
          tone={kept === null ? 'flat' : kept > 0 ? 'up' : 'down'}
          note={
            kept === null
              ? 'No positive in-sample median to keep a share of'
              : 'Of what the heatmap promised'
          }
        />
        <Tile
          label="Compounded"
          value={compounded.value}
          tone={compounded.tone}
          note={`Across ${count(verdict.folds_scored)} scored fold(s)`}
        />
      </div>

      <div className="grid gap-3 sm:grid-cols-2">
        <Tile
          label="Parameters"
          value={
            how === 'unknown'
              ? '—'
              : `${count(verdict.distinct_choices)} of ${count(verdict.folds_decided)}`
          }
          tone={how === 'stable' ? 'up' : how === 'unstable' ? 'down' : 'flat'}
          note={STABILITY_NOTE[how]}
        />
        <Tile
          label="Profitable folds"
          value={
            verdict.folds_scored === 0
              ? '—'
              : `${count(verdict.folds_profitable)} / ${count(verdict.folds_scored)}`
          }
          note="One winning fold is a window; most of them is a pattern"
        />
      </div>

      <p className="max-w-3xl text-xs text-slate-500">
        Every out-of-sample figure here was produced by parameters chosen on{' '}
        <span className="text-slate-400">earlier candles only</span> — no fold's training window
        overlaps the window that scores it. That is what makes these numbers evidence rather than
        description, and it is also why they are usually far worse than a heatmap's best cell.
        {undecided > 0 &&
          ` ${count(undecided)} fold(s) chose nothing: no parameter set in the grid traded that
          window, which is a finding about the method rather than a gap in the report.`}
        {survival(verdict) === 'lost' &&
          ' The median fold lost money out of sample: whatever the grid found, it did not survive being chosen blind.'}
        {folds.some((fold) => fold.test_trades !== null && fold.test_trades < 5) &&
          ' At least one fold was scored on very few trades — read its return as an anecdote.'}
      </p>
    </div>
  )
}
