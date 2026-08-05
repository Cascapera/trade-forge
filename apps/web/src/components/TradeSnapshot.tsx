// The entry chart: the bars the strategy was looking at, with the levels drawn over them.
//
// All the arithmetic lives in `../trades/snapshot`; this file turns those shapes into SVG and
// nothing else. Inline SVG rather than the charting library the equity curve uses, for two
// reasons: the library has no rectangle primitive (a zone would need a custom one) and a screen
// shows many of these, where one canvas instance each is real weight.

import type { Snapshot } from '../api/types'
import { money } from '../format'
import {
  VIEW,
  candles,
  curveRuns,
  makeScale,
  levelSegments,
  markers,
  priceBand,
  priceLabels,
  regions,
  toNumber,
} from '../trades/snapshot'

const HHMM = (iso: string): string => `${iso.slice(8, 10)}/${iso.slice(5, 7)} ${iso.slice(11, 16)}`

// Chosen against this app's own `bg-slate-950`, and checked rather than eyeballed: the three
// level hues clear the CVD separation floor and every colour clears 3:1 against that surface.
// Up and down candles sit in the 6–8 CVD band, which is legal only with a second encoding —
// hence hollow for up and filled for down, the classic convention, doing real work here.
const CANDLE_UP = '#1FA97E'
const CANDLE_DOWN = '#D96047'
const LEVEL = { entry: '#5F8AD2', stop: '#CE5F94', average: '#BC8620' } as const
const DASH = { entry: '0', stop: '4 3', average: '5 3' } as const
// The candle body's fill for an up bar: the page behind it, so the outline reads as hollow.
const SURFACE = '#020617'
// The broken structure gets its own hue, distinct from the three trade levels: it is not a
// price the order relates to, it is the event that justified the order existing.
const LEVEL_STRUCTURE = '#C9A227'

interface Props {
  snapshot: Snapshot
  entryPrice: string
  stopLoss: string | null
  /** The trade's recorded levels. Only `average` is read, and only when there is no curve. */
  context: Record<string, string | null>
}

