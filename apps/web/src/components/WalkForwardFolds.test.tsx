import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { WalkForwardFold } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { WalkForwardFolds } from './WalkForwardFolds'

function fold(index: number, over: Partial<WalkForwardFold> = {}): WalkForwardFold {
  return {
    index,
    study_id: `study-${String(index)}`,
    train_from: '2024-01-01T00:00:00+00:00',
    train_to: '2024-02-29T23:00:00+00:00',
    test_from: '2024-03-01T00:00:00+00:00',
    test_to: '2024-03-31T23:00:00+00:00',
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

describe('WalkForwardFolds', () => {
  it('shows both windows on one row, so the reader can check they do not overlap', () => {
    // ⚠️ The single claim the whole feature rests on, put where it can be verified rather than
    // trusted. A table showing only the result would be asking to be believed about exactly the
    // thing that cannot be seen from a result.
    renderWithProviders(<WalkForwardFolds folds={[fold(0)]} />)

    const row = screen.getAllByRole('row')[1]!
    expect(within(row).getByText('2024-01-01 → 2024-02-29')).toBeInTheDocument()
    expect(within(row).getByText('2024-03-01 → 2024-03-31')).toBeInTheDocument()
  })

  it('shows the bar counts, because the windows were cut by counting bars', () => {
    // Without them, two folds spanning different amounts of calendar look like a bug rather
    // than like a market with a holiday week in it.
    renderWithProviders(<WalkForwardFolds folds={[fold(0)]} />)

    expect(screen.getByText('1,200 bars')).toBeInTheDocument()
    expect(screen.getByText('400 bars')).toBeInTheDocument()
  })

  it('numbers the folds from one, not from zero', () => {
    renderWithProviders(<WalkForwardFolds folds={[fold(0), fold(1)]} />)

    const cells = screen.getAllByRole('row').slice(1).map((row) => within(row).getAllByRole('cell')[0])
    expect(cells.map((cell) => cell?.textContent)).toEqual(['1', '2'])
  })

  it('links each training window to its own grid and each test to its run', () => {
    // The link is the evidence: fold 1's heatmap beside fold 5's is what over-fitting looks
    // like, and no number on this screen states it as plainly.
    renderWithProviders(<WalkForwardFolds folds={[fold(0)]} />)

    expect(screen.getByRole('link', { name: /2024-01-01/ })).toHaveAttribute(
      'href',
      '/studies/study-0',
    )
    expect(screen.getByRole('link', { name: /2024-03-01/ })).toHaveAttribute(
      'href',
      '/results/run-0',
    )
  })

  it('says a fold chose nothing rather than leaving the cell blank', () => {
    // ⚠️ An undecided fold is a **result** — nothing in the grid traded that window. A blank
    // cell reads as one that is still loading, which is the opposite of what happened.
    renderWithProviders(
      <WalkForwardFolds
        folds={[
          fold(0, {
            chosen_label: null,
            chosen_strategy_id: null,
            test_backtest_id: null,
            in_sample_return: null,
            out_of_sample_return: null,
            test_status: null,
            test_trades: null,
          }),
        ]}
      />,
    )

    expect(screen.getByText('chose nothing')).toBeInTheDocument()
    // And with no run to open, the test window is plain text rather than a dead link.
    expect(screen.queryByRole('link', { name: /2024-03-01/ })).not.toBeInTheDocument()
  })

  it('shows a dash for a return that has not landed, never a zero', () => {
    renderWithProviders(
      <WalkForwardFolds
        folds={[fold(0, { out_of_sample_return: null, test_status: 'running', test_trades: null })]}
      />,
    )

    const row = screen.getAllByRole('row')[1]!
    expect(within(row).getAllByText('—')).toHaveLength(2)
    expect(within(row).queryByText('0.0%')).not.toBeInTheDocument()
  })
})
