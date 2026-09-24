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

  it('offers every measure it can rank by, each once', () => {
    expect(RANKINGS.map((one) => one.label)).toEqual([
      'Return',
      'Profit factor',
      'Win rate',
      'Expectancy',
      'Smallest drawdown',
    ])
  })

  it('refuses a measure it does not know', () => {
    expect(() => rankingOf('nope' as never)).toThrow(/unknown ranking/)
  })
})
