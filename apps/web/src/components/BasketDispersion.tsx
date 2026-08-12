import type { BasketAggregate } from '../api/types'
import { count, percent, sign } from '../format'

const toneClass = { up: 'text-emerald-400', down: 'text-red-400', flat: 'text-slate-100' } as const

// `note` and `tone` are `| undefined` rather than merely optional: under
// `exactOptionalPropertyTypes` a present-but-undefined prop is not the same as an absent one, and
// a symbol that has no best market yet passes `undefined` on purpose.
function Tile(props: {
  label: string
  value: string
  note?: string | undefined
  tone?: keyof typeof toneClass | undefined
}): React.JSX.Element {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <div className="text-xs tracking-wide text-slate-400 uppercase">{props.label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${toneClass[props.tone ?? 'flat']}`}>
        {props.value}
      </div>
      {props.note !== undefined && <div className="mt-1 text-xs text-slate-400">{props.note}</div>}
    </div>
  )
}

/** A return that may not exist yet. Undefined is a dash, never 0% — see the header comment. */
function ret(fraction: string | null): { value: string; tone: keyof typeof toneClass } {
  if (fraction === null) return { value: '—', tone: 'flat' }
  return { value: percent(fraction), tone: sign(fraction) }
}

/**
 * How far apart a basket's markets landed — **dispersion, never a combined account.**
 *
 * ⚠️ There is no total here and no summed curve, and their absence is the most important thing
 * about this component. Every run in a basket starts with the whole initial capital, so four runs
 * of $10 000 are neither a $10 000 account nor a $40 000 one. A headline "total return" would be
 * a number with no referent, drawn convincingly enough that nobody would question it.
 *
 * The **median**, not the mean. A basket exists to expose the market where a strategy falls
 * apart, and the average is precisely the statistic that hides one catastrophic symbol behind
 * three good ones: +30%, −25%, +1% and +2% averages to a respectable +2% and means nothing.
 *
 * The extremes carry **names**, because "worst: −25%" sends the reader looking and "worst: EURUSD
 * −25%" tells them where to look.
 *
 * Every statistic is a dash until a run finishes. Undefined is not zero: a basket whose runs are
 * still queued has not returned 0%, and rendering it as such would rank an experiment that has
 * not started alongside one that broke even in every market.
 */
export function BasketDispersion({ aggregate }: { aggregate: BasketAggregate }): React.JSX.Element {
  const median = ret(aggregate.median_return)
  const best = ret(aggregate.best_return)
  const worst = ret(aggregate.worst_return)
  const pending = aggregate.runs_total - aggregate.runs_finished - aggregate.runs_failed

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
        <Tile
          label="Median market"
          value={median.value}
          tone={median.tone}
          note="the typical market, not the average"
        />
        <Tile
          label="Best"
          value={best.value}
          tone={best.tone}
          note={aggregate.best_symbol ?? undefined}
        />
        <Tile
          label="Worst"
          value={worst.value}
          tone={worst.tone}
          note={aggregate.worst_symbol ?? undefined}
        />
        <Tile
          label="Profitable"
          value={`${count(aggregate.runs_profitable)} / ${count(aggregate.runs_finished)}`}
          note={
            pending > 0
              ? `${count(pending)} still running`
              : aggregate.runs_failed > 0
                ? `${count(aggregate.runs_failed)} failed`
                : 'all markets finished'
          }
        />
      </div>
      {/* Stated on the screen, not only in the code: a reader who sees four curves on one chart
          will reach for the sum unless told why there isn't one. */}
      <p className="text-xs text-slate-500">
        Each market ran on its own full capital, so these returns are comparable with each other
        but never additive — a basket is a spread of outcomes, not a portfolio.
      </p>
    </div>
  )
}
