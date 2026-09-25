import { describe, expect, it } from 'vitest'

import type { Metrics } from '../api/types'

import { RANKINGS, rankingOf } from './ranking'

function metrics(over: Partial<Metrics>): Metrics {
  return {
    net_profit: '0',
    gross_profit: '0',
    gross_loss: '0',
    total_trades: 4,
    long_trades: 4,
    short_trades: 0,
    win_rate: '0.5',
    payoff: null,
    profit_factor: '1',
    expectancy: '0',
    max_drawdown_abs: '0',
    max_drawdown_pct: '0.1',
    max_dd_duration_days: 0,
    sharpe: null,
    sortino: null,
    cagr: null,
    avg_trade_duration: null,
    ...over,
  }
}

function score(key: Parameters<typeof rankingOf>[0], over: Partial<Metrics>): number | null {
  return rankingOf(key).score(metrics(over))
}

describe('the measures a run is ranked by', () => {
  it('reads a return as a number, not as text', () => {
    expect(score('return', { net_profit: '10' })).toBeGreaterThan(score('return', { net_profit: '9' }) ?? 0)
  })

  it('reads a profit factor with gains and no loss as the best there is, and one with neither as nothing', () => {
    expect(score('profit_factor', { profit_factor: null, gross_profit: '50' })).toBe(
      Number.POSITIVE_INFINITY,
    )
    expect(score('profit_factor', { profit_factor: null, gross_profit: '0' })).toBeNull()
    expect(score('profit_factor', { profit_factor: '1.5' })).toBe(1.5)
  })

  it('has no win rate over no trade, and no expectancy where the engine left none', () => {
    expect(score('win_rate', { total_trades: 0, win_rate: '0' })).toBeNull()
    expect(score('win_rate', { win_rate: '0.4' })).toBe(0.4)
    expect(score('expectancy', { expectancy: null })).toBeNull()
  })

  it('ranks the smallest drawdown highest', () => {
    expect(score('drawdown', { max_drawdown_pct: '0.05' })).toBeGreaterThan(
      score('drawdown', { max_drawdown_pct: '0.6' }) ?? 0,
    )
  })

  it('reads the risk in R, and a run recorded before it as nothing to rank by (25/09)', () => {
    expect(score('net_r', { net_r: '12' })).toBe(12)
    expect(score('net_r', {})).toBeNull()
    expect(score('positive_years', { positive_year_share: '0.75' })).toBe(0.75)
    expect(score('positive_years', { positive_year_share: null })).toBeNull()
    expect(score('drawdown_r', { max_drawdown_r: '4' })).toBeGreaterThan(
      score('drawdown_r', { max_drawdown_r: '25' }) ?? 0,
    )
    expect(score('drawdown_r', {})).toBeNull()
  })

  it('scores recovery as net R over drawdown R, with the no-drawdown cases the server uses', () => {
    // The steadier run wins: +12 R through 4 R of drawdown against +20 R through 25 R.
    expect(score('recovery_r', { net_r: '12', max_drawdown_r: '4' })).toBe(3)
    expect(score('recovery_r', { net_r: '20', max_drawdown_r: '25' })).toBe(0.8)
    expect(score('recovery_r', { net_r: '3', max_drawdown_r: '0' })).toBe(Number.POSITIVE_INFINITY)
    expect(score('recovery_r', { net_r: '0', max_drawdown_r: '0' })).toBeNull()
    expect(score('recovery_r', {})).toBeNull()
  })

  it('offers every measure it can rank by, each once', () => {
    expect(RANKINGS.map((one) => one.label)).toEqual([
      'Return',
      'Profit factor',
      'Win rate',
      'Expectancy',
      'Smallest drawdown',
      'Net R',
      'Net R per R of drawdown',
      'Share of years positive',
      'Smallest drawdown in R',
    ])
  })

  it('refuses a measure it does not know', () => {
    expect(() => rankingOf('nope' as never)).toThrow(/unknown ranking/)
  })
})
