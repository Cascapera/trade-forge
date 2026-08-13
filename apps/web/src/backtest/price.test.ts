import type { Candle, OverlaySeries, Trade } from '../api/types'
import { CURVE_COLORS, toBars, toCurves, toMarkers, toSeconds, visibleRangeFor } from './price'

function candle(over: Partial<Candle>): Candle {
  return {
    time: '2024-08-01T13:00:00Z',
    open: '226.2700000000',
    high: '226.9800000000',
    low: '225.7600000000',
    close: '226.2000000000',
    ...over,
  }
}

function trade(over: Partial<Trade>): Trade {
  return {
    id: 1,
    direction: 'long',
    entry_time: '2024-08-01T14:00:00Z',
    entry_price: '226.50',
    exit_time: '2024-08-01T17:00:00Z',
    exit_price: '228.00',
    exit_reason: 'take_profit',
    volume: '10',
    stop_loss: '225.00',
    take_profit: '229.50',
    gross_pnl: '15.00',
    costs: '0',
    net_pnl: '15.00',
    r_multiple: '2.00',
    context: {},
    has_snapshot: true,
    ...over,
  }
}

/** Hourly bars from 13:00, `count` of them. */
function hourly(count: number, day = '2024-08-01'): Candle[] {
  return Array.from({ length: count }, (_, i) =>
    candle({ time: `${day}T${String(13 + i).padStart(2, '0')}:00:00Z` }),
  )
}

describe('toSeconds', () => {
  it('renders an instant in seconds, not milliseconds', () => {
    // The axis is indexed in seconds. Milliseconds would put every bar a thousand times further
    // out than the scale expects, and the chart renders empty rather than complaining.
    expect(toSeconds('2024-08-01T13:00:00Z')).toBe(1722517200)
  })
})

describe('toBars', () => {
  it('turns the wire strings into the numbers the canvas needs', () => {
    const [bar] = toBars([candle({})])

    expect(bar).toEqual({
      time: 1722517200,
      open: 226.27,
      high: 226.98,
      low: 225.76,
      close: 226.2,
    })
  })

  it('keeps the bars in the order they arrived', () => {
    const times = toBars(hourly(3)).map((b) => b.time)

    expect(times).toEqual([...times].sort((a, b) => a - b))
  })
})

