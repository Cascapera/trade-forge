/**
 * The geometry of an entry chart, with no React and no DOM in sight.
 *
 * Everything here is arithmetic on the recorded window: where each candle sits, where the curves
 * run, where the zone rectangle is cut off, and where the price labels go once they are stopped
 * from overlapping. Keeping it separate from the component is what makes the interesting parts
 * — the ones with off-by-ones and edge cases — testable by calling a function.
 *
 * Three rules are carried from the engine and are not the drawing's to reinterpret:
 *
 * 1. **Prices are exact decimals as strings.** They are parsed to numbers *here*, at the last
 *    moment, because pixels are floats anyway. Nothing compares them as numbers.
 * 2. **A curve joins the bars on time, never on position.** A point whose bar is not in the
 *    window is dropped and the line breaks there. Bridging the gap is how a hole goes unseen.
 * 3. **A region's left edge may precede the window** and is clamped to the chart's edge — never
 *    moved to the first visible bar, which would draw the zone as younger than it is.
 */

import type { Snapshot, SnapshotBar } from '../api/types'

export const VIEW = { width: 760, height: 260, padLeft: 8, padRight: 66, padTop: 12, padBottom: 26 }

/** The vertical gap price labels are pushed apart to. Slightly more than the type's own height. */
const LABEL_GAP = 11.5

export interface Scale {
  /** Horizontal centre of the bar at `index`. */
  x: (index: number) => number
  /** Vertical position of a price. */
  y: (price: number) => number
  /** Body width of one candle. */
  barWidth: number
  plotHeight: number
  plotRight: number
}

export interface CandleShape {
  index: number
  time: string
  up: boolean
  x: number
  wickTop: number
  wickBottom: number
  bodyTop: number
  bodyHeight: number
  bar: SnapshotBar
}

export interface CurveRun {
  label: string
  /** `x,y` pairs ready for a `<polyline points=…>`. One run per unbroken stretch. */
  points: string
}

export interface RegionShape {
  label: string
  x: number
  y: number
  width: number
  height: number
  /** True when the zone begins before the window and the rectangle is cut at the left edge. */
  clipped: boolean
}

export interface PriceLabel {
  kind: 'entry' | 'stop' | 'average'
  price: number
  /** Where the line is drawn — the true price. */
  y: number
  /** Where the text is drawn, after labels were pushed apart. Equal to `y` when nothing moved. */
  labelY: number
  text: string
}

export interface LevelSegment {
  label: string
  price: number
  y: number
  /** Left end. Clamped to the chart edge when the level was set before the window. */
  x1: number
  /** Right end: the bar that broke it. Clamped to the right edge if that is past the window. */
  x2: number
  /** True when either end was clamped — the segment is longer than it can be drawn. */
  clamped: boolean
}

export interface Marker {
  kind: 'decision' | 'fill'
  index: number
  x: number
}

export function toNumber(value: string): number {
  return Number(value)
}

/**
 * The price band the chart covers: every bar, every level, and every curve point, plus padding.
 *
 * The curves and the zone are included deliberately. A zone that sat outside the bars' own range
 * would otherwise be drawn off-canvas — and it is exactly the entries where price never reached
 * the zone that a reader wants to look at.
 */
export function priceBand(snapshot: Snapshot, levels: number[]): { low: number; high: number } {
  const values = [...levels]
  for (const bar of snapshot.bars) {
    values.push(toNumber(bar.low), toNumber(bar.high))
  }
  for (const region of snapshot.regions) {
    values.push(toNumber(region.bottom), toNumber(region.top))
  }
  for (const level of snapshot.levels) {
    values.push(toNumber(level.price))
  }
  for (const series of snapshot.series) {
    for (const [, value] of series.points) values.push(toNumber(value))
  }
  const low = Math.min(...values)
  const high = Math.max(...values)
  // A flat window has no range to divide by; give it an arbitrary one rather than dividing by
  // zero and painting every bar on the same line.
  const pad = (high - low) * 0.07 || 1
  return { low: low - pad, high: high + pad }
}

