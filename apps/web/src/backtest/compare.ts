// Comparing runs: which ones are on screen, what colour each one wears, and how a curve in money
// becomes a curve in percent.
//
// No React and no charting library here on purpose. The chart draws on a canvas, which jsdom does
// not implement, so anything living inside the component is code no test can look at. Everything
// below is the part worth proving — the seat rule, the normalisation, the cost reading — and it is
// exercised directly. The component that consumes it is deliberately dumb.

import type { BacktestListItem, EquityPoint } from '../api/types'

/**
 * How many runs can be compared at once.
 *
 * Not a storage limit — a legibility one. Categorical colour runs out of distinguishable hues
 * around six to eight, and past that a reader is matching line to legend by guesswork. The seventh
 * tick is refused with a reason rather than granted into an unreadable chart.
 */
export const MAX_COMPARED = 6

/**
 * The categorical palette, in fixed order.
 *
 * These are the first six slots of the reference categorical theme, stepped for a dark surface.
 * Validated as a set against this app's actual chart surface (`#070d1f`, slate-900/40 over
 * slate-950): every slot inside the dark lightness band, above the chroma floor, above 3:1
 * contrast, worst adjacent CVD separation ΔE 8.4 and worst normal-vision separation ΔE 19.3.
 *
 * The *order* is the colourblind-safety mechanism, not decoration. Re-ordering these, or dropping
 * one from the middle, invalidates the measurement — re-run the validator if you touch them.
 */
export const SERIES_COLORS: readonly string[] = [
  '#3987e5', // blue
  '#d95926', // orange
  '#199e70', // aqua
  '#c98500', // yellow
  '#d55181', // magenta
  '#008300', // green
]

/**
 * Which run sits in which colour slot. A fixed-length row of seats, `null` where empty.
 *
 * This is a *seating chart*, not a list, and the difference is the whole reason the type exists.
 * Colour has to follow the run, never its rank: if the palette were assigned by position in a
 * plain array of selected ids, unticking the first run would shift every survivor one slot left
 * and repaint the entire chart. A reader who removed one line to see the rest better would find
 * all of them changed colour — the comparison they were in the middle of making, destroyed by the
 * act of narrowing it.
 *
 * A seat is claimed when a run is ticked and released when it is unticked. Nobody else moves.
 */
export type Seats = readonly (string | null)[]

export const EMPTY_SEATS: Seats = Object.freeze(Array.from({ length: MAX_COMPARED }, () => null))

/** The colour slot this run occupies, or `null` if it is not selected. */
export function seatOf(seats: Seats, id: string): number | null {
  const index = seats.indexOf(id)
  return index === -1 ? null : index
}

export function isSelected(seats: Seats, id: string): boolean {
  return seats.includes(id)
}

/** The colour this run is drawn in, or `null` if it is not on the chart. */
export function colorOf(seats: Seats, id: string): string | null {
  const seat = seatOf(seats, id)
  return seat === null ? null : (SERIES_COLORS[seat] ?? null)
}

/** The selected runs in seat order — the order the legend and the comparison table read in. */
export function selectedIds(seats: Seats): string[] {
  return seats.filter((id): id is string => id !== null)
}

export function isFull(seats: Seats): boolean {
  return !seats.includes(null)
}

/**
 * Tick or untick a run.
 *
 * Ticking claims the **lowest free seat**, so the palette is used from the top down and a chart of
 * two runs is always blue and orange rather than whichever slots happen to be free. Unticking
 * frees that one seat and touches nothing else. Ticking when every seat is taken is a no-op: the
 * caller disables the control and says why, rather than silently evicting a run the reader chose.
 */
export function toggleSeat(seats: Seats, id: string): Seats {
  const seat = seatOf(seats, id)
  if (seat !== null) {
    return seats.map((occupant, index) => (index === seat ? null : occupant))
  }
  const free = seats.indexOf(null)
  if (free === -1) return seats
  return seats.map((occupant, index) => (index === free ? id : occupant))
}

/** One point of a normalised curve, in the shape the chart wants: epoch seconds and a percent. */
export interface PercentPoint {
  time: number
  value: number
}

/**
 * An equity curve in money, re-expressed as return on the capital the run started with.
 *
 * Anchored on `initial_capital` rather than on the curve's own first point. The two are nearly
 * always equal — the engine writes a point per candle, so the first one is the close of the first
 * bar, usually before any trade — but "nearly always" is not a definition. Anchoring on the
 * declared capital says exactly *return on what was put on the table*, which is what makes a
 * $10 000 run and a $100 000 run readable on one axis. Anchoring on the first point would quietly
 * measure from wherever the first bar happened to leave the account.
 *
 * A run declaring zero capital yields an empty curve rather than a division by zero: there is no
 * percentage of nothing, and a chart of `Infinity` would be worse than a chart of nothing.
 *
 * This is the one place in the app where an exact decimal from the backend becomes a float, and it
 * is the right place: the value that decided anything already decided it in Postgres, and what
 * comes out of here only ever gets drawn. Do not route a number from this function back into a
 * comparison or a total.
 */
