import type { BacktestListItem, EquityPoint } from '../api/types'
import {
  EMPTY_SEATS,
  MAX_COMPARED,
  SERIES_COLORS,
  buildSeries,
  colorOf,
  compareLabel,
  costLabel,
  costlessAmong,
  isCostless,
  isFull,
  percentCurve,
  runLabel,
  seatOf,
  selectedIds,
  toggleSeat,
} from './compare'

function listed(over: Partial<BacktestListItem>): BacktestListItem {
  return {
    id: 'b1',
    strategy_id: 's1',
    strategy_name: 'Ponto Contínuo',
    strategy_version: 1,
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-08-01T00:00:00Z',
    date_to: '2026-07-31T00:00:00Z',
    initial_capital: '10000',
    cost_model: { type: 'none' },
    status: 'done',
    error: null,
    created_at: '2026-08-06T12:00:00Z',
    finished_at: '2026-08-06T12:00:30Z',
    metrics: null,
    ...over,
  }
}

function point(time: string, equity: string): EquityPoint {
  return { time, equity }
}

describe('seating', () => {
  it('fills the palette from the top down', () => {
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')

    expect(seatOf(seats, 'a')).toBe(0)
    expect(seatOf(seats, 'b')).toBe(1)
    expect(colorOf(seats, 'a')).toBe(SERIES_COLORS[0])
    expect(colorOf(seats, 'b')).toBe(SERIES_COLORS[1])
  })

  it('does not repaint the survivors when one run is removed', () => {
    // The reason `Seats` exists at all. Colour follows the run, not its rank: a reader who drops
    // one line to see the rest better must find the rest unchanged. Assigning colour by position
    // in a list of selected ids passes every other test here and fails this one — 'c' would slide
    // from slot 2 to slot 1 and turn orange the moment 'b' was unticked.
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')
    seats = toggleSeat(seats, 'c')
    const before = colorOf(seats, 'c')

    seats = toggleSeat(seats, 'b')

    expect(colorOf(seats, 'a')).toBe(SERIES_COLORS[0])
    expect(colorOf(seats, 'c')).toBe(before)
    expect(colorOf(seats, 'c')).toBe(SERIES_COLORS[2])
    expect(colorOf(seats, 'b')).toBeNull()
  })

  it('gives a freed seat to the next run ticked', () => {
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')
    seats = toggleSeat(seats, 'a')
    seats = toggleSeat(seats, 'c')

    // 'b' keeps slot 1; 'c' takes the slot 'a' released, not slot 2.
    expect(seatOf(seats, 'b')).toBe(1)
    expect(seatOf(seats, 'c')).toBe(0)
  })

  it('refuses the run past the limit instead of evicting one', () => {
    let seats = EMPTY_SEATS
    for (let i = 0; i < MAX_COMPARED; i += 1) seats = toggleSeat(seats, `run-${String(i)}`)
    expect(isFull(seats)).toBe(true)

    const after = toggleSeat(seats, 'one-too-many')

    expect(selectedIds(after)).toEqual(selectedIds(seats))
    expect(seatOf(after, 'one-too-many')).toBeNull()
  })

  it('reads out in seat order, not in the order they were ticked', () => {
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')
    seats = toggleSeat(seats, 'a') // frees slot 0
    seats = toggleSeat(seats, 'c') // takes slot 0

    expect(selectedIds(seats)).toEqual(['c', 'b'])
  })

  it('has one colour per seat and no more', () => {
    // A seventh seat would draw `undefined` as a colour. The two constants have to agree.
    expect(SERIES_COLORS).toHaveLength(MAX_COMPARED)
    expect(EMPTY_SEATS).toHaveLength(MAX_COMPARED)
  })
})

describe('percentCurve', () => {
  // Compared with `toBeCloseTo` throughout, never `toBe`. This is the one place in the app where
  // an exact decimal from the backend becomes a float: 11000/10000 − 1 is 0.10000000000000009, so
  // a percent is 10.000000000000009. That is fine for a chart and a table — the value that decides
  // anything already decided it in Postgres — but an exact-equality assertion on it would be a
  // test of IEEE 754 rounding rather than of this function.
  it('measures return on the capital the run declared', () => {
    const curve = percentCurve(
      [point('2024-08-01T13:00:00Z', '10000'), point('2024-08-01T14:00:00Z', '11500')],
      '10000',
    )

    expect(curve.map((p) => p.time)).toEqual([
      Date.parse('2024-08-01T13:00:00Z') / 1000,
      Date.parse('2024-08-01T14:00:00Z') / 1000,
    ])
    expect(curve[0]?.value).toBeCloseTo(0, 10)
    expect(curve[1]?.value).toBeCloseTo(15, 10)
  })

  it('puts two runs of different size on the same axis', () => {
    // The whole point of normalising. A $1 500 gain on $10 000 and a $15 000 gain on $100 000 are
    // the same result, and money would draw them an order of magnitude apart.
    const small = percentCurve([point('2024-08-01T13:00:00Z', '11500')], '10000')
    const large = percentCurve([point('2024-08-01T13:00:00Z', '115000')], '100000')

    expect(small[0]?.value).toBeCloseTo(large[0]?.value ?? NaN, 10)
  })

  it('does not anchor on the curve’s own first point', () => {
    // A run whose first recorded bar already sits above its starting capital. Anchoring on the
    // first point would draw this as starting at zero and hide the gain that came before it.
    const curve = percentCurve(
      [point('2024-08-01T13:00:00Z', '11000'), point('2024-08-01T14:00:00Z', '12000')],
      '10000',
    )

    expect(curve[0]?.value).toBeCloseTo(10, 10)
    expect(curve[1]?.value).toBeCloseTo(20, 10)
  })

  it('carries a loss below zero', () => {
    const curve = percentCurve([point('2024-08-01T13:00:00Z', '8750')], '10000')
    expect(curve[0]?.value).toBeCloseTo(-12.5, 10)
  })

  it('draws nothing rather than dividing by zero', () => {
    expect(percentCurve([point('2024-08-01T13:00:00Z', '100')], '0')).toEqual([])
  })
})

