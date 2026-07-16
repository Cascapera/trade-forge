import { AreaSeries, ColorType, createChart, type UTCTimestamp } from 'lightweight-charts'
import { useEffect, useRef } from 'react'

import type { EquityPoint } from '../api/types'

// The equity curve: one series over time, so no legend — the heading names it, and a single hue
// carries it. lightweight-charts brings its own crosshair and tooltip, which is the hover layer
// the dataviz method asks for on a line/area chart. Dark surface styled to match the app, not an
// automatic flip.
export function EquityCurve({ points }: { points: EquityPoint[] }): React.JSX.Element {
  const container = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const element = container.current
    if (!element) return

    const chart = createChart(element, {
      height: 260,
      autoSize: true,
      layout: {
        background: { type: ColorType.Solid, color: 'transparent' },
        textColor: '#94a3b8',
      },
      grid: { vertLines: { color: '#1e293b' }, horzLines: { color: '#1e293b' } },
      rightPriceScale: { borderColor: '#334155' },
      timeScale: { borderColor: '#334155', timeVisible: true },
    })
    const series = chart.addSeries(AreaSeries, {
      lineColor: '#38bdf8',
      topColor: 'rgba(56, 189, 248, 0.35)',
      bottomColor: 'rgba(56, 189, 248, 0.02)',
      lineWidth: 2,
    })
    series.setData(
      points.map((point) => ({
        time: (Date.parse(point.time) / 1000) as UTCTimestamp,
        value: Number(point.equity),
      })),
    )
    chart.timeScale().fitContent()

    return () => {
      chart.remove()
    }
  }, [points])

  return (
    <div
      ref={container}
      aria-label="equity curve"
      className="rounded-lg border border-slate-800 bg-slate-900/40 p-2"
    />
  )
}
