import { describe, expect, it } from 'vitest'

import type { BacktestListItem, StudyOut, StudyPoint } from '../api/types'

import { GAIN, LOSS, NEUTRAL, PENDING, axisName, fillFor, layoutOf } from './study'

const CAPITAL = '10000'

function point(over: Partial<StudyPoint> & { values: Record<string, unknown> }): StudyPoint {
  return {
    backtest_id: over.backtest_id ?? crypto.randomUUID(),
    strategy_id: 'strategy',
    label: over.label ?? 'point',
    status: over.status ?? 'done',
    ...over,
  }
}

function run(id: string, netProfit: string | null): BacktestListItem {
  return {
    id,
    strategy_id: 'strategy',
    strategy_name: 'name',
    strategy_version: 1,
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: CAPITAL,
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

/** A 3x2 study whose six points return the profits given, in grid order. */
function study(profits: readonly (string | null)[]): StudyOut {
  const periods = [5, 9, 20]
  const targets = [1, 2]
  const points: StudyPoint[] = []
  const runs: BacktestListItem[] = []
  let at = 0
  for (const period of periods) {
    for (const target of targets) {
      const id = `run-${String(period)}-${String(target)}`
      points.push(
        point({
          backtest_id: id,
          label: `period=${String(period)}, rr=${String(target)}`,
          values: { 'setup.params.period': period, 'setup.params.rr': target },
        }),
      )
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
    initial_capital: CAPITAL,
    created_at: '2024-01-01T00:00:00Z',
    grid: { 'setup.params.period': periods, 'setup.params.rr': targets },
    points,
    aggregate: {
      points_total: 6,
      points_finished: 6,
      points_failed: 0,
      points_profitable: 0,
      best_label: null,
      best_return: null,
      worst_label: null,
      worst_return: null,
      median_return: null,
    },
    runs,
  }
}

describe('axisName', () => {
  it('shows the leaf of a path, because the prefix is the same on every axis', () => {
    expect(axisName('setup.params.period')).toBe('period')
    expect(axisName('indicators.0.params.period')).toBe('period')
  })
})

describe('fillFor', () => {
  it('paints a point that has not finished as neither a gain nor a flat zero', () => {
    // ⚠️ A queued run has no return. Painting it with the zero colour would put it on the scale
    // beside points that genuinely broke even — and a half-finished study would read as a
    // sea of mediocrity rather than as a study that is still running.
    expect(fillFor(null, 0.4)).toBe(PENDING)
    expect(fillFor(null, 0.4)).not.toBe(NEUTRAL)
  })

  it('paints exactly zero as the neutral, on either side of it', () => {
    expect(fillFor(0, 0.4)).toBe(NEUTRAL)
  })

  it('reaches the full pole only at the extreme', () => {
    expect(fillFor(0.4, 0.4)).toBe(GAIN)
    expect(fillFor(-0.4, 0.4)).toBe(LOSS)
  })

  it('normalises both arms against the same extent', () => {
    // ⚠️ The mutant this exists for: scaling each arm to its own maximum. In a study whose worst
    // point lost 2% and whose best made 40%, that would paint the 2% loss as vividly as the 40%
    // gain — a picture in which a trivial loss and a spectacular win are equally loud.
    const smallLoss = fillFor(-0.02, 0.4)
    const bigGain = fillFor(0.4, 0.4)

    expect(bigGain).toBe(GAIN)
    expect(smallLoss).not.toBe(LOSS)
    // Barely off the neutral, which is what a 5% deflection should look like.
    expect(smallLoss).toBe(fillFor(-0.02, 0.4))
    expect(distance(smallLoss, NEUTRAL)).toBeLessThan(distance(smallLoss, LOSS))
  })

  it('does not run off the end of the scale when a value exceeds the extent', () => {
    // Cannot happen from `layoutOf`, which derives the extent from the same returns — but a
    // caller passing its own extent must not produce a hex outside 00–ff, which renders as
    // nothing at all rather than as an error.
    expect(fillFor(1, 0.4)).toBe(GAIN)
    expect(fillFor(-1, 0.4)).toBe(LOSS)
  })

  it('falls back to the neutral when nothing has a return to scale against', () => {
    expect(fillFor(0.1, 0)).toBe(NEUTRAL)
  })
})

/** How far apart two hex colours are, summed over the channels. Enough to say "closer to". */
function distance(left: string, right: string): number {
  return [1, 3, 5]
    .map((at) =>
      Math.abs(
        Number.parseInt(left.slice(at, at + 2), 16) - Number.parseInt(right.slice(at, at + 2), 16),
      ),
    )
    .reduce((total, value) => total + value, 0)
}

describe('layoutOf', () => {
  it('places every point on the axes its own values name', () => {
    const layout = layoutOf(study(['100', '200', '300', '400', '500', '600']))

    expect(layout.rows.path).toBe('setup.params.period')
    expect(layout.columns?.path).toBe('setup.params.rr')
    expect(layout.cells.map((cell) => [cell.row, cell.column])).toEqual([
      [0, 0],
      [0, 1],
      [1, 0],
      [1, 1],
      [2, 0],
      [2, 1],
    ])
  })

  it('places by value, not by position in the list', () => {
    // ⚠️ The mutant: `row: index % rows.length`. It agrees with the truth on a list that arrives
    // sorted, which is exactly what the server sends — so the scenario has to shuffle. Reversed,
    // a positional layout puts the last point at the top-left and the picture is mirrored.
    const sorted = study(['100', '200', '300', '400', '500', '600'])
    const shuffled: StudyOut = {
      ...sorted,
      points: [...sorted.points].reverse(),
    }

    const layout = layoutOf(shuffled)

    expect(layout.cells.map((cell) => [cell.row, cell.column])).toEqual([
      [2, 1],
      [2, 0],
      [1, 1],
      [1, 0],
      [0, 1],
      [0, 0],
    ])
  })

  it('reads each return as a fraction of the capital its own run started with', () => {
    const layout = layoutOf(study(['1000', '-500', '0', null, '2000', '100']))

    expect(layout.cells.map((cell) => cell.ret)).toEqual([0.1, -0.05, 0, null, 0.2, 0.01])
  })

  it('takes the extent from the largest absolute return, not the largest gain', () => {
    // ⚠️ A study whose worst point is worse than its best is good is the case that separates
    // `Math.max(...gains)` from `Math.max(...|returns|)`. With the wrong one, every loss beyond
    // the best gain saturates and a catastrophic corner looks exactly like a mild one.
    const layout = layoutOf(study(['1000', '-8000', '0', '0', '0', '0']))

    expect(layout.extent).toBe(0.8)
    expect(layout.cells[1]?.fill).toBe(LOSS)
    expect(layout.cells[0]?.fill).not.toBe(GAIN)
  })

  it('leaves a study with one axis a single column of rows', () => {
    const one = study(['100', '200', '300', '400', '500', '600'])
    const layout = layoutOf({
      ...one,
      grid: { 'setup.params.period': [5, 9, 20] },
      points: one.points.slice(0, 3).map((each, at) => ({
        ...each,
        values: { 'setup.params.period': [5, 9, 20][at] },
      })),
    })

    expect(layout.columns).toBeNull()
    expect(layout.cells.map((cell) => cell.column)).toEqual([0, 0, 0])
  })

  it('names the axes it cannot draw instead of flattening them onto the two it can', () => {
    // A grid of three parameters is a cube. Flattening one would stack cells on top of each
    // other, and the picture would look like a search of a space half its real size.
    const three = study(['100', '200', '300', '400', '500', '600'])
    const layout = layoutOf({
      ...three,
      grid: { ...three.grid, 'setup.params.side': ['long', 'short'] },
    })

    expect(layout.undrawn).toEqual(['setup.params.side'])
    expect(layout.columns?.path).toBe('setup.params.rr')
  })

  it('refuses to place a value its axis does not list, rather than putting it first', () => {
    // ⚠️ The mutant this exists for is `at === -1 ? 0 : at`, which is what "be forgiving" looks
    // like when written down. A point whose value is not on the axis is not the *first* point —
    // stacking it there hides a real cell underneath a stray one, and the heatmap silently
    // reports one combination in the place of another. -1 is what lets the screen drop it and
    // say how many it dropped.
    //
    // Unreachable through this API today, since the server derives every point's values from
    // the same grid it stores. It stays pinned because "unreachable" is a property of today's
    // callers, and the failure if it ever is reached is a picture that is wrong without looking
    // wrong.
    const base = study(['100', '200', '300', '400', '500', '600'])
    const stray: StudyOut = {
      ...base,
      points: [
        {
          ...base.points[0]!,
          values: { 'setup.params.period': 999, 'setup.params.rr': 1 },
        },
      ],
    }

    const layout = layoutOf(stray)

    expect(layout.cells[0]?.row).toBe(-1)
    expect(layout.cells[0]?.column).toBe(0)
  })

  it('is empty rather than broken for a study with no axes at all', () => {
    const none = study([])
    const layout = layoutOf({ ...none, grid: {} })

    expect(layout.cells).toEqual([])
    expect(layout.extent).toBe(0)
  })
})
