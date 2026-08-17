import { screen } from '@testing-library/react'

import type { WalkForwardFold, WalkForwardOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({ useWalkForward: vi.fn() }))

import { useWalkForward } from '../api/hooks'

import { WalkForwardResult } from './WalkForwardResult'

const mocked = vi.mocked(useWalkForward)

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

function walkForward(over: Partial<WalkForwardOut> = {}): WalkForwardOut {
  const folds = over.folds ?? [fold(0), fold(1)]
  return {
    id: 'wf-1',
    study_id: 'study-base',
    strategy_id: 'base',
    strategy_name: 'MME9 breakout',
    symbol: 'AAPL',
    timeframe: 'H1',
    initial_capital: '10000',
    grid: { 'setup.params.period': [5, 9] },
    folds_requested: 2,
    train_multiple: 3,
    anchored: false,
    metric: 'net_profit',
    status: 'done',
    error: null,
    created_at: '2024-01-01T00:00:00Z',
    started_at: '2024-01-01T00:00:00Z',
    finished_at: '2024-01-01T00:05:00Z',
    verdict: {
      folds_total: folds.length,
      folds_decided: folds.filter((one) => one.chosen_label !== null).length,
      folds_scored: folds.filter((one) => one.out_of_sample_return !== null).length,
      folds_profitable: 2,
      in_sample_median: '0.20',
      out_of_sample_median: '0.05',
      degradation: '-0.15',
      compounded: '0.10',
      distinct_choices: 1,
    },
    ...over,
    folds,
  }
}

function showing(data: WalkForwardOut | undefined, state: 'pending' | 'error' | 'ok' = 'ok'): void {
  mocked.mockReturnValue({
    data,
    isPending: state === 'pending',
    isError: state === 'error',
    error: state === 'error' ? new Error('nope') : null,
  } as unknown as ReturnType<typeof useWalkForward>)
}

beforeEach(() => {
  vi.clearAllMocks()
})

describe('WalkForwardResult', () => {
  it('says how it was cut, because the cut is what the numbers mean', () => {
    // Rolling versus anchored and the training multiple are not settings a reader can shrug at:
    // they decide what each fold's choice was made from, and two walk-forwards of one study can
    // disagree entirely because of them.
    showing(walkForward())

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByText(/rolling/)).toBeInTheDocument()
    expect(screen.getByText(/chosen by net profit/)).toBeInTheDocument()
  })

  it('links back to the grid it re-ran', () => {
    showing(walkForward())

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByRole('link', { name: /the grid you searched/ })).toHaveAttribute(
      'href',
      '/studies/study-base',
    )
  })

  it('says how many folds are still working rather than showing dashes in silence', () => {
    // ⚠️ Minutes, not seconds — each fold searches the whole grid before it can choose. A screen
    // of dashes without a reason reads as an experiment that produced nothing.
    showing(
      walkForward({
        folds: [
          fold(0),
          fold(1, { out_of_sample_return: null, test_status: 'running', test_trades: null }),
        ],
        status: 'running',
      }),
    )

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByRole('status')).toHaveTextContent('1 of 2 folds still running')
  })

  it('does not count an undecided fold as one still running', () => {
    // Nothing in that grid traded that window, and waiting will not change it. Counted as
    // pending, the banner would never go away.
    showing(
      walkForward({
        folds: [
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
        ],
      }),
    )

    renderWithProviders(<WalkForwardResult />)

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('chose nothing')).toBeInTheDocument()
  })

  it('shows the folds that finished when the experiment stopped early', () => {
    // A failed walk-forward is not an empty one. Hiding the folds that landed would throw away
    // real out-of-sample results because a later one crashed.
    showing(walkForward({ status: 'failed', error: 'the dataset moved under it' }))

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByRole('alert')).toHaveTextContent('the dataset moved under it')
    expect(screen.getByRole('table')).toBeInTheDocument()
  })

  it('says something even when a failure recorded no reason', () => {
    showing(walkForward({ status: 'failed', error: null }))

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByRole('alert')).toHaveTextContent('no reason was recorded')
  })

  it('leads with the promise beside the delivery', () => {
    showing(walkForward())

    renderWithProviders(<WalkForwardResult />)

    // ⚠️ "Median in sample", not "In sample". The tiles are middle values across folds; the
    // table's columns are one fold's own return, and the two must not share a name.
    expect(screen.getByText('Median in sample')).toBeInTheDocument()
    expect(screen.getByText('Median out of sample')).toBeInTheDocument()
    expect(screen.getAllByText('In sample')).toHaveLength(1)
  })

  it('says it is loading rather than rendering an empty report', () => {
    showing(undefined, 'pending')

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByText(/Loading the walk-forward/)).toBeInTheDocument()
  })

  it('reports a failure to load instead of a blank screen', () => {
    showing(undefined, 'error')

    renderWithProviders(<WalkForwardResult />)

    expect(screen.getByText(/Could not load this walk-forward/)).toBeInTheDocument()
  })
})