describe('toMarkers', () => {
  it('points the entry arrow the way the trade expects price to go', () => {
    const [long] = toMarkers([trade({ direction: 'long' })], null)
    const [short] = toMarkers([trade({ direction: 'short' })], null)

    expect(long).toMatchObject({ shape: 'arrowUp', position: 'belowBar' })
    expect(short).toMatchObject({ shape: 'arrowDown', position: 'aboveBar' })
  })

  it('colours the exit by outcome, not by direction', () => {
    // The test that separates the two rules. A *short* that made money must wear the up colour;
    // an implementation that coloured by `direction` would agree with every long-only fixture
    // and be wrong on exactly this trade.
    const [, winningShort] = toMarkers(
      [trade({ direction: 'short', net_pnl: '15.00' })],
      null,
    )
    const [, losingLong] = toMarkers([trade({ direction: 'long', net_pnl: '-15.00' })], null)

    expect(winningShort?.color).not.toBe(losingLong?.color)
    const [, winningLong] = toMarkers([trade({ direction: 'long', net_pnl: '15.00' })], null)
    expect(winningShort?.color).toBe(winningLong?.color)
  })

  it('reads the outcome after costs, never before them', () => {
    // A trade whose gross is positive and whose net is negative is a trade that lost. Charging
    // the spread is the whole point of PR-226; a chart that read `gross_pnl` would paint it as a
    // winner and disagree with the metrics beside it.
    //
    // ⚠️ Both comparisons are needed, and the reference trades must disagree under the *mutant*
    // as well as under the truth. An earlier version compared against a loser whose `gross_pnl`
    // was left at the fixture's positive default: under "read the gross" that reference became a
    // winner too, both sides came out the same colour, and the test passed against exactly the
    // implementation it was written to forbid.
    const [, eatenByCosts] = toMarkers(
      [trade({ gross_pnl: '5.00', costs: '9.00', net_pnl: '-4.00' })],
      null,
    )
    const [, loser] = toMarkers([trade({ gross_pnl: '-1.00', net_pnl: '-1.00' })], null)
    const [, winner] = toMarkers([trade({ gross_pnl: '5.00', net_pnl: '5.00' })], null)

    expect(eatenByCosts?.color).toBe(loser?.color)
    expect(eatenByCosts?.color).not.toBe(winner?.color)
  })

  it('separates a winning exit from a losing one by shape as well as colour', () => {
    // Not belt and braces. Measured against this chart's surface, the up and down hues are
    // ΔE 7.5 apart under deuteranopia — inside the band where colour is only legal alongside a
    // second encoding. Without the shape, a red-green reader sees one mark for both outcomes on
    // the chart whose whole subject is telling them apart.
    const [, winner] = toMarkers([trade({ net_pnl: '5.00' })], null)
    const [, loser] = toMarkers([trade({ net_pnl: '-5.00' })], null)

    expect(winner?.shape).not.toBe(loser?.shape)
  })

  it('gives a trade that never closed no exit mark', () => {
    // A position still open when the run ended has no closing bar. Drawing one anyway — at the
    // last candle, say — would show an exit the strategy never took.
    const marks = toMarkers([trade({ exit_time: null, exit_price: null, net_pnl: null })], null)

    expect(marks).toHaveLength(1)
    expect(marks[0]?.shape).toBe('arrowUp')
  })

  it('sorts every mark ascending, across trades', () => {
    // lightweight-charts requires ascending markers and does not sort them. Trades arrive
    // newest-first, and one trade's exit routinely shares a bar with the next one's entry, so
    // neither the input order nor a naive interleave is ascending.
    const marks = toMarkers(
      [
        trade({ id: 2, entry_time: '2024-08-02T13:00:00Z', exit_time: '2024-08-02T16:00:00Z' }),
        trade({ id: 1, entry_time: '2024-08-01T13:00:00Z', exit_time: '2024-08-01T16:00:00Z' }),
      ],
      null,
    )

    expect(marks.map((m) => m.time)).toEqual([...marks].map((m) => m.time).sort((a, b) => a - b))
  })

  it('labels only the selected trade', () => {
    const both = [trade({ id: 1 }), trade({ id: 2 })]

    const none = toMarkers(both, null)
    const one = toMarkers(both, 2)

    expect(none.every((m) => m.text === '')).toBe(true)
    expect(one.filter((m) => m.text !== '').map((m) => m.tradeId)).toEqual([2, 2])
  })

  it('writes the R multiple on the chosen trade, signed', () => {
    const [, exit] = toMarkers([trade({ id: 7, r_multiple: '-1.00' })], 7)

    expect(exit?.text).toBe('−1.00R')
  })
})