export function TradeSnapshot({
  snapshot,
  entryPrice,
  stopLoss,
  context,
}: Props): React.JSX.Element {
  const entry = toNumber(entryPrice)
  const stop = stopLoss === null ? null : toNumber(stopLoss)
  // The scalar the rule was judged against. It is drawn only when the run recorded no curve —
  // with one, the curve *is* the average, and a horizontal mark at one of its values would
  // read as a level the average never held.
  const recorded = context.average
  const average = recorded === undefined || recorded === null ? null : toNumber(recorded)

  const band = priceBand(snapshot, stop === null ? [entry] : [entry, stop])
  const scale = makeScale(snapshot.bars.length, band)
  const shapes = candles(snapshot, scale)
  const runs = curveRuns(snapshot, scale)
  const zones = regions(snapshot, scale)
  const segments = levelSegments(snapshot, scale)
  const labels = priceLabels(
    { entry, stop, average, hasCurve: snapshot.series.length > 0 },
    scale,
    (value) => money(String(value)),
  )
  const ticks = markers(snapshot)
  const first = snapshot.bars[0]
  const last = snapshot.bars[snapshot.bars.length - 1]

  return (
    <svg
      viewBox={`0 0 ${String(VIEW.width)} ${String(VIEW.height)}`}
      className="w-full h-auto overflow-visible"
      role="img"
      aria-label={`Barras em volta da entrada de ${HHMM(snapshot.filled_at)}`}
    >
      {/* Zones first — they sit behind price. */}
      {zones.map((zone, index) => (
        <g key={`zone-${String(index)}`}>
          <rect
            x={zone.x}
            y={zone.y}
            width={zone.width}
            height={zone.height}
            fill={LEVEL.entry}
            opacity="0.09"
          />
          <rect
            x={zone.x}
            y={zone.y}
            width={zone.width}
            height={zone.height}
            fill="none"
            stroke={LEVEL.entry}
            strokeWidth="1"
            strokeDasharray="3 3"
            opacity="0.55"
          />
          {zone.clipped && (
            <text x="3" y={zone.y - 4} fontSize="9" fill={LEVEL.entry} opacity="0.85">
              zona começa antes ←
            </text>
          )}
        </g>
      ))}

      {/* The decision bar's column, so the eye finds it before reading a label. */}
      {ticks
        .filter((tick) => tick.kind === 'decision')
        .map((tick) => (
          <rect
            key="decision-band"
            x={scale.x(tick.index) - scale.barWidth / 2 - 1.5}
            y={VIEW.padTop}
            width={scale.barWidth + 3}
            height={scale.plotHeight}
            fill="currentColor"
            opacity="0.07"
          />
        ))}

      {/* Candles: hollow up, filled down — direction is carried by shape, not colour alone. */}
      {shapes.map((shape) => {
        const stroke = shape.up ? CANDLE_UP : CANDLE_DOWN
        return (
          <g key={shape.time}>
            <line
              x1={shape.x}
              y1={shape.wickTop}
              x2={shape.x}
              y2={shape.wickBottom}
              stroke={stroke}
              strokeWidth="1"
            />
            <rect
              x={shape.x - scale.barWidth / 2}
              y={shape.bodyTop}
              width={scale.barWidth}
              height={shape.bodyHeight}
              fill={shape.up ? SURFACE : stroke}
              stroke={stroke}
              strokeWidth="1"
            />
          </g>
        )
      })}

      {/* The structure that broke: a segment from the bar that set the level to the bar that
          crossed it. Not extended — a broken level stops being structure the moment it gives
          way, and drawing it onward would show one still standing. */}
      {segments.map((segment, index) => (
        <g key={`level-${segment.label}-${String(index)}`}>
          <line
            x1={segment.x1}
            y1={segment.y}
            x2={segment.x2}
            y2={segment.y}
            stroke={LEVEL_STRUCTURE}
            strokeWidth="2"
            strokeDasharray="7 4"
            opacity="0.95"
          />
          <text
            x={Math.min(segment.x1 + 4, scale.plotRight - 30)}
            y={segment.y - 5}
            fontSize="9.5"
            fill={LEVEL_STRUCTURE}
            opacity="0.95"
          >
            {segment.label.toUpperCase()}
            {segment.clamped ? ' ←' : ''} {money(String(segment.price))}
          </text>
        </g>
      ))}

      {/* The indicator curves, joined to the bars on time. A break is left as a break. */}
      {runs.map((run, index) => (
        <polyline
          key={`curve-${run.label}-${String(index)}`}
          points={run.points}
          fill="none"
          stroke={LEVEL.average}
          strokeWidth="1.75"
          strokeLinejoin="round"
          opacity="0.95"
        />
      ))}

      {/* Levels. The line stays at the true price; only the text was pushed apart. */}
      {labels.map((label) => (
        <g key={label.kind}>
          <line
            x1="0"
            y1={label.y}
            x2={scale.plotRight}
            y2={label.y}
            stroke={LEVEL[label.kind]}
            strokeWidth="1.5"
            strokeDasharray={DASH[label.kind]}
            opacity="0.9"
          />
          {Math.abs(label.labelY - label.y) > 1.5 && (
            <path
              d={`M${String(scale.plotRight)},${String(label.y)} L${String(scale.plotRight + 4)},${String(label.labelY)}`}
              stroke={LEVEL[label.kind]}
              strokeWidth="1"
              fill="none"
              opacity="0.55"
            />
          )}
          <text
            x={scale.plotRight + 6}
            y={label.labelY + 3.4}
            fontSize="10"
            fill={LEVEL[label.kind]}
          >
            {label.text}
          </text>
        </g>
      ))}

      {/* Decision and fill, marked below the axis. */}
      {ticks.map((tick) => (
        <g key={tick.kind}>
          <path
            d={`M${String(scale.x(tick.index) - 4)},${String(VIEW.height - VIEW.padBottom + 5)} L${String(scale.x(tick.index))},${String(VIEW.height - VIEW.padBottom)} L${String(scale.x(tick.index) + 4)},${String(VIEW.height - VIEW.padBottom + 5)}Z`}
            fill="currentColor"
            opacity="0.75"
          />
          <text
            x={scale.x(tick.index)}
            y={VIEW.height - VIEW.padBottom + 17}
            fontSize="9.5"
            textAnchor="middle"
            fill="currentColor"
            opacity="0.75"
          >
            {tick.kind === 'decision' ? 'decisão' : 'fill'}
          </text>
        </g>
      ))}

      {first !== undefined && (
        <text x="0" y={VIEW.height - VIEW.padBottom + 17} fontSize="9.5" fill="currentColor" opacity="0.5">
          {HHMM(first.time)}
        </text>
      )}
      {last !== undefined && (
        <text
          x={scale.plotRight}
          y={VIEW.height - VIEW.padBottom + 17}
          fontSize="9.5"
          textAnchor="end"
          fill="currentColor"
          opacity="0.5"
        >
          {HHMM(last.time)}
        </text>
      )}
    </svg>
  )
}
