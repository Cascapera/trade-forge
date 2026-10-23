import { describe, expect, it } from 'vitest'

import type { WalkForwardFold, WalkForwardVerdict } from '../api/types'

import { pending, retained, scored, stability, survival } from './report'

function verdict(over: Partial<WalkForwardVerdict> = {}): WalkForwardVerdict {
  return {
    folds_total: 6,
    folds_decided: 6,
    folds_scored: 6,
    folds_profitable: 4,
    in_sample_median: '0.20',
    out_of_sample_median: '0.05',
    degradation: '-0.15',
    compounded: '0.28',
    distinct_choices: 2,
    ...over,
  }
}

function fold(index: number, over: Partial<WalkForwardFold> = {}): WalkForwardFold {
  return {
    index,
    study_id: `study-${String(index)}`,
    train_from: '2024-01-01T00:00:00+00:00',
    train_to: '2024-03-01T00:00:00+00:00',
    test_from: '2024-03-01T01:00:00+00:00',
    test_to: '2024-04-01T00:00:00+00:00',
    train_bars: 1200,
    test_bars: 400,
    chosen_label: 'period=9',
    chosen_strategy_id: 'strategy-1',
    test_backtest_id: `run-${String(index)}`,
    in_sample_return: '0.20',
    out_of_sample_return: '0.05',
    test_status: 'done',
    test_trades: 30,
    ...over,
  }
}

describe('stability', () => {
  it('calls it stable only when every decided fold picked the same point', () => {
    // The strongest evidence a grid can give: six independent searches, one answer.
    expect(stability(verdict({ distinct_choices: 1 }))).toBe('stable')
  })

  it('calls it unstable when the winner changed every fold', () => {
    // ⚠️ True regardless of the returns, which is why it gets its own reading. A positive
    // out-of-sample result with a different winner every fold means there is no "the
    // parameters" to go and trade — and that is exactly the case where a reader stops reading.
    expect(stability(verdict({ distinct_choices: 6, folds_decided: 6 }))).toBe('unstable')
  })

  it('calls it partly when the folds neither agreed nor all differed', () => {
    expect(stability(verdict({ distinct_choices: 3, folds_decided: 6 }))).toBe('partly')
  })

  it('refuses to judge stability from a single decided fold', () => {
    // One observation cannot agree or disagree with anything. Reporting "stable" here would be
    // a claim manufactured out of a sample of one — and `distinct_choices` really is 1.
    expect(stability(verdict({ folds_decided: 1, distinct_choices: 1 }))).toBe('unknown')
    expect(stability(verdict({ folds_decided: 0, distinct_choices: 0 }))).toBe('unknown')
  })
})

describe('survival', () => {
  it('reads the out-of-sample median rather than the compounded figure', () => {
    // ⚠️ The fixture separates the two on purpose: a *negative* median with a **positive**
    // compounded result is exactly what one lucky fold looks like, and it is the case where
    // reading the product would report a method that mostly lost as one that worked.
    expect(survival(verdict({ out_of_sample_median: '-0.02', compounded: '0.40' }))).toBe('lost')
  })

  it('says nothing at all until a fold has been scored', () => {
    expect(survival(verdict({ folds_scored: 0, out_of_sample_median: null }))).toBe('unknown')
  })

  it('treats exactly zero as not surviving', () => {
    // A method that returned nothing out of sample did not keep anything. Rounding zero upward
    // into "kept" would be generosity applied to the one number that must not receive any.
    expect(survival(verdict({ out_of_sample_median: '0' }))).toBe('lost')
  })
})

describe('retained', () => {
  it('is the share of the in-sample median that arrived out of sample', () => {
    expect(retained(verdict({ in_sample_median: '0.20', out_of_sample_median: '0.05' }))).toBe(0.25)
  })

  it('refuses to form a fraction of a promise that was never positive', () => {
    // ⚠️ The negative case is the dangerous one, and it inverts. A grid whose median point lost
    // 20% in training and lost 5% out of sample would report "kept 25%" — a cheerful number
    // describing two losses. Null forces the screen to show the numbers instead.
    expect(retained(verdict({ in_sample_median: '-0.20', out_of_sample_median: '-0.05' }))).toBeNull()
    expect(retained(verdict({ in_sample_median: '0' }))).toBeNull()
    expect(retained(verdict({ in_sample_median: null }))).toBeNull()
    expect(retained(verdict({ out_of_sample_median: null }))).toBeNull()
  })

  it('can exceed one, and says so rather than capping it', () => {
    // Doing better out of sample than in is unusual and worth seeing. Clamping it to 100% would
    // hide the one result that should make a reader suspicious of the split itself.
    //
    // ⚠️ `toBeCloseTo`, because this is a float: `0.15 / 0.10` is `1.4999999999999998`. That is
    // fine for a number that is only ever rendered — and it is exactly why nothing that decides
    // anything is computed this side of the wire.
    expect(
      retained(verdict({ in_sample_median: '0.10', out_of_sample_median: '0.15' })),
    ).toBeCloseTo(1.5)
  })
})

describe('scored and pending', () => {
  it('counts only the folds that produced an out-of-sample number', () => {
    const folds = [fold(0), fold(1, { out_of_sample_return: null, test_status: 'queued' })]

    expect(scored(folds).map((row) => row.index)).toEqual([0])
  })

  it('does not count an undecided fold as one still running', () => {
    // ⚠️ Nothing in the grid traded that window, and no amount of waiting changes it. Counted as
    // pending, the screen would say "1 fold still running" for ever — and the poll banner is the
    // one piece of the screen a reader uses to decide whether to wait.
    const folds = [
      fold(0),
      fold(1, {
        chosen_label: null,
        chosen_strategy_id: null,
        test_backtest_id: null,
        in_sample_return: null,
        out_of_sample_return: null,
        test_status: null,
        test_trades: null,
      }),
    ]

    expect(pending(verdict({ folds_total: 2, folds_decided: 1, folds_scored: 1 }), folds)).toBe(0)
  })

  it('counts a fold whose test run has not landed', () => {
    const folds = [
      fold(0),
      fold(1, { out_of_sample_return: null, test_status: 'running', test_trades: null }),
    ]

    expect(pending(verdict({ folds_total: 2, folds_decided: 2, folds_scored: 1 }), folds)).toBe(1)
  })

  it('counts a failed test run as settled rather than pending', () => {
    // A failed run is a terminal answer. Waiting on it would be waiting on nothing.
    const folds = [
      fold(0),
      fold(1, { out_of_sample_return: null, test_status: 'failed', test_trades: null }),
    ]

    expect(pending(verdict({ folds_total: 2, folds_decided: 2, folds_scored: 1 }), folds)).toBe(0)
  })
})
