import {
  CandlestickSeries,
  ColorType,
  LineSeries,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef, useState } from 'react'

import type { Candle, OverlaySeries, Trade, Zone } from '../api/types'
import type { View, ZoneRect } from '../backtest/price'
import { toBars, toCurves, toMarkers, toZones, visibleRangeFor, zoneRects } from '../backtest/price'

// The price a run was executed over, with its trades marked on it.
//
// **The candles are hollow going up and filled going down**, and that is a requirement rather
// than a style. Measured with this project's palette validator against this chart's own surface,
// the up and down hues separate by ΔE 7.5 under deuteranopia — inside the 6–8 band, where colour
// is legal *only* alongside a second encoding. Fill is that encoding, and it is the convention a
// trader already reads. The same measurement is why a winning exit is a circle and a losing one
// a square: on the chart whose subject is which trades worked, the two must not be one mark.
//
// One price scale, never two. Nothing here is indexed or normalised — it is one instrument's own
// price — so the question of a second axis does not arise, and it is not invited.

// Measured surface: slate-900/40 over slate-950, the container this chart sits in.
const SURFACE = '#070d1f'
const UP = '#1FA97E'
const DOWN = '#D96047'
const GRID = '#1e293b'
const AXIS = '#334155'
const INK = '#94a3b8'

// A module constant, and the test `sets the markers through the plugin` is what caught its
// absence: `overlays = []` written in the destructuring builds a *fresh* array on every render,
// so the curves recompute, the effect's dependency changes, and the chart is torn down and
// rebuilt — discarding whatever the reader had zoomed to, on every click.
const NO_OVERLAYS: readonly OverlaySeries[] = []
const NO_ZONES: readonly Zone[] = []

interface Props {
  candles: readonly Candle[]
  trades: readonly Trade[]
  /** The trade the reader picked in the table, or `null`. Drives both the labels and the view. */
  selectedTradeId: number | null
  /** What the strategy was reading. Empty is ordinary — a structure setup draws zones, not lines. */
  overlays?: readonly OverlaySeries[]
  /** The regions it marked. Empty is ordinary too — a swing setup reads a curve and marks none. */
  zones?: readonly Zone[]
  symbol: string
  timeframe: string
}

/**
 * One region: a band of price from the bar that marked it to the bar that took it.
 *
 * Three things are encoded, and none of them is colour alone.
 *
 * * **Which side of the book** is the candle hue — a demand zone in the up colour, a supply zone
 *   in the down one — *and* the thickness of its entry edge. That pair separates by only ΔE 7.5
 *   under deuteranopia, so the edge is not decoration: it is the second encoding. It also carries
 *   real meaning, being the price the limit order actually rests at.
 * * **Still standing or already taken** is fill. A live region is filled; a mitigated one keeps
 *   only its outline, so the chart still shows where it died — which is what explains a stretch
 *   the strategy sat out — without competing with the regions that are still in play.
 * * **Primary or secondary** is a solid against a dashed border. Without it there is no way to
 *   see the effect of the `allow_secondary` flag the run was launched with.
 */
function ZoneShape({ rect }: { rect: ZoneRect }): React.JSX.Element {
  const hue = rect.kind === 'demand' ? UP : DOWN
  // The side price must come back to: a demand region's top, a supply region's bottom.
  const entryEdgeY = rect.kind === 'demand' ? rect.y : rect.y + rect.height
  return (
    <g>
      <rect
        x={rect.x}
        y={rect.y}
        width={rect.width}
        height={rect.height}
        fill={rect.live ? hue : 'none'}
        fillOpacity={rect.live ? 0.1 : 0}
        stroke={hue}
        strokeOpacity={rect.live ? 0.55 : 0.28}
        strokeWidth={1}
        strokeDasharray={rect.primary ? undefined : '4 3'}
      />
      <line
        x1={rect.x}
        x2={rect.x + rect.width}
        y1={entryEdgeY}
        y2={entryEdgeY}
        stroke={hue}
        strokeOpacity={rect.live ? 0.9 : 0.4}
        strokeWidth={2}
        strokeDasharray={rect.primary ? undefined : '4 3'}
      />
    </g>
  )
}

