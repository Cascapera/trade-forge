// Ranking a sweep entry's runs, best first, by a measure the reader picks — and paging through them.
//
// ⚠️ **Done on the screen, not on the server.** The sweep page already holds every run (the
// counter and the chart read them), so sorting and slicing here costs nothing and changes with
// no round trip. A server-side page would be a second request for data already in hand.

import type { BacktestListItem, Metrics } from '../api/types'

export type RankKey = 'return' | 'profit_factor' | 'win_rate' | 'expectancy' | 'drawdown'

export interface Ranking {
  key: RankKey
  label: string
  /** A larger score ranks higher. Null means "nothing to rank by" and always goes last. */
  score: (metrics: Metrics) => number | null
}

/**
 * A profit factor, with the case the engine leaves empty put back where it belongs.
 *
 * ⚠️ The engine writes `null` when there was **no losing trade** (`gross_profit / -gross_loss`
 * has no denominator). A run that only won is then the best possible run by this measure, and
 * ranking its null last would bury exactly the run the reader is looking for. So: gains and no
 * loss is +∞; no gain and no loss (no trade, or only break-evens) has nothing to say.
 */
function profitFactor(metrics: Metrics): number | null {
  if (metrics.profit_factor !== null) return Number(metrics.profit_factor)
  return Number(metrics.gross_profit) > 0 ? Number.POSITIVE_INFINITY : null
}

export const RANKINGS: readonly Ranking[] = [
  { key: 'return', label: 'Return', score: (m) => Number(m.net_profit) },
  { key: 'profit_factor', label: 'Profit factor', score: profitFactor },
  {
    key: 'win_rate',
    label: 'Win rate',
    // A run that never traded has a stored win rate of 0, which is not a measurement.
    score: (m) => (m.total_trades > 0 ? Number(m.win_rate) : null),
  },
  {
    key: 'expectancy',
    label: 'Expectancy',
    score: (m) => (m.expectancy === null ? null : Number(m.expectancy)),
  },
  // Smaller is better, so the score is the drawdown negated.
  { key: 'drawdown', label: 'Smallest drawdown', score: (m) => -Number(m.max_drawdown_pct) },
]

export function rankingOf(key: RankKey): Ranking {
  const found = RANKINGS.find((one) => one.key === key)
  if (found === undefined) throw new Error(`unknown ranking: ${key}`)
  return found
}

/**
 * The runs, best first by `key`.
 *
 * Runs with nothing to rank by — unfinished, or with no score under this measure — come last, in
 * the order they arrived. Ties keep their arrival order too (the sort is stable), so the same
 * data always ranks the same way.
 */
export function rank(runs: readonly BacktestListItem[], key: RankKey): BacktestListItem[] {
  const { score } = rankingOf(key)
  const scored = runs.map((run) => ({
    run,
    value: run.metrics === null ? null : score(run.metrics),
  }))
  const ranked = scored.filter((one): one is { run: BacktestListItem; value: number } => one.value !== null)
  const unranked = scored.filter((one) => one.value === null)
  ranked.sort((a, b) => (a.value === b.value ? 0 : a.value > b.value ? -1 : 1))
  return [...ranked, ...unranked].map((one) => one.run)
}

export const RUNS_PER_PAGE = 10

export interface Page<T> {
  items: T[]
  /** Zero-based, and clamped: a page past the end becomes the last page. */
  index: number
  pages: number
  /** 1-based position of the first and last item shown; 0 and 0 when there is nothing. */
  first: number
  last: number
}

export function pageOf<T>(items: readonly T[], index: number, size = RUNS_PER_PAGE): Page<T> {
  const pages = Math.max(1, Math.ceil(items.length / size))
  const clamped = Math.min(Math.max(0, index), pages - 1)
  const start = clamped * size
  const shown = items.slice(start, start + size)
  return {
    items: shown,
    index: clamped,
    pages,
    first: shown.length === 0 ? 0 : start + 1,
    last: start + shown.length,
  }
}