export function makeScale(barCount: number, band: { low: number; high: number }): Scale {
  const plotWidth = VIEW.width - VIEW.padLeft - VIEW.padRight
  const plotHeight = VIEW.height - VIEW.padTop - VIEW.padBottom
  const slot = plotWidth / Math.max(1, barCount)
  return {
    x: (index) => VIEW.padLeft + (index + 0.5) * slot,
    y: (price) => VIEW.padTop + ((band.high - price) / (band.high - band.low)) * plotHeight,
    barWidth: Math.max(1.5, Math.min(9, slot * 0.62)),
    plotHeight,
    plotRight: VIEW.width - VIEW.padRight,
  }
}

export function candles(snapshot: Snapshot, scale: Scale): CandleShape[] {
  return snapshot.bars.map((bar, index) => {
    const open = toNumber(bar.open)
    const close = toNumber(bar.close)
    const up = close >= open
    const bodyTop = scale.y(Math.max(open, close))
    const bodyBottom = scale.y(Math.min(open, close))
    return {
      index,
      time: bar.time,
      up,
      x: scale.x(index),
      wickTop: scale.y(toNumber(bar.high)),
      wickBottom: scale.y(toNumber(bar.low)),
      bodyTop,
      // A doji has zero body and would render as nothing at all; one pixel keeps it visible.
      bodyHeight: Math.max(1, bodyBottom - bodyTop),
      bar,
    }
  })
}

/**
 * The curves, split into unbroken runs.
 *
 * A point whose timestamp is not one of the window's bars is dropped, and the line **breaks**
 * there rather than being joined across the gap. That is the whole reason points carry times:
 * a bridged gap is a hole nobody can see, which is worse than a line that visibly stops.
 *
 * Runs of a single point are dropped too — a polyline needs two, and one orphan point in the
 * middle of a break is noise rather than information.
 */
export function curveRuns(snapshot: Snapshot, scale: Scale): CurveRun[] {
  const indexOf = new Map(snapshot.bars.map((bar, index) => [bar.time, index]))
  const runs: CurveRun[] = []
  for (const series of snapshot.series) {
    let current: string[] = []
    const flush = (): void => {
      if (current.length >= 2) runs.push({ label: series.label, points: current.join(' ') })
      current = []
    }
    for (const [time, value] of series.points) {
      const index = indexOf.get(time)
      if (index === undefined) {
        flush()
        continue
      }
      current.push(`${String(scale.x(index))},${String(scale.y(toNumber(value)))}`)
    }
    flush()
  }
  return runs
}

/**
 * The zone rectangles, extended rightward to the chart's edge.
 *
 * Extended right because that is how the author reads a zone: it stays live until price comes
 * back into it, so the rectangle runs forward and the bars show when it was reached.
 *
 * Cut, not moved, when the zone predates the window: `clipped` says so, and a caller draws a
 * mark rather than pretending the rectangle starts at the first visible bar.
 */
export function regions(snapshot: Snapshot, scale: Scale): RegionShape[] {
  const first = snapshot.bars[0]
  if (first === undefined) return []
  return snapshot.regions.map((region) => {
    const clipped = region.from_time < first.time
    const index = snapshot.bars.findIndex((bar) => bar.time >= region.from_time)
    const left = clipped || index < 0 ? 0 : scale.x(index) - scale.barWidth / 2
    const top = scale.y(toNumber(region.top))
    return {
      label: region.label,
      x: left,
      y: top,
      width: scale.plotRight - left,
      height: Math.max(1, scale.y(toNumber(region.bottom)) - top),
      clipped,
    }
  })
}

/**
 * The broken structural levels, as segments bounded at both ends.
 *
 * A level routinely predates the window — a structure that held for two hundred bars is exactly
 * the kind worth breaking — so the left end is clamped to the chart edge. The right end is the
 * bar that broke it, which for a zone entered long after the break also falls outside, on the
 * other side. `clamped` says a segment ran past what can be drawn, so the caller can mark it
 * rather than let a cut line read as a short one: the length **is** the information here.
 */
