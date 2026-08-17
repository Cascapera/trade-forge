// Turning a walk-forward's numbers into the two claims a reader should leave with. Pure, so the
// claims can be tested directly — they are the part of this screen that can be wrong while every
// number on it is right.
//
// ⚠️ **Deliberately free of thresholds.** It would be easy to write "degradation over 50% means
// over-fitted", and that number would be invented here and then quoted as if it were measured.
// Every judgement below is a comparison the data itself settles: is the out-of-sample median
// above zero, did the folds pick the same point or different ones, what fraction of the promise
// arrived. Where a fraction cannot be formed honestly, the answer is null and the screen shows
// the raw numbers instead of a verdict.

import type { WalkForwardFold, WalkForwardVerdict } from '../api/types'

export type Stability = 'unknown' | 'stable' | 'partly' | 'unstable'

/**
 * Did the folds agree on which parameters to trade?
 *
 * The question a heatmap cannot answer and the reason a walk-forward reports more than a return.
 * One point winning every fold is the strongest evidence a grid can give that it found something
 * about the method. A different winner every fold means there is no "the parameters" to go and
 * trade — and that is true even when the out-of-sample result is positive, which is exactly the
 * case where a reader would otherwise stop reading.
 */
export function stability(verdict: WalkForwardVerdict): Stability {
  // Fewer than two decided folds cannot disagree, so calling that "stable" would be a claim made
  // from one observation. Undecided folds are a finding of their own and the screen says so
  // separately.
  if (verdict.folds_decided < 2) return 'unknown'
  if (verdict.distinct_choices === 1) return 'stable'
  if (verdict.distinct_choices === verdict.folds_decided) return 'unstable'
  return 'partly'
}

export type Survival = 'unknown' | 'kept' | 'lost'

/**
 * Did anything survive being chosen blind?
 *
 * Read off the **out-of-sample median**, not the compounded figure: with a handful of folds one
 * spectacular window moves a product far more than it moves a middle value, and the spectacular
 * window is the one most likely to have been luck.
 */
export function survival(verdict: WalkForwardVerdict): Survival {
  if (verdict.folds_scored === 0 || verdict.out_of_sample_median === null) return 'unknown'
  return Number(verdict.out_of_sample_median) > 0 ? 'kept' : 'lost'
}

/**
 * What fraction of the in-sample promise actually arrived, or `null` when the question is
 * meaningless.
 *
 * ⚠️ **A `number`, and therefore a float** — `0.15 / 0.10` is `1.4999999999999998`, not `1.5`.
 * Acceptable here and nowhere else in this codebase: this value is rendered and never decides
 * anything, which is the same line `format.ts` draws when it parses an exact-decimal string to
 * display it. Every figure that *decides* something was computed as a `Decimal` on the backend
 * and crosses the wire as a string.
 *
 * `out / in`, and the null cases are the point. If the in-sample median was zero the ratio is
 * undefined; if it was **negative** the ratio is worse than undefined — it inverts, so a fold set
 * that lost money in training and lost less out of sample would report a cheerful fraction. A
 * grid whose median point lost money has no promise to keep, and the screen should say the
 * numbers rather than a percentage of them.
 */
export function retained(verdict: WalkForwardVerdict): number | null {
  const promise = verdict.in_sample_median
  const delivery = verdict.out_of_sample_median
  if (promise === null || delivery === null) return null
  const inside = Number(promise)
  if (inside <= 0) return null
  return Number(delivery) / inside
}

/**
 * The folds a reader can act on: decided, and with an out-of-sample number.
 *
 * Split out because "how many folds are there" and "how many folds said anything" are different
 * questions and the screen shows both. A fold that chose nothing is not a fold that is loading.
 */
export function scored(folds: readonly WalkForwardFold[]): WalkForwardFold[] {
  return folds.filter((fold) => fold.out_of_sample_return !== null)
}

/**
 * How many folds are still working — total minus the ones that reached an answer of either kind.
 *
 * ⚠️ An **undecided** fold counts as finished, not pending. Nothing in the grid traded its
 * window, and no amount of waiting changes that; counting it as pending would leave the screen
 * saying "1 fold still running" for ever.
 */
export function pending(verdict: WalkForwardVerdict, folds: readonly WalkForwardFold[]): number {
  const settled = folds.filter(
    (fold) => fold.out_of_sample_return !== null || fold.test_status === 'failed',
  ).length
  const undecided = verdict.folds_total - verdict.folds_decided
  return Math.max(0, verdict.folds_total - settled - undecided)
}
