import { screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { BacktestListItem, StudyOut, StudyPoint } from '../api/types'
import { GAIN, LOSS } from '../backtest/study'
import { renderWithProviders } from '../test-utils'

import { StudyHeatmap } from './StudyHeatmap'

function run(id: string, netProfit: string | null): BacktestListItem {
  return {
    id,
    strategy_id: 'strategy',
    strategy_name: id,
    strategy_version: 1,
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: '10000',
    cost_model: { type: 'none' },
    status: netProfit === null ? 'queued' : 'done',
    error: null,
    created_at: '2024-01-01T00:00:00Z',
    finished_at: null,
    metrics:
      netProfit === null
        ? null
        : ({ net_profit: netProfit } as unknown as BacktestListItem['metrics']),
  }
}

/** A 2x2 study over `period` and `rr`, with the profits given in grid order. */
function study(profits: readonly (string | null)[], grid?: Record<string, unknown[]>): StudyOut {
  const points: StudyPoint[] = []
  const runs: BacktestListItem[] = []
  let at = 0
  for (const period of [5, 9]) {
    for (const rr of [2, 3]) {
      const id = `p${String(period)}r${String(rr)}`
      points.push({
        backtest_id: id,
        strategy_id: id,
        label: `period=${String(period)}, rr=${String(rr)}`,
        values: { 'setup.params.period': period, 'setup.params.rr': rr },
        status: 'done',
      })
      runs.push(run(id, profits[at] ?? null))
      at += 1
    }
  }
  return {
    id: 'study',
    strategy_id: 'strategy',
    strategy_name: 'MME9',
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: '10000',
    created_at: '2024-01-01T00:00:00Z',
    grid: grid ?? { 'setup.params.period': [5, 9], 'setup.params.rr': [2, 3] },
    points,
    aggregate: {
      points_total: 4,
      points_finished: 4,
      points_failed: 0,
      points_profitable: 2,
      best_label: null,
      best_return: null,
      worst_label: null,
      worst_return: null,
      median_return: null,
    },
    runs,
  }
}

describe('StudyHeatmap', () => {
  it('carries the number on every cell, so colour is never the only encoding', () => {
    // ⚠️ The accessibility decision, and it is why this is a table rather than an SVG. A cell's
    // colour is its value, and a reader with the commonest colour blindness — or a printed page,
    // or a forced-colours mode — has nothing else to go on unless the number is there too.
    renderWithProviders(<StudyHeatmap study={study(['1000', '-500', '0', '2000'])} />)

    // Scoped to the table: the legend carries the same numbers at its ends, because marking the
    // extremes is what a legend is. Two elements sharing a number is not an ambiguity here —
    // they are a value and its scale, and only one of them is the picture.
    const cells = within(screen.getByRole('table'))
    expect(cells.getByText('10.0%')).toBeInTheDocument()
    expect(cells.getByText('-5.0%')).toBeInTheDocument()
    expect(cells.getByText('20.0%')).toBeInTheDocument()
  })

  it('names both axes and their values as table headers', () => {
    renderWithProviders(<StudyHeatmap study={study(['1000', '-500', '0', '2000'])} />)

    // The leaf of each path, because `setup.params.` is identical on every axis.
    expect(screen.getByText('period \\ rr')).toBeInTheDocument()
    expect(screen.getByRole('rowheader', { name: '5' })).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: '3' })).toBeInTheDocument()
  })

  it('paints the extremes at the poles of the scale', () => {
    renderWithProviders(<StudyHeatmap study={study(['2000', '-2000', '0', '0'])} />)

    const cells = within(screen.getByRole('table'))
    const best = cells.getByText('20.0%').closest('td')
    const worst = cells.getByText('-20.0%').closest('td')

    expect(best).toHaveStyle({ backgroundColor: GAIN })
    expect(worst).toHaveStyle({ backgroundColor: LOSS })
  })

  it('shows a dash for a point that has not finished, not a zero', () => {
    renderWithProviders(<StudyHeatmap study={study(['1000', null, '0', '2000'])} />)

    const table = screen.getByRole('table')
    expect(within(table).getByText('—')).toBeInTheDocument()
  })

  it('says which axes it is not drawing rather than flattening them', () => {
    // A grid of three parameters is a cube. Folding one onto the rectangle would stack cells,
    // and the picture would look like a search of a space half its real size.
    renderWithProviders(
      <StudyHeatmap
        study={study(['1000', '-500', '0', '2000'], {
          'setup.params.period': [5, 9],
          'setup.params.rr': [2, 3],
          'setup.params.side': ['long', 'short'],
        })}
      />,
    )

    expect(screen.getByText(/Also varying side/)).toBeInTheDocument()
  })

  it('tells the reader what the picture is for', () => {
    renderWithProviders(<StudyHeatmap study={study(['1000', '-500', '0', '2000'])} />)

    expect(screen.getByText(/Read the shape, not the best cell/)).toBeInTheDocument()
  })
})
