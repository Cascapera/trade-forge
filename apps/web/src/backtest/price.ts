// Turning a run's candles and trades into what a candlestick chart draws.
//
// Everything here is pure: strings off the wire become numbers and seconds, trades become
// markers, and a chosen trade becomes a visible range. `PriceChart` does the canvas and nothing
// else — which is what lets these rules be tested without a chart at all, since jsdom has no
// canvas for lightweight-charts to draw on.

import type { Candle, OverlaySeries, Trade, Zone } from '../api/types'

/** A bar as lightweight-charts wants it: seconds, and numbers rather than exact decimals. */
export interface Bar {
  time: number
  open: number
  high: number
  low: number
  close: number
}

export type MarkerShape = 'arrowUp' | 'arrowDown' | 'circle' | 'square'
export type MarkerPosition = 'aboveBar' | 'belowBar'

export interface Marker {
  time: number
  position: MarkerPosition
  shape: MarkerShape
  color: string
  text: string
  /** The trade this mark belongs to, so a click on the chart could find its row. */
  tradeId: number
}

// The same hues `TradeSnapshot` uses, and reused rather than re-picked: they were checked
// against this app's own surface (slate-900/40 over slate-950) for contrast and for colour-vision
// separation, and a second, eyeballed pair of green and red would quietly undo that.
const UP = '#1FA97E'
const DOWN = '#D96047'
// Entry is neither good nor bad news, so it does not borrow the outcome palette. It is the same
// blue the snapshot draws the entry level in, which makes the two charts read as one system.
const ENTRY = '#5F8AD2'

/**
 * An ISO instant as whole seconds.
 *
 * lightweight-charts indexes its horizontal scale in seconds; milliseconds would place every
 * bar a thousand times further out than the axis expects, and the chart would render empty
 * rather than complain.
 */
export function toSeconds(iso: string): number {
  return Math.floor(Date.parse(iso) / 1000)
}

/**
 * Bars for the chart, in the order they arrived.
 *
 * Prices become floats here, and that is safe for exactly one reason: nothing downstream does
 * arithmetic with them. They are drawn. Every number that is *accounted* with — P&L, R multiple,
 * costs — is computed in the engine in `Decimal` and rendered from its own string.
 */
export function toBars(candles: readonly Candle[]): Bar[] {
  return candles.map((candle) => ({
    time: toSeconds(candle.time),
    open: Number(candle.open),
    high: Number(candle.high),
    low: Number(candle.low),
    close: Number(candle.close),
  }))
}

/**
 * The hues an overlay curve can wear, in fixed order.
 *
 * The first is the same gold `TradeSnapshot` draws an average in, so the average is the same
 * colour whether you are looking at one entry or the whole run — the two charts read as one
 * system rather than as two pictures of the same thing.
 *
 * Measured as a set against this chart's surface together with the candle and entry hues, not
 * picked: with one curve the worst normal-vision adjacent pair is ΔE 24.8, with two it is 19.2,
 * and every colour clears 3:1 against `#070d1f`. Reordering this list or dropping one from the
 * middle invalidates that measurement.
 *
 * Assigned by position, which is safe **here** and would not be everywhere: the series come back
 * in the order the strategy declares them and nothing on this screen filters one out, so a
 * position is a stable identity. The run comparison chart faces the opposite situation and needs
 * a seating chart for exactly that reason — see `compare.ts`.
 */
export const CURVE_COLORS: readonly string[] = ['#BC8620', '#d55181', '#3987e5']

/** How a curve is drawn within its family. The first component of an indicator is the subject. */
export type CurveStroke = 'solid' | 'dashed' | 'dotted'

const STROKES: readonly CurveStroke[] = ['solid', 'dashed', 'dotted']

export interface Curve {
  label: string
  color: string
  stroke: CurveStroke
  points: { time: number; value: number }[]
}

/**
 * The indicator a series belongs to: `bb.upper` and `bb.lower` are two readings of one `bb`.
 *
 * Splitting on the first dot is enough because the ref grammar has exactly two shapes that reach
 * an indicator — a bare id, and `id.component` — and neither `price.*` nor `candle[-N].*` is ever
 * an overlay label.
 */
function entityOf(label: string): string {
  const dot = label.indexOf('.')
  return dot === -1 ? label : label.slice(0, dot)
}

/**
 * Overlay series off the wire, ready to draw.
 *
 * ⚠️ **The palette is spent per indicator, not per curve, and that is the whole reason this
 * function has to group.** A Bollinger is one declaration with three readings, so colouring by
 * position would hand its bands three unrelated hues — the reader sees three indicators — and
 * exhaust a three-hue palette on a single `bb`, dropping every other curve the strategy declared.
 * Hue therefore follows the *entity*: the three bands share one, and are told apart by stroke and
 * by where they sit on the chart. Same principle the run-comparison chart needed a seating chart
 * for: colour follows the thing, never the slot it arrived in.
 *
 * Within a family, the **first component is solid and the rest are dashed then dotted**. Nothing
 * here knows what a band is — the order comes from the engine, which declares components primary
 * first precisely so a drawing routine can make this call without knowing.
 *
 * Indicators past the palette are **dropped, not recycled**. A fourth hue repeating the first
 * would be two indicators the reader is invited to read as one, and a legend naming both in the
 * same swatch says nothing. Losing a line visibly beats lying about the ones that are kept.
 */
