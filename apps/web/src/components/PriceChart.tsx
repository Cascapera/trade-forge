import {
  CandlestickSeries,
  ColorType,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesMarkersPluginApi,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useMemo, useRef } from 'react'

import type { Candle, Trade } from '../api/types'
import { toBars, toMarkers, visibleRangeFor } from '../backtest/price'

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

interface Props {
  candles: readonly Candle[]
  trades: readonly Trade[]
  /** The trade the reader picked in the table, or `null`. Drives both the labels and the view. */
  selectedTradeId: number | null
  symbol: string
  timeframe: string
}

export function PriceChart({
  candles,
  trades,
  selectedTradeId,
  symbol,
  timeframe,
}: Props): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null)
  const chartRef = useRef<IChartApi | null>(null)
  // `Time`, not `UTCTimestamp`: the plugin is generic over whatever the series' horizontal scale
  // is, and a candlestick series declares the library's open `Time`. Pinning the ref narrower
  // than the value it holds is what makes the assignment fail rather than the data be wrong.
  const plugin = useRef<ISeriesMarkersPluginApi<Time> | null>(null)

  const bars = useMemo(() => toBars(candles), [candles])
  const marks = useMemo(() => toMarkers(trades, selectedTradeId), [trades, selectedTradeId])

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

    chartRef.current = chart
    plugin.current = createSeriesMarkers(drawn)
    chart.timeScale().fitContent()

    return () => {
      chartRef.current = null
      plugin.current = null
      chart.remove()
    }
  }, [bars])

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

  if (bars.length === 0) {
    return (
      <p className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-sm text-slate-400">
        This run recorded no candles to chart.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <div
        ref={container}
        role="img"
        aria-label={`${symbol} ${timeframe} price, ${String(bars.length)} candles, with ${String(trades.length)} trades marked`}
        className="rounded-lg border border-slate-800 bg-slate-900/40 p-2"
      />
      <ul className="flex flex-wrap gap-x-5 gap-y-2 text-xs text-slate-300">
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
