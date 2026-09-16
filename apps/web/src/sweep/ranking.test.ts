import { describe, expect, it } from 'vitest'

import type { BacktestListItem, Metrics } from '../api/types'

import { RANKINGS, pageOf, rank } from './ranking'

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

function run(id: string, over: Partial<Metrics> | null): BacktestListItem {
  return { id, metrics: over === null ? null : metrics(over) } as BacktestListItem
}

function ids(runs: BacktestListItem[]): string[] {
  return runs.map((one) => one.id)
}

describe('rank', () => {
  it('puts the largest return first, and a string compare would not', () => {
    // As strings, "9" > "10" > "-5"; as numbers, 10 > 9 > -5.
    const runs = [run('nine', { net_profit: '9' }), run('ten', { net_profit: '10' }), run('neg', { net_profit: '-5' })]

    expect(ids(rank(runs, 'return'))).toEqual(['ten', 'nine', 'neg'])
  })

  it('puts unfinished runs last, in the order they arrived', () => {
    const runs = [run('q1', null), run('low', { net_profit: '-1' }), run('q2', null), run('high', { net_profit: '1' })]

    expect(ids(rank(runs, 'return'))).toEqual(['high', 'low', 'q1', 'q2'])
  })

  it('keeps ties in arrival order', () => {
    const runs = [run('b', { net_profit: '5' }), run('a', { net_profit: '5' })]

    expect(ids(rank(runs, 'return'))).toEqual(['b', 'a'])
  })

  it('does not reorder the list it was given', () => {
    const runs = [run('low', { net_profit: '1' }), run('high', { net_profit: '2' })]

    rank(runs, 'return')

    expect(ids(runs)).toEqual(['low', 'high'])
  })

  it('ranks a run that never lost above every finite profit factor', () => {
    // The engine leaves the factor empty when there was no loss; with gains that is the best case.
    const runs = [
      run('good', { profit_factor: '3.5' }),
      run('flawless', { profit_factor: null, gross_profit: '400' }),
      run('idle', { profit_factor: null, gross_profit: '0', total_trades: 0 }),
      run('poor', { profit_factor: '0.4' }),
    ]

    expect(ids(rank(runs, 'profit_factor'))).toEqual(['flawless', 'good', 'poor', 'idle'])
  })

  it('does not rank a run that never traded by its stored win rate of zero', () => {
    // `lost` traded and won nothing: a real 0 %, ranked. `idle` arrives first with the same stored
    // 0, so ranking it would keep it ahead of `lost` — only leaving it unranked puts it last.
    const runs = [
      run('idle', { total_trades: 0, win_rate: '0' }),
      run('lost', { win_rate: '0' }),
      run('low', { win_rate: '0.1' }),
      run('high', { win_rate: '0.9' }),
    ]

    expect(ids(rank(runs, 'win_rate'))).toEqual(['high', 'low', 'lost', 'idle'])
  })

  it('ranks by expectancy and leaves a missing one last', () => {
    const runs = [
      run('none', { expectancy: null }),
      run('neg', { expectancy: '-2' }),
      run('pos', { expectancy: '3' }),
    ]

    expect(ids(rank(runs, 'expectancy'))).toEqual(['pos', 'neg', 'none'])
  })

  it('puts the smallest drawdown first', () => {
    const runs = [
      run('deep', { max_drawdown_pct: '0.6' }),
      run('shallow', { max_drawdown_pct: '0.05' }),
      run('mid', { max_drawdown_pct: '0.2' }),
    ]

    expect(ids(rank(runs, 'drawdown'))).toEqual(['shallow', 'mid', 'deep'])
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
})

describe('pageOf', () => {
  const items = Array.from({ length: 25 }, (_, i) => i + 1)

  it('slices ten at a time and says which positions are shown', () => {
    expect(pageOf(items, 0)).toEqual({
      items: [1, 2, 3, 4, 5, 6, 7, 8, 9, 10],
      index: 0,
      pages: 3,
      first: 1,
      last: 10,
    })
    const lastPage = pageOf(items, 2)
    expect(lastPage.items).toEqual([21, 22, 23, 24, 25])
    expect([lastPage.first, lastPage.last]).toEqual([21, 25])
  })

  it('clamps a page past either end', () => {
    expect(pageOf(items, 9).index).toBe(2)
    expect(pageOf(items, -1).index).toBe(0)
  })

  it('is one empty page when there is nothing', () => {
    expect(pageOf([], 0)).toEqual({ items: [], index: 0, pages: 1, first: 0, last: 0 })
  })

  it('fits exactly ten on one page', () => {
    expect(pageOf(items.slice(0, 10), 0).pages).toBe(1)
    expect(pageOf(items.slice(0, 11), 0).pages).toBe(2)
  })
})