export function levelSegments(snapshot: Snapshot, scale: Scale): LevelSegment[] {
  const first = snapshot.bars[0]
  const last = snapshot.bars[snapshot.bars.length - 1]
  if (first === undefined || last === undefined) return []
  const at = (time: string): number | null => {
    const index = snapshot.bars.findIndex((candle) => candle.time === time)
    return index < 0 ? null : scale.x(index)
  }
  return snapshot.levels.map((level) => {
    const start = at(level.from_time)
    const end = at(level.to_time)
    return {
      label: level.label,
      price: toNumber(level.price),
      y: scale.y(toNumber(level.price)),
      x1: start ?? 0,
      x2: end ?? scale.plotRight,
      clamped: start === null || end === null,
    }
  })
}

/**
 * The price labels, pushed apart so they stay readable — with the **lines left where they are**.
 *
 * On this setup the average and the stop routinely land within a pixel of each other: both sit
 * near the decision bar's low. Measured across a real two-year run, 17 of 26 entries had two
 * labels closer together than the type is tall.
 *
 * Only `labelY` moves. `y` stays at the true price, and a caller draws a leader between them,
 * because moving the line would be drawing a price that does not exist.
 */
export function priceLabels(
  input: { entry: number; stop: number | null; average: number | null; hasCurve: boolean },
  scale: Scale,
  format: (value: number) => string,
): PriceLabel[] {
  const labels: PriceLabel[] = []
  const push = (kind: PriceLabel['kind'], price: number, prefix: string): void => {
    const y = scale.y(price)
    labels.push({ kind, price, y, labelY: y, text: `${prefix} ${format(price)}` })
  }
  // The average gets a label only when there is no curve. With one, the line *is* the average
  // and a second horizontal mark at one of its values would read as a level it never was.
  if (input.average !== null && !input.hasCurve) push('average', input.average, 'méd')
  push('entry', input.entry, 'ent')
  if (input.stop !== null) push('stop', input.stop, 'stp')

  // Walked with running extremes rather than by index. Index access under
  // `noUncheckedIndexedAccess` forces `undefined` guards on positions the loop bounds already
  // guarantee — branches no test can reach, which is worse than the arithmetic they protect.
  labels.sort((a, b) => a.y - b.y)

  let lowest = -Infinity
  for (const label of labels) {
    label.labelY = Math.max(label.labelY, lowest + LABEL_GAP)
    lowest = label.labelY
  }

  // Pushing apart only ever moves labels down, so the stack can run off the axis; shift it back.
  const floor = VIEW.height - VIEW.padBottom - 2
  if (lowest > floor) for (const label of labels) label.labelY -= lowest - floor

  // And the shift itself can push the top one above the plot when the labels nearly fill it.
  let highest = Infinity
  for (const label of labels) highest = Math.min(highest, label.labelY)
  const ceiling = VIEW.padTop + 8
  if (highest < ceiling) for (const label of labels) label.labelY += ceiling - highest

  return labels
}

/**
 * Where to put the "decision" and "fill" ticks.
 *
 * Both are looked up by time rather than assumed to be the last two bars: the decision sits in
 * the middle of the window once the broker has extended it to the fill, and on a long rest the
 * two coincide — in which case only one tick is returned, because two labels on one bar overlap
 * into nonsense.
 */
export function markers(snapshot: Snapshot): Marker[] {
  const found: Marker[] = []
  const decision = snapshot.bars.findIndex((bar) => bar.time === snapshot.decided_at)
  const fill = snapshot.bars.findIndex((bar) => bar.time === snapshot.filled_at)
  if (decision >= 0) found.push({ kind: 'decision', index: decision, x: 0 })
  if (fill >= 0 && fill !== decision) found.push({ kind: 'fill', index: fill, x: 0 })
  return found
}
