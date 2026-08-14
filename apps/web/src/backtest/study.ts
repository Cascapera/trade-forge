// Laying a study's points out as a grid, and colouring them by what they returned.
//
// The heatmap exists to answer one question, and it is *not* "which parameters won". It is
// **what shape does the good region have** — a broad plateau, or one bright cell surrounded by
// losses. A plateau says the method tolerates being slightly wrong about its parameters, which
// is the only kind of result that survives contact with a market that has moved on. A lone
// bright cell says the search found the luckiest arrangement of the same noise, and the number
// in it is the least trustworthy number on the screen.
//
// Everything that decides where a cell goes and what colour it is lives here, with no React and
// no DOM, so it can be asserted directly.

import type { StudyOut, StudyPoint } from '../api/types'

/**
 * The diverging scale's two poles, and the neutral it passes through at zero.
 *
 * ⚠️ **Blue for profit, not the green used everywhere else in this app, and it is not a whim.**
 * On a heatmap the colour *is* the value: a cell has no room for the second encoding a candle
 * has (hollow against filled) or a region has (a thicker entry edge). Measured with the palette
 * validator against this app's own surface, the candle pair separates by only ΔE 7.5 under
 * deuteranopia — inside the 6–8 band that is legal only *with* a second encoding — while blue
 * against the same red measures 20.3. For a reader with the commonest colour blindness the green
 * version would not be a degraded chart, it would be one where profitable and losing regions are
 * the same picture.
 *
 * No new colour was introduced: `#5F8AD2` is already the app's entry-limit marker. The neutral
 * is the chart furniture's own slate, so a point that returned nothing reads as nothing.
 */
export const GAIN = '#5F8AD2'
export const LOSS = '#D96047'
export const NEUTRAL = '#334155'

/** A point that has not finished: no return to colour, and it must not read as a flat zero. */
export const PENDING = '#0f172a'

export interface Cell {
  /** Index along the first axis — the rows of the drawn grid. */
  row: number
  /** Index along the second axis, or 0 when a study varies only one parameter. */
  column: number
  label: string
  backtestId: string
  /** The run's return as a fraction of the capital it started with, or null while it is not in. */
  ret: number | null
  fill: string
}

export interface Layout {
  /** The path and values of the axis drawn down the side. */
  rows: { path: string; values: readonly unknown[] }
  /** The axis drawn across the top, or null when the study varies a single parameter. */
  columns: { path: string; values: readonly unknown[] } | null
  cells: readonly Cell[]
  /** The largest absolute return in the study, which is what the scale is normalised against. */
  extent: number
  /**
   * Axes beyond the two that can be drawn.
   *
   * A grid of three parameters is a cube, and there is no honest way to flatten one onto a
   * rectangle — cells would silently overlap, and the picture would look like a search of a
   * space half its real size. Named here so the screen can say what it is not showing rather
   * than showing it wrongly.
   */
  undrawn: readonly string[]
}

/** The leaf of a dotted path: `setup.params.period` reads as `period` on an axis. */
export function axisName(path: string): string {
  return path.split('.').at(-1) ?? path
}

/**
 * Where a return sits on the diverging scale, as a colour.
 *
 * `extent` normalises both arms against the **same** number — the largest absolute return in the
 * study — so a -10% cell and a +10% cell are equally strong. Scaling each arm to its own maximum
 * is the tempting alternative and it lies: in a study whose worst point lost 2% and whose best
 * made 40%, it would paint that 2% loss as vividly as the 40% gain.
 */
export function fillFor(ret: number | null, extent: number): string {
  if (ret === null) return PENDING
  if (extent === 0 || ret === 0) return NEUTRAL
  const strength = Math.min(Math.abs(ret) / extent, 1)
  // Mixed against the neutral rather than faded to transparent: a translucent cell picks up
  // whatever is behind it, and the thing behind a cell is another cell's border.
  return mix(NEUTRAL, ret > 0 ? GAIN : LOSS, strength)
}

/**
 * Two hex colours blended in sRGB. Enough for a scale read as "stronger" and "weaker".
 *
 * ⚠️ Upper case, matching the constants above, and that is not cosmetic: at full strength this
 * returns the pole itself, and a caller asking `fill === GAIN` — a legend, a test, a screenshot
 * comparison — would get `false` from `#5f8ad2` against `#5F8AD2` while the two render
 * identically. A difference that is invisible on screen and decisive in code.
 */
function mix(from: string, to: string, amount: number): string {
  const a = channels(from)
  const b = channels(to)
  const blended = a.map((value, index) =>
    Math.round(value + ((b[index] ?? value) - value) * amount),
  )
  return `#${blended.map((value) => value.toString(16).padStart(2, '0')).join('')}`.toUpperCase()
}

function channels(hex: string): number[] {
  return [1, 3, 5].map((at) => Number.parseInt(hex.slice(at, at + 2), 16))
}

/**
 * The study's points arranged on their axes, ready to draw.
 *
 * Placement comes from each point's own `values`, never from its position in the array and never
 * from parsing its label. The server already sorts the points, but a client that *depended* on
 * that order would break silently the day a row is re-read in a different one — and a heatmap
 * drawn from the wrong order is not obviously wrong, it is just a different picture.
 */
export function layoutOf(study: StudyOut): Layout {
  const axes = Object.entries(study.grid)
  const [rows, columns] = [axes[0], axes[1]]
  if (rows === undefined) return empty()

  const returns = new Map(
    study.runs.map((run) => [
      run.id,
      run.metrics === null ? null : Number(run.metrics.net_profit) / Number(run.initial_capital),
    ]),
  )
  const extent = Math.max(
    0,
    ...[...returns.values()].filter((value): value is number => value !== null).map(Math.abs),
  )

  const cells = study.points.map((point: StudyPoint) => {
    const ret = returns.get(point.backtest_id) ?? null
    return {
      row: indexOn(rows[1], point.values[rows[0]]),
      column: columns === undefined ? 0 : indexOn(columns[1], point.values[columns[0]]),
      label: point.label,
      backtestId: point.backtest_id,
      ret,
      fill: fillFor(ret, extent),
    }
  })

  return {
    rows: { path: rows[0], values: rows[1] },
    columns: columns === undefined ? null : { path: columns[0], values: columns[1] },
    cells,
    extent,
    undrawn: axes.slice(2).map(([path]) => path),
  }
}

/**
 * Which position a value occupies on its axis, or -1 for one the axis does not list.
 *
 * -1 rather than 0: a value the grid does not know about is not the first one, and placing it
 * there would stack it on top of a cell that is real. The screen drops those, and says so.
 */
function indexOn(values: readonly unknown[], value: unknown): number {
  return values.findIndex((candidate) => Object.is(candidate, value))
}

function empty(): Layout {
  return {
    rows: { path: '', values: [] },
    columns: null,
    cells: [],
    extent: 0,
    undrawn: [],
  }
}
