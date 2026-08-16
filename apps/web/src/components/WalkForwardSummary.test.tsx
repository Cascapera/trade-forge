import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { WalkForwardFold, WalkForwardVerdict } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { WalkForwardSummary } from './WalkForwardSummary'

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

function fold(over: Partial<WalkForwardFold> = {}): WalkForwardFold {
  return {
    index: 0,
    study_id: 'study-0',
    train_from: '2024-01-01T00:00:00+00:00',
    train_to: '2024-03-01T00:00:00+00:00',
    test_from: '2024-03-01T01:00:00+00:00',
    test_to: '2024-04-01T00:00:00+00:00',
    train_bars: 1200,
    test_bars: 400,
    chosen_label: 'period=9',
    chosen_strategy_id: 'strategy-1',
    test_backtest_id: 'run-0',
    in_sample_return: '0.20',
    out_of_sample_return: '0.05',
    test_status: 'done',
    test_trades: 30,
    ...over,
  }
}

describe('WalkForwardSummary', () => {
  it('shows the promise beside the delivery, which is the whole comparison', () => {
    renderWithProviders(<WalkForwardSummary verdict={verdict()} folds={[fold()]} />)

    expect(screen.getByText('Median in sample')).toBeInTheDocument()
    expect(screen.getByText('20.0%')).toBeInTheDocument()
    expect(screen.getByText('Median out of sample')).toBeInTheDocument()
    expect(screen.getByText('5.0%')).toBeInTheDocument()
    // A quarter of the promise arrived: 0.05 / 0.20.
    expect(screen.getByText('25%')).toBeInTheDocument()
  })

  it('shows the numbers instead of a share when there was no promise to keep', () => {
    // ⚠️ A grid whose median point *lost* money has nothing to keep a fraction of, and the
    // fraction inverts if formed anyway — two losses would report as a cheerful "kept 25%".
    renderWithProviders(
      <WalkForwardSummary
        verdict={verdict({ in_sample_median: '-0.20', out_of_sample_median: '-0.05' })}
        folds={[fold()]}
      />,
    )

    expect(screen.getByText(/No positive in-sample median/)).toBeInTheDocument()
    expect(screen.queryByText('25%')).not.toBeInTheDocument()
  })

  it('says the parameters were stable only when every fold agreed', () => {
    renderWithProviders(
      <WalkForwardSummary verdict={verdict({ distinct_choices: 1 })} folds={[fold()]} />,
    )

    expect(screen.getByText('Every fold chose the same parameters')).toBeInTheDocument()
  })

  it('calls out a different winner in every fold even when the return is positive', () => {
    // ⚠️ The case the whole tile exists for. A positive out-of-sample median is where a reader
    // stops reading, and it is precisely where "there is no parameter set to trade" matters.
    renderWithProviders(
      <WalkForwardSummary
        verdict={verdict({ distinct_choices: 6, out_of_sample_median: '0.05' })}
        folds={[fold()]}
      />,
    )

    expect(screen.getByText('A different winner in every fold')).toBeInTheDocument()
  })

  it('says the median fold lost money when it did', () => {
    renderWithProviders(
      <WalkForwardSummary
        verdict={verdict({ out_of_sample_median: '-0.03' })}
        folds={[fold()]}
      />,
    )

    expect(screen.getByText(/did not survive being chosen blind/)).toBeInTheDocument()
  })

  it('reports folds that chose nothing as a finding rather than a gap', () => {
    renderWithProviders(
      <WalkForwardSummary
        verdict={verdict({ folds_decided: 4 })}
        folds={[fold({ chosen_label: null })]}
      />,
    )

    expect(screen.getByText(/2 fold\(s\) chose nothing/)).toBeInTheDocument()
  })

  it('warns when a fold was scored on almost no trades', () => {
    // A fold that took two trades has a return, not a finding — and its number sits in the
    // median looking exactly like one built from thirty.
    renderWithProviders(
      <WalkForwardSummary verdict={verdict()} folds={[fold({ test_trades: 2 })]} />,
    )

    expect(screen.getByText(/very few trades/)).toBeInTheDocument()
  })

  it('shows dashes rather than zeroes before anything has landed', () => {
    renderWithProviders(
      <WalkForwardSummary
        verdict={verdict({
          folds_decided: 0,
          folds_scored: 0,
          folds_profitable: 0,
          in_sample_median: null,
          out_of_sample_median: null,
          degradation: null,
          compounded: null,
          distinct_choices: 0,
        })}
        folds={[]}
      />,
    )

    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(5)
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
  })

  it('says the out-of-sample figures came from earlier candles only', () => {
    // The sentence is the claim the numbers rest on. Without it the screen is just two columns.
    renderWithProviders(<WalkForwardSummary verdict={verdict()} folds={[fold()]} />)

    expect(screen.getByText(/earlier candles only/)).toBeInTheDocument()
  })
})
