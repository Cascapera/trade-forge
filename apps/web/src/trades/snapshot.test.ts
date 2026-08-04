/**
 * The chart's arithmetic, tested by calling it.
 *
 * These are the parts with edge cases — a curve that stops early, a zone older than the window,
 * two labels landing on the same pixel — and none of them are visible from a rendered component
 * without reading coordinates out of SVG attributes. Keeping the geometry pure is what lets the
 * claims be stated directly.
 */

import type { Snapshot } from '../api/types'
import {
  VIEW,
  candles,
  curveRuns,
  makeScale,
  markers,
  priceBand,
  priceLabels,
  regions,
} from './snapshot'

const bar = (hour: number, low: string, high: string, open: string, close: string) => ({
  time: `2024-01-01T${String(hour).padStart(2, '0')}:00:00Z`,
  open,
  high,
  low,
  close,
})

function aSnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    decided_at: '2024-01-01T02:00:00Z',
    filled_at: '2024-01-01T03:00:00Z',
    bars: [
      bar(0, '100', '104', '101', '103'),
      bar(1, '102', '106', '103', '105'),
      bar(2, '104', '108', '105', '107'),
      bar(3, '106', '110', '107', '109'),
    ],
    regions: [],
    series: [],
    ...overrides,
  }
}

const scaleFor = (snapshot: Snapshot, levels: number[] = []) =>
  makeScale(snapshot.bars.length, priceBand(snapshot, levels))

describe('priceBand', () => {
  it('contains the zone even when price never reached it', () => {
    // The entries worth staring at are often the ones where price stopped short of the zone.
    // Sizing the band on the bars alone would push that rectangle off the canvas.
    const snapshot = aSnapshot({
      regions: [{ label: 'zone', top: '130', bottom: '125', from_time: '2024-01-01T00:00:00Z' }],
    })
    const band = priceBand(snapshot, [])
    expect(band.high).toBeGreaterThan(130)
  })

  it('contains the curve as well as the bars', () => {
    const snapshot = aSnapshot({
      series: [{ label: 'average', points: [['2024-01-01T00:00:00Z', '80']] }],
    })
    expect(priceBand(snapshot, []).low).toBeLessThan(80)
  })

  it('gives a flat window a range instead of dividing by zero', () => {
    const flat = aSnapshot({ bars: [bar(0, '100', '100', '100', '100')] })
    const band = priceBand(flat, [])
    expect(band.high).toBeGreaterThan(band.low)
    expect(Number.isFinite(makeScale(1, band).y(100))).toBe(true)
  })
})

describe('candles', () => {
  it('marks a bar that closed up and one that closed down', () => {
    const snapshot = aSnapshot({
      bars: [bar(0, '100', '110', '101', '109'), bar(1, '100', '110', '109', '101')],
    })
    const shapes = candles(snapshot, scaleFor(snapshot))
    expect(shapes.map((shape) => shape.up)).toEqual([true, false])
  })

  it('gives a doji a body a pixel tall so it does not vanish', () => {
    const snapshot = aSnapshot({ bars: [bar(0, '99', '101', '100', '100')] })
    const [shape] = candles(snapshot, scaleFor(snapshot))
    expect(shape!.bodyHeight).toBeGreaterThanOrEqual(1)
  })
})

describe('curveRuns', () => {
  it('joins points to bars by time, not by position', () => {
    // The curve starts on the third bar. Read positionally it would be drawn over bars 0-1.
    const snapshot = aSnapshot({
      series: [
        {
          label: 'average',
          points: [
            ['2024-01-01T02:00:00Z', '106'],
            ['2024-01-01T03:00:00Z', '108'],
          ],
        },
      ],
    })
    const scale = scaleFor(snapshot)
    const [run] = curveRuns(snapshot, scale)
    const xs = run!.points.split(' ').map((pair) => Number(pair.split(',')[0]))
    expect(xs).toEqual([scale.x(2), scale.x(3)])
  })

  it('breaks the line at a gap instead of bridging it', () => {
    // A bridged gap is a hole nobody can see, which is worse than a line that visibly stops.
    const snapshot = aSnapshot({
      series: [
        {
          label: 'average',
          points: [
            ['2024-01-01T00:00:00Z', '101'],
            ['2024-01-01T01:00:00Z', '103'],
            ['1999-01-01T00:00:00Z', '999'], // not a bar of this window
            ['2024-01-01T02:00:00Z', '105'],
            ['2024-01-01T03:00:00Z', '107'],
          ],
        },
      ],
    })
    expect(curveRuns(snapshot, scaleFor(snapshot))).toHaveLength(2)
  })

  it('drops a run of a single point rather than drawing a polyline of one', () => {
    const snapshot = aSnapshot({
      series: [{ label: 'average', points: [['2024-01-01T02:00:00Z', '106']] }],
    })
    expect(curveRuns(snapshot, scaleFor(snapshot))).toEqual([])
  })
})

