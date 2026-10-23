import {
  ColorType,
  LineSeries,
  LineStyle,
  createChart,
  type IChartApi,
  type ISeriesApi,
  type MouseEventParams,
  type UTCTimestamp,
} from 'lightweight-charts'
import { useEffect, useRef, useState } from 'react'

import type { ComparisonSeries } from '../backtest/compare'

// Several runs' equity, indexed to percent, on one time axis.
//
// One axis, never two — the reason the curves are normalised in the first place. A run on $10 000
// and a run on $100 000 plotted in money would need two price scales, and a dual-axis chart lets
// the author decide where the lines cross by choosing the scales. In percent they share one scale
// and the crossing is the data's.
//
// Lines rather than the filled area a single curve gets: six translucent fills stacked on each
// other read as mud, and the fill was only ever carrying "this is the one thing here".
//
// Identity is never colour alone. The legend below is always present, and up to four series also
// wear the price scale's last-value badge — a direct label at the end of the line. Past four those
// badges collide, so the legend carries it by itself.
const DIRECT_LABEL_LIMIT = 4

/** The percent axis, formatted once so the badge, the crosshair and the legend all agree. */
function formatPercent(value: number): string {
  return `${value >= 0 ? '+' : '−'}${Math.abs(value).toFixed(1)}%`
}

export function ComparisonChart({ series }: { series: ComparisonSeries[] }): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null)
  // What the crosshair is currently over, keyed by series id. Empty when the pointer is away, and
  // the legend then falls back to each line's final value — which is the number a reader wants
  // when they are not pointing at anything in particular.
  const [hovered, setHovered] = useState<Record<string, number>>({})

  useEffect(() => {
    const element = container.current
    if (!element || series.length === 0) return

    const chart: IChartApi = createChart(element, {
      height: 320,
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#94a3b8',
      },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      rightPriceScale: { borderColor: '#334155' },
      timeScale: { borderColor: '#334155', timeVisible: true },
      localization: { priceFormatter: formatPercent },
      crosshair: { vertLine: { labelVisible: true } },
    })

    const drawn = new Map<string, ISeriesApi<'Line'>>()
    for (const one of series) {
      const line = chart.addSeries(LineSeries, {
        color: one.color,
        lineWidth: 2,
        lastValueVisible: series.length <= DIRECT_LABEL_LIMIT,
        priceLineVisible: false,
        priceFormat: { type: 'custom', formatter: formatPercent, minMove: 0.01 },
      })
      line.setData(one.points.map((p) => ({ time: p.time as UTCTimestamp, value: p.value })))
      drawn.set(one.id, line)
    }

    // Break-even, drawn on the first series because a price line has to hang off one. It is the
    // only reference that matters here: above it the run made money, below it lost.
    const first = drawn.get(series[0]?.id ?? '')
    first?.createPriceLine({
      price: 0,
      color: '#475569',
      lineWidth: 1,
      lineStyle: LineStyle.Dashed,
      axisLabelVisible: false,
      title: '',
    })

    // The hover layer. lightweight-charts brings the crosshair; this turns the values under it
    // into the legend's numbers, so one pointer position reads all six runs at once.
    const onCrosshair = (param: MouseEventParams): void => {
      if (param.time === undefined) {
        setHovered({})
        return
      }
      const reading: Record<string, number> = {}
      for (const [id, line] of drawn) {
        const point = param.seriesData.get(line)
        if (point && 'value' in point && typeof point.value === 'number') reading[id] = point.value
      }
      setHovered(reading)
    }
    chart.subscribeCrosshairMove(onCrosshair)
    chart.timeScale().fitContent()

    return () => {
      chart.unsubscribeCrosshairMove(onCrosshair)
      chart.remove()
    }
  }, [series])

  if (series.length === 0) {
    return (
      <p className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-sm text-slate-400">
        Pick a run to see its equity curve. Pick more than one to compare them.
      </p>
    )
  }

  return (
    <div className="space-y-3">
      <div
        ref={container}
        role="img"
        aria-label={`equity curves, in percent, for ${String(series.length)} runs`}
        className="rounded-lg border border-slate-800 bg-slate-900/40 p-2"
      />
      <ul className="flex flex-wrap gap-x-5 gap-y-2 text-xs">
        {series.map((one) => {
          // The line's own last value when nothing is hovered: the total return of the run.
          const value = hovered[one.id] ?? one.points[one.points.length - 1]?.value
          return (
            <li key={one.id} className="flex items-center gap-2">
              {/* The swatch carries identity; the text stays in ink, never in the series colour. */}
              <span
                aria-hidden
                className="h-0.5 w-4 shrink-0 rounded-full"
                style={{ backgroundColor: one.color }}
              />
              <span className="text-slate-300">{one.label}</span>
              <span className="font-medium text-slate-100 tabular-nums">
                {value === undefined ? '—' : formatPercent(value)}
              </span>
            </li>
          )
        })}
      </ul>
    </div>
  )
}