describe('buildSeries', () => {
  const a = listed({ id: 'a', symbol: 'AAPL', initial_capital: '10000' })
  const b = listed({ id: 'b', symbol: 'EURUSD', timeframe: 'M15', initial_capital: '50000' })

  it('draws each run in its own seat’s colour', () => {
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')
    const curves = new Map([
      ['a', [point('2024-08-01T13:00:00Z', '11000')]],
      ['b', [point('2025-01-01T13:00:00Z', '55000')]],
    ])

    const series = buildSeries(seats, [a, b], curves)

    expect(series.map((s) => s.color)).toEqual([SERIES_COLORS[0], SERIES_COLORS[1]])
    expect(series[0]?.points[0]?.value).toBeCloseTo(10, 10)
    expect(series[1]?.points[0]?.value).toBeCloseTo(10, 10)
  })

  it('draws the curves that arrived without waiting for the slowest', () => {
    // One request per run, landing at different moments. A run still loading is absent, and the
    // one already loaded keeps the colour its seat gave it — a line appearing late must not
    // recolour the lines already on screen.
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')

    const series = buildSeries(seats, [a, b], new Map([['b', [point('2025-01-01T13:00:00Z', '55000')]]]))

    expect(series).toHaveLength(1)
    expect(series[0]?.id).toBe('b')
    expect(series[0]?.color).toBe(SERIES_COLORS[1])
  })

  it('leaves each run on the timestamps it actually has', () => {
    // No common grid, no forward fill. Two windows that barely overlap stay two windows: writing
    // points for 'b' across 'a''s earlier months would draw a flat stretch that never happened.
    let seats = toggleSeat(EMPTY_SEATS, 'a')
    seats = toggleSeat(seats, 'b')
    const curves = new Map([
      ['a', [point('2024-08-01T13:00:00Z', '10000'), point('2024-09-01T13:00:00Z', '10500')]],
      ['b', [point('2025-01-01T13:00:00Z', '50000')]],
    ])

    const series = buildSeries(seats, [a, b], curves)

    expect(series[0]?.points).toHaveLength(2)
    expect(series[1]?.points).toHaveLength(1)
    expect(series[1]?.points[0]?.time).toBe(Date.parse('2025-01-01T13:00:00Z') / 1000)
  })

  it('skips a selected run that is not on this page', () => {
    const seats = toggleSeat(EMPTY_SEATS, 'gone')
    expect(buildSeries(seats, [a, b], new Map([['gone', [point('2024-08-01T13:00:00Z', '1')]]]))).toEqual([])
  })

  it('names a run by instrument and strategy version', () => {
    expect(runLabel(b)).toBe('EURUSD M15 · Ponto Contínuo v1')
  })
})

describe('compareLabel', () => {
  it('tells two runs of the same configuration apart', () => {
    // The case a research log is full of, and the one `runLabel` alone cannot serve: the same
    // strategy on the same instrument, relaunched. Six controls announcing the same words leave a
    // screen-reader user with no idea which row they are on.
    const morning = listed({ id: 'am', created_at: '2026-08-06T12:00:00Z' })
    const evening = listed({ id: 'pm', created_at: '2026-08-06T18:00:00Z' })

    expect(runLabel(morning)).toBe(runLabel(evening))
    expect(compareLabel(morning)).not.toBe(compareLabel(evening))
  })

  it('tells the same strategy over two windows apart', () => {
    const short = listed({ date_from: '2025-01-01T00:00:00Z' })
    const long = listed({ date_from: '2024-08-01T00:00:00Z' })
    expect(compareLabel(short)).not.toBe(compareLabel(long))
  })

  it('still opens with what a human reads', () => {
    expect(compareLabel(listed({}))).toContain('compare AAPL H1 · Ponto Contínuo v1')
  })
})

describe('costs', () => {
  it('reads the three models the API builds', () => {
    expect(costLabel({ type: 'none' })).toBe('none')
    expect(costLabel({ type: 'spread', spread_points: '12' })).toBe('spread 12 pts')
    expect(costLabel({ type: 'commission', commission_per_unit: '0.02' })).toBe(
      'commission 0.02/unit',
    )
  })

  it('treats a shape it does not know as costed', () => {
    // Claiming "no costs" about a document this code does not recognise would be an assertion
    // with nothing behind it. The safe direction is to say nothing and show the raw type.
    expect(isCostless({ type: 'swap' })).toBe(false)
    expect(isCostless({})).toBe(false)
    expect(costLabel({ type: 'swap' })).toBe('swap')
    expect(costLabel({})).toBe('unknown')
  })

  it('finds the costless runs among the selected ones only', () => {
    const free = listed({ id: 'free', cost_model: { type: 'none' } })
    const costed = listed({ id: 'costed', cost_model: { type: 'spread', spread_points: '12' } })
    const unpicked = listed({ id: 'unpicked', cost_model: { type: 'none' } })

    let seats = toggleSeat(EMPTY_SEATS, 'free')
    seats = toggleSeat(seats, 'costed')

    expect(costlessAmong(seats, [free, costed, unpicked]).map((r) => r.id)).toEqual(['free'])
  })
})
