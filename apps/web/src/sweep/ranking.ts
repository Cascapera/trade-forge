// The measures a sweep entry's runs can be ranked by — their names, and what each one scores.
//
// ⚠️ **The ranking itself is the server's since 24/09** (`GET /sweeps/{id}/runs`): the screen used
// to hold every run and sort them here, which on a sweep of 22 thousand runs meant 89 MB a poll.
// `score` stays as the written statement of each rule — the server's order is held to it by the
// API's own tests — and is what a single run's measure reads as.

import type { Metrics } from '../api/types'

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

export const RUNS_PER_PAGE = 10