describe('regions', () => {
  it('cuts a zone older than the window at the left edge and says so', () => {
    // Never *moved* to the first visible bar: that would draw the zone as younger than it is,
    // which is the one thing the rectangle exists to show.
    const snapshot = aSnapshot({
      regions: [{ label: 'zone', top: '106', bottom: '104', from_time: '2023-12-25T00:00:00Z' }],
    })
    const [zone] = regions(snapshot, scaleFor(snapshot))
    expect(zone!.clipped).toBe(true)
    expect(zone!.x).toBe(0)
  })

  it('starts a zone on its own bar when that bar is in the window', () => {
    const snapshot = aSnapshot({
      regions: [{ label: 'zone', top: '106', bottom: '104', from_time: '2024-01-01T01:00:00Z' }],
    })
    const scale = scaleFor(snapshot)
    const [zone] = regions(snapshot, scale)
    expect(zone!.clipped).toBe(false)
    expect(zone!.x).toBeCloseTo(scale.x(1) - scale.barWidth / 2)
  })

  it('extends the rectangle rightward to the edge of the plot', () => {
    // A zone stays live until price comes back into it, so it runs forward and the bars show
    // when it was reached.
    const snapshot = aSnapshot({
      regions: [{ label: 'zone', top: '106', bottom: '104', from_time: '2024-01-01T01:00:00Z' }],
    })
    const scale = scaleFor(snapshot)
    const [zone] = regions(snapshot, scale)
    expect(zone!.x + zone!.width).toBeCloseTo(scale.plotRight)
  })
})

describe('regions, degenerate inputs', () => {
  it('draws nothing when the window has no bars', () => {
    const empty = aSnapshot({ bars: [] })
    expect(regions(empty, makeScale(0, { low: 100, high: 110 }))).toEqual([])
  })

  it('falls back to the left edge for a zone no bar reaches', () => {
    // `from_time` after every bar has no bar to start on. The engine cannot produce this — a
    // region may not begin after the decision — but the arithmetic must not emit NaN if it did.
    const snapshot = aSnapshot({
      regions: [{ label: 'zone', top: '106', bottom: '104', from_time: '2099-01-01T00:00:00Z' }],
    })
    const [zone] = regions(snapshot, scaleFor(snapshot))
    expect(zone!.x).toBe(0)
    expect(Number.isFinite(zone!.width)).toBe(true)
  })
})

describe('priceLabels', () => {
  const scale = makeScale(4, { low: 100, high: 110 })
  const fmt = (value: number) => value.toFixed(2)

  it('pushes overlapping labels apart while leaving the lines where they are', () => {
    // Measured on a real two-year run: 17 of 26 entries had two labels closer together than the
    // type is tall, because the average and the stop both sit near the decision bar's low.
    const labels = priceLabels(
      { entry: 105, stop: 104.99, average: 104.98, hasCurve: false },
      scale,
      fmt,
    )
    const sorted = [...labels].sort((a, b) => a.labelY - b.labelY)
    for (let i = 1; i < sorted.length; i += 1) {
      expect(sorted[i]!.labelY - sorted[i - 1]!.labelY).toBeGreaterThanOrEqual(11)
    }
    // The lines did not move: each still sits at its own price.
    for (const label of labels) expect(label.y).toBeCloseTo(scale.y(label.price))
  })

  it.each([
    ['at the top of the band', { entry: 110, stop: 109.99, average: 109.98 }],
    ['at the bottom of the band', { entry: 100, stop: 100.01, average: 100.02 }],
  ])('keeps every label inside the plot when they crowd %s', (_name, prices) => {
    // Pushing apart moves labels down; three of them crowded at the bottom would run off the
    // axis, so the stack is shifted back up. Both ends are exercised, because a clamp tested
    // from one side is a clamp that can be missing on the other.
    const labels = priceLabels({ ...prices, hasCurve: false }, scale, fmt)
    for (const label of labels) {
      expect(label.labelY).toBeGreaterThanOrEqual(VIEW.padTop)
      expect(label.labelY).toBeLessThanOrEqual(VIEW.height - VIEW.padBottom)
    }
  })

  it('returns nothing to draw when there is nothing to label', () => {
    expect(priceLabels({ entry: NaN, stop: null, average: null, hasCurve: true }, scale, fmt)).toHaveLength(1)
  })

  it('omits the average label when a curve is drawn', () => {
    // With a curve, the curve *is* the average; a horizontal mark at one of its values would
    // read as a level the average never held.
    const withCurve = priceLabels({ entry: 105, stop: 104, average: 106, hasCurve: true }, scale, fmt)
    expect(withCurve.map((label) => label.kind)).not.toContain('average')

    const without = priceLabels({ entry: 105, stop: 104, average: 106, hasCurve: false }, scale, fmt)
    expect(without.map((label) => label.kind)).toContain('average')
  })

  it('omits the stop label for a trade that carried none', () => {
    const labels = priceLabels({ entry: 105, stop: null, average: null, hasCurve: false }, scale, fmt)
    expect(labels.map((label) => label.kind)).toEqual(['entry'])
  })
})

describe('markers', () => {
  it('finds the decision in the middle of the window, not at its end', () => {
    // Once the broker has extended the bars to the fill, the decision is no longer the last bar.
    const snapshot = aSnapshot()
    expect(markers(snapshot)).toEqual([
      { kind: 'decision', index: 2, x: 0 },
      { kind: 'fill', index: 3, x: 0 },
    ])
  })

  it('marks one bar once when the decision and the fill coincide', () => {
    // The long-rest case: the window kept the arming half alone, so `filled_at === decided_at`.
    const snapshot = aSnapshot({ filled_at: '2024-01-01T02:00:00Z' })
    expect(markers(snapshot).map((mark) => mark.kind)).toEqual(['decision'])
  })

  it('marks nothing rather than guessing when neither instant is in the window', () => {
    // Unreachable through the engine, which refuses a snapshot whose decision bar is absent.
    // Handled anyway, because the alternative is an index of -1 drawing a tick off-canvas.
    const snapshot = aSnapshot({
      decided_at: '2099-01-01T00:00:00Z',
      filled_at: '2099-01-01T01:00:00Z',
    })
    expect(markers(snapshot)).toEqual([])
  })
})
