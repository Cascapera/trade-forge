import { inR } from '../format'

const BAR = 6
const GAP = 2
const HEIGHT = 28

/**
 * One run's R by year of entry as a row of small bars (01/10): up for a year that made R, down for
 * one that lost it, nothing for a year with no trade. What a ranked row of a long window shows in
 * place of a column per year — eleven numbers side by side read as a wall, the shape reads at once.
 *
 * ⚠️ **Scaled to the row's own largest year**, never across rows: the question a row answers is
 * "how steady was this run from year to year", and one run's outlier year would flatten every
 * other row on a shared scale. The exact R is each bar's tooltip and the image's label; one ink,
 * because the direction already says the sign.
 */
export function YearBars(props: {
  years: readonly number[]
  /** R per year of entry, as the run's `metrics.yearly_r` holds it; a year absent had no trade. */
  yearly: Record<string, string>
}): React.JSX.Element {
  const values = props.years.map((year) => {
    const r = props.yearly[String(year)]
    return { year, r: r === undefined ? null : Number(r), text: r ?? null }
  })
  const largest = Math.max(...values.map((one) => Math.abs(one.r ?? 0)), Number.MIN_VALUE)
  const middle = HEIGHT / 2
  const reach = middle - 1
  const width = props.years.length * (BAR + GAP) - GAP
  const label = values
    .map((one) => `${String(one.year)} ${one.text === null ? 'no trade' : inR(one.text)}`)
    .join(', ')
  return (
    <svg
      role="img"
      aria-label={`R by year of entry: ${label}`}
      width={width}
      height={HEIGHT}
      viewBox={`0 0 ${String(width)} ${String(HEIGHT)}`}
      className="inline-block align-middle"
    >
      <line x1={0} x2={width} y1={middle} y2={middle} className="stroke-slate-700" strokeWidth={1} />
      {values.map((one, index) => {
        const x = index * (BAR + GAP)
        const size = one.r === null ? 0 : Math.max(1, (Math.abs(one.r) / largest) * reach)
        return (
          <g key={one.year}>
            <title>{`${String(one.year)}: ${one.text === null ? 'no trade' : inR(one.text)}`}</title>
            {/* The hover target is the whole column, not the bar: a small year is still a year. */}
            <rect x={x} y={0} width={BAR} height={HEIGHT} fill="transparent" />
            {one.r !== null && (
              <rect
                x={x}
                y={one.r >= 0 ? middle - size : middle}
                width={BAR}
                height={size}
                rx={1}
                className="fill-slate-400"
              />
            )}
          </g>
        )
      })}
    </svg>
  )
}
