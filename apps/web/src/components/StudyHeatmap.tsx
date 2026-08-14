import type { StudyOut } from '../api/types'
import { axisName, fillFor, layoutOf } from '../backtest/study'
import { percent } from '../format'

/**
 * A study's grid, drawn as cells coloured by what each combination returned.
 *
 * ⚠️ **This picture is here to be read for its shape, not for its brightest cell.** A broad
 * region of gain says the method tolerates being slightly wrong about its parameters, which is
 * the only kind of result that survives a market moving on. One bright cell among losses says
 * the search found the luckiest arrangement of the same noise — and the number in that cell is
 * the least trustworthy on the screen, precisely because it is the largest.
 *
 * Built as a table rather than as SVG, and that is the accessibility decision: a heatmap **is**
 * a table of numbers with a colour on each, so a screen reader gets rows, columns and headers
 * for free, and every cell carries its return as text. Colour is never the only encoding.
 */
export function StudyHeatmap({ study }: { study: StudyOut }): React.JSX.Element {
  const layout = layoutOf(study)
  if (layout.cells.length === 0) {
    return <p className="text-sm text-slate-400">This study has no points to draw.</p>
  }

  // A point whose value is not on its axis cannot be placed. It cannot arrive from this API —
  // the server derives every point's values from the grid it stores — but dropping it silently
  // would leave a hole in the picture with nothing to say a cell is missing.
  const placed = layout.cells.filter((cell) => cell.row >= 0 && cell.column >= 0)
  const dropped = layout.cells.length - placed.length

  const columns = layout.columns?.values ?? [null]
  const at = (row: number, column: number) =>
    placed.find((cell) => cell.row === row && cell.column === column)

  return (
    <div className="space-y-3">
      <div className="overflow-x-auto">
        <table className="border-separate border-spacing-1 text-sm">
          <caption className="caption-top pb-2 text-left text-xs text-slate-400">
            Return of every combination, in-sample. Read the shape, not the best cell.
          </caption>
          <thead>
            <tr>
              <th scope="col" className="px-2 text-right text-xs font-normal text-slate-400">
                {axisName(layout.rows.path)}
                {layout.columns !== null && ` \\ ${axisName(layout.columns.path)}`}
              </th>
              {columns.map((value, column) => (
                <th
                  key={column}
                  scope="col"
                  className="px-2 text-center text-xs font-normal text-slate-400 tabular-nums"
                >
                  {label(value)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {layout.rows.values.map((rowValue, row) => (
              <tr key={row}>
                <th
                  scope="row"
                  className="px-2 text-right text-xs font-normal text-slate-400 tabular-nums"
                >
                  {String(rowValue)}
                </th>
                {columns.map((_, column) => {
                  const cell = at(row, column)
                  return (
                    <td
                      key={column}
                      // A 2px gap between fills is the border-spacing above; the ring keeps a
                      // cell's edge legible where two strong ones meet.
                      className="min-w-16 rounded px-2 py-2 text-center text-xs tabular-nums ring-1 ring-slate-950"
                      style={{ backgroundColor: cell?.fill ?? 'transparent' }}
                      title={cell === undefined ? undefined : cell.label}
                    >
                      {/* The number is on every cell on purpose. Colour alone is not an
                          encoding a colourblind reader or a printed page can use, and the
                          value is what someone comparing two neighbours actually wants. */}
                      <span className="text-slate-100">
                        {cell?.ret === undefined || cell.ret === null
                          ? '—'
                          : percent(String(cell.ret))}
                      </span>
                    </td>
                  )
                })}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <Legend extent={layout.extent} />

      {layout.undrawn.length > 0 && (
        <p className="text-xs text-amber-300/80">
          Also varying {layout.undrawn.map(axisName).join(', ')} — a grid of more than two
          parameters is not a rectangle, so those axes are folded into the table below rather than
          drawn here.
        </p>
      )}
      {dropped > 0 && (
        <p className="text-xs text-amber-300/80">
          {dropped} point{dropped === 1 ? '' : 's'} could not be placed on these axes.
        </p>
      )}
    </div>
  )
}

/**
 * An axis value as a caption.
 *
 * Written out rather than `String(value)` because the values come off the wire as `unknown`, and
 * `String` on an object yields `[object Object]` — a heading that says nothing while looking
 * like a heading. Nothing in this API sends one; the axis is captioned honestly if it ever does.
 */
function label(value: unknown): string {
  if (value === null || value === undefined) return ''
  if (typeof value === 'object') return JSON.stringify(value)
  // eslint-disable-next-line @typescript-eslint/no-base-to-string -- narrowed to a primitive
  return String(value)
}

/**
 * What the colours mean, in the study's own numbers.
 *
 * Both arms are scaled against the same extent, so the legend says the extent once and the two
 * poles are its negative and its positive. A legend per arm would imply two scales.
 */
function Legend({ extent }: { extent: number }): React.JSX.Element {
  return (
    <div className="flex items-center gap-2 text-xs text-slate-400">
      <span>{extent === 0 ? '—' : percent(String(-extent))}</span>
      <span className="flex h-3 w-40 overflow-hidden rounded" aria-hidden>
        {[-1, -0.6, -0.3, 0, 0.3, 0.6, 1].map((stop) => (
          <span
            key={stop}
            className="flex-1"
            style={{
              backgroundColor: fillFor(stop * (extent || 1), extent || 1),
            }}
          />
        ))}
      </span>
      <span>{extent === 0 ? '—' : percent(String(extent))}</span>
      <span className="sr-only">
        Blue is a gain and red is a loss; the strength of either is its size against the largest
        absolute return in this study. Every cell also carries its number.
      </span>
    </div>
  )
}