export function PriceChart({
  candles,
  trades,
  selectedTradeId,
  symbol,
  timeframe,
  overlays = NO_OVERLAYS,
  zones = NO_ZONES,
}: Props): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  const priceRef = useRef<ISeriesApi<'Candlestick'> | null>(null)
  // `Time`, not `UTCTimestamp`: the plugin is generic over whatever the series' horizontal scale
  // is, and a candlestick series declares the library's open `Time`. Pinning the ref narrower
  // than the value it holds is what makes the assignment fail rather than the data be wrong.
  const plugin = useRef<ISeriesMarkersPluginApi<Time> | null>(null)

  const bars = useMemo(() => toBars(candles), [candles])
  const marks = useMemo(() => toMarkers(trades, selectedTradeId), [trades, selectedTradeId])
  const curves = useMemo(() => toCurves(overlays), [overlays])
  const regions = useMemo(() => toZones(zones), [zones])
  // Recomputed on every pan and zoom, because the rectangles live in pixel space. `null` while
  // the chart has not reported a view yet — an empty layer, not a layer of nothing at 0,0.
  const [rects, setRects] = useState<ZoneRect[] | null>(null)

  // The chart itself, rebuilt only when the bars change — which for a finished run is once.
  // Keeping it out of the marker and selection effects is what lets a reader click through
  // trades without the zoom they set being thrown away on every click.
  useEffect(() => {
    const element = container.current
    if (!element || bars.length === 0) return

    const chart: IChartApi = createChart(element, {
      height: 420,
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: INK,
      },
      grid: { vertLines: { color: GRID }, horzLines: { color: GRID } },
      rightPriceScale: { borderColor: AXIS },
      timeScale: { borderColor: AXIS, timeVisible: true },
      crosshair: { vertLine: { labelVisible: true } },
    })

    const drawn = chart.addSeries(CandlestickSeries, {
      // Hollow up, filled down: the body of a rising bar is painted in the page behind it, so
      // the outline is all that remains. See the note at the top — this is the second encoding.
      upColor: SURFACE,
      downColor: DOWN,
      borderUpColor: UP,
      borderDownColor: DOWN,
      wickUpColor: UP,
      wickDownColor: DOWN,
    })
    drawn.setData(bars.map((bar) => ({ ...bar, time: bar.time as UTCTimestamp })))

    // The curves the strategy was reading. Each carries its own timestamps and is set as its own
    // series, so a curve that starts late (an average warming up) simply begins where it begins —
    // joining by position would slide every point one warm-up period to the left, and the shape
    // would still look like a moving average.
    for (const curve of curves) {
      const line = chart.addSeries(LineSeries, {
        color: curve.color,
        lineWidth: 2,
        lastValueVisible: false,
        priceLineVisible: false,
      })
      line.setData(curve.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })))
    }

    chartRef.current = chart
    priceRef.current = drawn
    plugin.current = createSeriesMarkers(drawn)
    chart.timeScale().fitContent()

    return () => {
      chartRef.current = null
      priceRef.current = null
      plugin.current = null
      chart.remove()
    }
  }, [bars, curves])

  // Markers are set through the plugin rather than by rebuilding the chart: selecting a trade
  // changes every marker's text, and rebuilding for that would reset the reader's view.
  useEffect(() => {
    plugin.current?.setMarkers(
      marks.map(
        (mark): SeriesMarker<Time> => ({
          time: mark.time as UTCTimestamp,
          position: mark.position,
          shape: mark.shape,
          color: mark.color,
          text: mark.text,
        }),
      ),
    )
  }, [marks])

  // Moving to the chosen trade. `visibleRangeFor` returns null when the trade's bars are not in
  // this series, and a null leaves the view alone — a chart that does not move is a better
  // answer than one scrolled to a range assembled out of a missing index.
  useEffect(() => {
    if (selectedTradeId === null) return
    const chosen = trades.find((trade) => trade.id === selectedTradeId)
    if (!chosen) return
    const range = visibleRangeFor(chosen, bars)
    if (!range) return
    chartRef.current?.timeScale().setVisibleRange({
      from: range.from as UTCTimestamp,
      to: range.to as UTCTimestamp,
    })
  }, [selectedTradeId, trades, bars])

  // The zone layer. lightweight-charts has no rectangle primitive, so the regions are drawn as an
  // SVG layer over the canvas, positioned by the chart's own coordinate conversions. The
  // alternative was a series primitive, which would pan and zoom for free — and would put the
  // geometry inside a canvas renderer, where none of it could be tested. Everything that decides
  // *where* a rectangle goes lives in `zoneRects`; this only asks the chart where things are.
  useEffect(() => {
    const chart = chartRef.current
    const price = priceRef.current
    const element = container.current
    if (!chart || !price || !element || regions.length === 0) {
      setRects(null)
      return
    }

    const measure = (): void => {
      const scale = chart.timeScale()
      const span = scale.getVisibleRange()
      if (span === null) {
        setRects(null)
        return
      }
      const view: View = {
        from: span.from as number,
        to: span.to as number,
        width: element.clientWidth,
        toX: (time) => scale.timeToCoordinate(time as UTCTimestamp),
        toY: (value) => price.priceToCoordinate(value),
      }
      setRects(zoneRects(regions, view))
    }

    measure()
    chart.timeScale().subscribeVisibleTimeRangeChange(measure)
    return () => {
      chart.timeScale().unsubscribeVisibleTimeRangeChange(measure)
    }
  }, [regions, bars])

  if (bars.length === 0) {
    return (
      <p className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-sm text-slate-400">
        This run recorded no candles to chart.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <div className="relative">
        <div
          ref={container}
          role="img"
          aria-label={`${symbol} ${timeframe} price, ${String(bars.length)} candles, with ${String(trades.length)} trades marked`}
          className="rounded-lg border border-slate-800 bg-slate-900/40 p-2"
        />
        {rects !== null && (
          // `pointer-events-none`: the crosshair, the pan and the zoom belong to the canvas
          // underneath. A layer that swallowed the pointer would leave a chart that looks
          // interactive and is not.
          <svg
            className="pointer-events-none absolute inset-0 h-full w-full"
            aria-hidden="true"
          >
            {rects.map((rect, index) => (
              <ZoneShape key={`${String(rect.x)}-${String(rect.y)}-${String(index)}`} rect={rect} />
            ))}
          </svg>
        )}
      </div>
      <ul className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-300">
        {/* The curves first, each directly named. A legend is not optional here: the line hues are
            only legal alongside a label, and "which average is this" is the question the caption
            answers. Text stays in ink; the swatch beside it carries the colour. */}
        {curves.map((curve) => (
          <li key={curve.label} className="flex items-center gap-2">
            <span
              aria-hidden
              className="h-0.5 w-4 shrink-0 rounded-full"
              style={{ backgroundColor: curve.color }}
            />
            {curve.label}
          </li>
        ))}
        <li className="flex items-center gap-2">
          <span aria-hidden style={{ color: '#5F8AD2' }}>
            ▲▼
          </span>
          entry — up for a long, down for a short
        </li>
        <li className="flex items-center gap-2">
          <span aria-hidden style={{ color: UP }}>
            ●
          </span>
          exit in profit, after costs
        </li>
        <li className="flex items-center gap-2">
          <span aria-hidden style={{ color: DOWN }}>
            ■
          </span>
          exit at a loss
        </li>
      </ul>
    </div>
  )
}