export function toCurves(series: readonly OverlaySeries[]): Curve[] {
  const families: string[] = []
  const grouped = new Map<string, OverlaySeries[]>()
  for (const one of series) {
    const entity = entityOf(one.label)
    const family = grouped.get(entity)
    if (family === undefined) {
      families.push(entity)
      grouped.set(entity, [one])
    } else {
      family.push(one)
    }
  }

  // Walking the palette rather than the families is what makes the cap structural: there is no
  // index here the palette does not have, so no assertion is needed to say so.
  return CURVE_COLORS.flatMap((color, index) => {
    const entity = families[index]
    if (entity === undefined) return []
    const family = grouped.get(entity) ?? []
    return family.map((one, position) => ({
      label: one.label,
      color,
      // Past the third component every further reading stays dotted rather than dropping out:
      // an indicator with four outputs is a legend problem, not a reason to hide a line.
      stroke: STROKES[position] ?? 'dotted',
      points: one.points.map(([time, value]) => ({
        time: toSeconds(time),
        value: Number(value),
      })),
    }))
  })
}

/** `+2.30R`, or `—` when the run recorded no R multiple for the trade. */
function rLabel(trade: Trade): string {
  if (trade.r_multiple === null) return '—'
  const value = Number(trade.r_multiple)
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(2)}R`
}

/**
 * Entry and exit marks for every trade, ordered by time.
 *
 * **Sorted, and not incidentally.** lightweight-charts requires markers in ascending time and
 * does not sort them for you. Trades arrive newest-first from the API, and one trade's exit
 * routinely shares a bar with the next one's entry, so neither the input order nor a naive
 * interleave is ascending.
 *
 * **Text only on the selected trade.** A run of a few hundred trades would carry a few hundred
 * labels, which is mud at any zoom that shows more than a day. The selected trade wearing its R
 * multiple is also what makes clicking a row *visible* on the chart, rather than a silent scroll.
 *
 * **A trade with no exit gets no exit mark.** A position still open when the run ended has no
 * closing bar, and inventing one — at the last candle, say — would draw an exit the strategy
 * never took, in the one place a reader goes to check what actually happened.
 */
export function toMarkers(trades: readonly Trade[], selectedId: number | null): Marker[] {
  const marks: Marker[] = []
  for (const trade of trades) {
    const chosen = trade.id === selectedId
    const long = trade.direction === 'long'
    marks.push({
      time: toSeconds(trade.entry_time),
      // Below a long, above a short: the arrow points the way the trade expects price to go,
      // and sits on the side it would go from.
      position: long ? 'belowBar' : 'aboveBar',
      shape: long ? 'arrowUp' : 'arrowDown',
      color: ENTRY,
      text: chosen ? `${trade.direction} entry` : '',
      tradeId: trade.id,
    })
    if (trade.exit_time === null) continue
    // Outcome, not direction: a short that made money is an up-coloured exit. `net_pnl` is the
    // number after costs, which is the only one that decides whether the trade was worth taking.
    const won = trade.net_pnl !== null && Number(trade.net_pnl) >= 0
    marks.push({
      time: toSeconds(trade.exit_time),
      position: long ? 'aboveBar' : 'belowBar',
      // ⚠️ Shape carries the outcome as well as colour, and it is not decoration. Measured with
      // this project's own palette validator against the chart's surface, the up and down hues
      // separate by **ΔE 7.5 under deuteranopia** — inside the 6–8 band, which is legal only
      // with a second encoding. Colour alone here would make winners and losers the same mark
      // for a red-green reader, on the one chart whose entire subject is which is which.
      shape: won ? 'circle' : 'square',
      color: won ? UP : DOWN,
      text: chosen ? rLabel(trade) : '',
      tradeId: trade.id,
    })
  }
  return marks.sort((a, b) => a.time - b.time)
}

/**
 * The window to show when a trade is chosen: the trade, plus context on either side.
 *
 * **Padded in bars, never in time**, and that is the whole subtlety. Markets have holes — a
 * weekend, a holiday, the hours a stock exchange is shut. Padding an entry by "six hours" on a
 * Friday evening asks the chart for a range in which no bar exists, and lightweight-charts
 * honours it: an empty window, which reads as a bug in the data rather than in the arithmetic.
 * Counting bars steps over the hole, because the hole is not in the array.
 *
 * Returns `null` when the trade's bars are not in this series at all — which should not happen,
 * since the run read these very candles, but a null is a chart that does not move rather than a
 * range built from `-1`.
 */
export function visibleRangeFor(
  trade: Trade,
  bars: readonly Bar[],
  padBars = 12,
): { from: number; to: number } | null {
  const entry = toSeconds(trade.entry_time)
  const exit = trade.exit_time === null ? entry : toSeconds(trade.exit_time)
  const first = indexAtOrBefore(bars, entry)
  const last = indexAtOrBefore(bars, exit)
  if (first === -1 || last === -1) return null

  const from = bars[Math.max(0, first - padBars)]
  const to = bars[Math.min(bars.length - 1, last + padBars)]
  if (from === undefined || to === undefined) return null
  return { from: from.time, to: to.time }
}

/**
 * The last bar at or before an instant.
 *
 * At-or-before rather than exact: a fill is stamped with the bar it happened on, but a run's
 * candles and a trade's timestamps travel separately, and an equality test that missed by a
 * second would silently refuse to move the chart. Linear because a run's bars are thousands,
 * not millions, and this runs on a click.
 */
function indexAtOrBefore(bars: readonly Bar[], time: number): number {
  let found = -1
  for (let i = 0; i < bars.length; i += 1) {
    const bar = bars[i]
    if (bar === undefined || bar.time > time) break
    found = i
  }
  return found
}

// --------------------------------------------------------------------------- //
// Regions                                                                       //
// --------------------------------------------------------------------------- //

/** A region with its numbers parsed: seconds on the axis, floats for the price band. */
export interface DrawableZone {
  kind: 'demand' | 'supply'
  top: number
  bottom: number
  fromTime: number
  mitigatedAt: number | null
  primary: boolean
}

export function toZones(zones: readonly Zone[]): DrawableZone[] {
  return zones.map((zone) => ({
    kind: zone.kind,
    top: Number(zone.top),
    bottom: Number(zone.bottom),
    fromTime: toSeconds(zone.from_time),
    mitigatedAt: zone.mitigated_at === null ? null : toSeconds(zone.mitigated_at),
    primary: zone.primary,
  }))
}

/** What the chart can tell us about where things are, in pixels. */
export interface View {
  /** The visible span in seconds, from the chart's own time scale. */
  from: number
  to: number
  width: number
  /** Seconds to an x offset, or null when the instant falls outside the visible span. */
  toX: (time: number) => number | null
  /** Price to a y offset, or null when it falls off the price scale. */
  toY: (price: number) => number | null
}

export interface ZoneRect {
  x: number
  y: number
  width: number
  height: number
  kind: 'demand' | 'supply'
  /**
   * Still standing when the run ended — never merely when the visible window ends.
   *
   * A region taken off to the right of what is on screen is drawn to the edge like a live one,
   * because that is where its rectangle genuinely runs, but it is `live: false` and keeps the
   * faded styling of a region that died. Panning cannot bring a zone back to life: what the
   * fill says is what the run found, not what happens to be scrolled into view.
   */
  live: boolean
  primary: boolean
  /** Clipped at the chart's left edge — the region began before anything on screen. */
  clippedLeft: boolean
}

/**
 * Regions as rectangles in the chart's pixel space.
 *
 * **Clipped at the edges, never moved to them.** A region routinely begins long before the
 * visible window — the rectangle really does start there — so a zone whose `fromTime` is off
 * screen is drawn from x=0 and flagged `clippedLeft`. Moving its start to the first visible bar
 * would redraw the zone as younger than it is, which is the one thing the rectangle exists to
 * say. The same rule the entry snapshot states for its own regions.
 *
 * **A region with no mitigation runs to the right edge.** `mitigatedAt === null` is "still
 * standing", not "unknown", and closing it at some invented bar would claim price came back when
 * it never did.
 *
 * Zones wholly outside the window are dropped rather than clamped to zero width: a rectangle of
 * no width is still a rectangle, and a row of them along an edge reads as regions that existed
 * there.
 */
export function zoneRects(zones: readonly DrawableZone[], view: View): ZoneRect[] {
  const out: ZoneRect[] = []
  for (const zone of zones) {
    const ends = zone.mitigatedAt
    if (zone.fromTime > view.to) continue
    if (ends !== null && ends < view.from) continue

    const clippedLeft = zone.fromTime < view.from
    const left = clippedLeft ? 0 : (view.toX(zone.fromTime) ?? 0)
    const right = ends === null || ends > view.to ? view.width : (view.toX(ends) ?? view.width)

    const top = view.toY(zone.top)
    const bottom = view.toY(zone.bottom)
    if (top === null || bottom === null) continue

    out.push({
      x: left,
      y: top,
      width: Math.max(right - left, 1),
      height: Math.max(bottom - top, 1),
      kind: zone.kind,
      live: ends === null,
      primary: zone.primary,
      clippedLeft,
    })
  }
  return out
}
