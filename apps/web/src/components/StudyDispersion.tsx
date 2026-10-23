import type { StudyAggregate } from '../api/types'
import { count, percent, sign } from '../format'

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

/** A return that may not exist yet. Undefined is a dash, never 0% — nothing has landed. */
function ret(fraction: string | null): {
  value: string
  tone: keyof typeof toneClass
} {
  if (fraction === null) return { value: '—', tone: 'flat' }
  // `sign` returns up/down/flat; up is the gain tone, which on this screen is blue rather than
  // green — see `backtest/study` for why the heatmap's poles are blue and red, and why the
  // tiles beside it must not disagree with the picture.
  return { value: percent(fraction), tone: sign(fraction) }
}

/**
 * What a grid says once its runs finish — **dispersion, and never the maximum alone.**
 *
 * ⚠️ The most important thing about this component is what is *not* at the top of it. A grid
 * always has a best point; a grid of pure noise has a best point. Leading with that number is
 * how an optimiser becomes a machine for producing convincing false results, and the wider the
 * grid the more convincing they get — searching a hundred parameter sets and keeping the winner
 * is mostly a search for the luckiest arrangement of the same noise.
 *
 * So the median leads: what a parameter set chosen *without* hindsight would have returned. Then
 * how much of the searched space works at all — ninety of a hundred is a property of the method,
 * three of a hundred is a corner whatever the best of those three did. The best and worst come
 * last, together, so the best reads as one end of a range rather than as a result.
 *
 * And the footnote is not decoration: every figure here is in-sample, the best one included.
 */
export function StudyDispersion({ aggregate }: { aggregate: StudyAggregate }): React.JSX.Element {
  const median = ret(aggregate.median_return)
  const best = ret(aggregate.best_return)
  const worst = ret(aggregate.worst_return)
  const finished = aggregate.points_finished

  return (
    <div className="space-y-2">
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Tile
          label="Median point"
          value={median.value}
          tone={median.tone}
          note="What a parameter set picked without hindsight returned"
        />
        <Tile
          label="Profitable"
          value={
            finished === 0 ? '—' : `${count(aggregate.points_profitable)} / ${count(finished)}`
          }
          note="How much of the searched space works at all"
        />
        <Tile
          label="Best"
          value={best.value}
          tone={best.tone}
          note={aggregate.best_label ?? undefined}
        />
        <Tile
          label="Worst"
          value={worst.value}
          tone={worst.tone}
          note={aggregate.worst_label ?? undefined}
        />
      </div>
      <p className="max-w-3xl text-xs text-slate-500">
        Every figure here is <span className="text-slate-400">in-sample</span>, the best one
        included: these runs were scored on the same data the grid searched. Nothing on this screen
        is evidence that the winning parameters will work next month — that needs a walk-forward,
        which chooses on one window and measures on the next.
        {aggregate.points_failed > 0 &&
          ` ${count(aggregate.points_failed)} point(s) failed to run and are not scored.`}
      </p>
    </div>
  )
}