describe('visibleRangeFor', () => {
  it('pads in bars, not in time, so a market hole cannot empty the window', () => {
    // ⚠️ The test this function exists for. Friday's last bar and Monday's first are three days
    // apart on the clock and adjacent in the array. Padding by "six hours" around a Friday
    // evening entry asks for a range no bar lives in, and lightweight-charts honours it: an
    // empty chart, which reads as missing data rather than as bad arithmetic.
    // ⚠️ The trade sits on **Monday's first bar**, so the padding itself has to step backwards
    // across the weekend. An earlier version of this test put the trade on Friday and let the
    // hole fall *between* the two padded edges — where counting bars and counting hours give
    // the same answer, and the scenario could not tell them apart at all.
    const bars = toBars([
      ...hourly(3, '2024-08-02'), // Friday 13:00, 14:00, 15:00
      ...hourly(3, '2024-08-05'), // Monday 13:00, 14:00, 15:00
    ])
    const monday = trade({
      entry_time: '2024-08-05T13:00:00Z',
      exit_time: '2024-08-05T14:00:00Z',
    })

    const range = visibleRangeFor(monday, bars, 2)

    // Two bars before Monday 13:00 is **Friday 14:00**. Two hours before it is Monday 11:00,
    // where no bar exists and the chart would come up empty.
    expect(range).toEqual({
      from: toSeconds('2024-08-02T14:00:00Z'),
      to: toSeconds('2024-08-05T15:00:00Z'),
    })
  })

  it('clamps at the ends of the series instead of running past them', () => {
    const bars = toBars(hourly(4))
    const first = trade({
      entry_time: '2024-08-01T13:00:00Z',
      exit_time: '2024-08-01T16:00:00Z',
    })

    expect(visibleRangeFor(first, bars, 50)).toEqual({
      from: bars[0]?.time,
      to: bars[bars.length - 1]?.time,
    })
  })

  it('centres an open trade on its entry', () => {
    const bars = toBars(hourly(6))
    const open = trade({ entry_time: '2024-08-01T15:00:00Z', exit_time: null })

    expect(visibleRangeFor(open, bars, 1)).toEqual({
      from: toSeconds('2024-08-01T14:00:00Z'),
      to: toSeconds('2024-08-01T16:00:00Z'),
    })
  })

  it('refuses to move the chart when the trade is not in these bars', () => {
    const bars = toBars(hourly(3, '2024-08-05'))
    const elsewhere = trade({
      entry_time: '2024-01-01T13:00:00Z',
      exit_time: '2024-01-01T14:00:00Z',
    })

    expect(visibleRangeFor(elsewhere, bars, 2)).toBeNull()
  })

  it('finds the bar a fill sits on even when the stamps do not match exactly', () => {
    // At-or-before, not equality: a run's candles and a trade's timestamps travel separately,
    // and an exact match that missed by a second would silently refuse to move the chart.
    const bars = toBars(hourly(4))
    const offset = trade({
      entry_time: '2024-08-01T14:00:30Z',
      exit_time: '2024-08-01T15:00:30Z',
    })

    expect(visibleRangeFor(offset, bars, 1)).toEqual({
      from: toSeconds('2024-08-01T13:00:00Z'),
      to: toSeconds('2024-08-01T16:00:00Z'),
    })
  })
})

describe('toCurves', () => {
  function series(over: Partial<OverlaySeries> = {}): OverlaySeries {
    return {
      label: 'EMA 9',
      points: [
        ['2024-08-01T14:00:00Z', '226.2700000000'],
        ['2024-08-01T15:00:00Z', '226.9800000000'],
      ],
      ...over,
    }
  }

  it('turns the wire pairs into seconds and numbers', () => {
    const [curve] = toCurves([series()])

    expect(curve?.label).toBe('EMA 9')
    expect(curve?.points).toEqual([
      { time: toSeconds('2024-08-01T14:00:00Z'), value: 226.27 },
      { time: toSeconds('2024-08-01T15:00:00Z'), value: 226.98 },
    ])
  })

  it('gives each curve its own colour, in the order the palette was measured in', () => {
    const curves = toCurves([series({ label: 'fast' }), series({ label: 'slow' })])

    expect(curves.map((c) => c.color)).toEqual([CURVE_COLORS[0], CURVE_COLORS[1]])
  })

  it('drops curves past the palette instead of recycling a colour', () => {
    // Recycling would put two different indicators in one swatch, and a legend naming both
    // against the same colour tells the reader nothing. Losing a line is visible; two lines
    // claiming to be the same one is not.
    const many = Array.from({ length: CURVE_COLORS.length + 2 }, (_, i) =>
      series({ label: `ind ${String(i)}` }),
    )

    const curves = toCurves(many)

    expect(curves).toHaveLength(CURVE_COLORS.length)
    expect(new Set(curves.map((c) => c.color)).size).toBe(CURVE_COLORS.length)
  })

  it('is empty for a strategy that reads no curves at all', () => {
    // The structure setups. Empty is an ordinary answer, not a failure.
    expect(toCurves([])).toEqual([])
  })

  it('keeps a curve that starts after the first bar starting where it does', () => {
    // The warm-up gap. Nothing here pads the head of the series to the length of the candles —
    // the timestamps are carried per point precisely so the chart joins on time, not on index.
    const late = series({ points: [['2024-08-01T18:00:00Z', '227.00']] })

    const [curve] = toCurves([late])

    expect(curve?.points).toHaveLength(1)
    expect(curve?.points[0]?.time).toBe(toSeconds('2024-08-01T18:00:00Z'))
  })
})