export function percentCurve(points: EquityPoint[], initialCapital: string): PercentPoint[] {
  const capital = Number(initialCapital)
  if (!Number.isFinite(capital) || capital === 0) return []
  return points.map((point) => ({
    time: Date.parse(point.time) / 1000,
    value: (Number(point.equity) / capital - 1) * 100,
  }))
}

export interface ComparisonSeries {
  id: string
  label: string
  color: string
  points: PercentPoint[]
}

/**
 * The series to draw, in seat order, for the runs whose curves have arrived.
 *
 * Curves load one request per run and land at different moments, so a selected run with no curve
 * yet is simply absent — the chart grows a line when its data arrives instead of waiting for the
 * slowest one. The seat, and therefore the colour, was already decided when the box was ticked, so
 * a line appearing late does not shift the colours of the lines already drawn.
 *
 * **Nothing is resampled onto a common time grid.** Two runs over different windows are drawn on
 * the times each of them actually has, and the chart unions the domains. Forward-filling one curve
 * across the other's window would write "equity held flat here" where the truth is "this run did
 * not exist here" — a fabricated flat stretch, indistinguishable on screen from a real one.
 */
export function buildSeries(
  seats: Seats,
  runs: readonly BacktestListItem[],
  curves: ReadonlyMap<string, EquityPoint[]>,
): ComparisonSeries[] {
  const byId = new Map(runs.map((run) => [run.id, run]))
  const series: ComparisonSeries[] = []
  seats.forEach((id, seat) => {
    if (id === null) return
    const run = byId.get(id)
    const points = curves.get(id)
    const color = SERIES_COLORS[seat]
    if (run === undefined || points === undefined || color === undefined) return
    series.push({ id, label: runLabel(run), color, points: percentCurve(points, run.initial_capital) })
  })
  return series
}

/** What names a run to a human: the instrument it ran on and the strategy version that ran. */
export function runLabel(run: BacktestListItem): string {
  return `${run.symbol} ${run.timeframe} · ${run.strategy_name} v${String(run.strategy_version)}`
}

/**
 * The accessible name of a run's compare control — `runLabel` plus what tells two of them apart.
 *
 * `runLabel` is deliberately not enough here, and the reason is this project's actual usage: the
 * same strategy is re-run on the same instrument and timeframe over and over, which is the whole
 * point of a research log. Six controls all announcing "compare AAPL H1 Ponto Contínuo v2" leave a
 * screen-reader user with no way to know which row they are on.
 *
 * The window and the launch instant are what differ. Two runs of an identical configuration
 * launched in the same second would still collide, but that pairing has no meaning to distinguish
 * anyway — it is the same experiment run twice.
 */
export function compareLabel(run: BacktestListItem): string {
  const window = `${run.date_from.slice(0, 10)} to ${run.date_to.slice(0, 10)}`
  return `compare ${runLabel(run)}, ${window}, launched ${run.created_at}`
}

/**
 * Did this run charge itself anything to trade?
 *
 * Worth its own function because the answer decides whether two rows are comparable at all. Under
 * ADR-07 costs are plugged in, and `{"type": "none"}` is a legitimate, deliberate choice — it just
 * means the result is an upper bound, not a P&L. On a wide-stop instrument the difference is
 * rounding; on M15 forex a pip of spread can eat a real fraction of the R, and a costless run
 * placed beside a costed one in the same table reads as a like-for-like win when it is not.
 *
 * Unknown shapes count as costed, not costless. Claiming "no costs" about a document this code
 * does not recognise would be an assertion it has no basis for; the label falls back to the raw
 * type, which at least tells the reader to go look.
 */
export function isCostless(costModel: Record<string, unknown>): boolean {
  return costModel.type === 'none'
}

/** The cost model as a table cell: short enough for a column, specific enough to compare. */
export function costLabel(costModel: Record<string, unknown>): string {
  const kind = costModel.type
  if (kind === 'none') return 'none'
  if (kind === 'spread') return `spread ${String(costModel.spread_points)} pts`
  if (kind === 'commission') return `commission ${String(costModel.commission_per_unit)}/unit`
  return typeof kind === 'string' ? kind : 'unknown'
}

/** The selected runs that charged nothing to trade — what the comparison warning is about. */
export function costlessAmong(
  seats: Seats,
  runs: readonly BacktestListItem[],
): BacktestListItem[] {
  const chosen = new Set(selectedIds(seats))
  return runs.filter((run) => chosen.has(run.id) && isCostless(run.cost_model))
}
