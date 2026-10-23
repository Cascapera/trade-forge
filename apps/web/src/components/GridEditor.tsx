import { AxisValues } from './AxisValues'
import { axesFor } from '../study/axes'
import type { Axis } from '../study/settings'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * The parameters to vary, and the values to try at each.
 *
 * Extracted from the study launcher when the catalogue needed the same editor. Two callers is
 * the whole argument: a second copy of seventy lines of controls is a second place for "what a
 * parameter may be" to be decided, and the day one of them learns a new control the other
 * quietly stops matching it.
 *
 * ⚠️ **Nothing here is a list of parameters.** The dropdown comes from `axesFor`, which reads
 * the setup's fields, bounds and defaults out of the generated JSON Schema — so a parameter
 * added in Python appears here with nothing changed anywhere. A strategy built from indicators
 * and conditions has no named setup and therefore no options; the path is typed instead, and
 * the server refuses one that leads nowhere.
 */
export function GridEditor(props: {
  /** The named setup the chosen strategy runs, or `null` — what decides the options. */
  setup: string | null
  axes: Axis[]
  onChange: (axes: Axis[]) => void
  /** What the legend says. The two callers ask the same question for different reasons: one is
   *  launching a search now, the other is saving one for later. */
  legend?: string
}): React.JSX.Element {
  const options = axesFor(props.setup)

  const setAxis = (at: number, patch: Partial<Axis>): void => {
    props.onChange(props.axes.map((axis, index) => (index === at ? { ...axis, ...patch } : axis)))
  }

  return (
    <fieldset className="space-y-2 rounded border border-slate-800 p-4">
      <legend className="px-1 text-sm text-slate-300">
        {props.legend ?? 'Parameters to vary'}
      </legend>
      {props.axes.map((axis, at) => {
        const chosen = options.find((option) => option.path === axis.path)
        return (
          <div key={at} className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              {/* A dropdown rather than a text field, and the options are **derived**: they come
                  from the JSON Schema the DSL generates, so a parameter added in Python appears
                  here with nothing changed. A typed path is still accepted for a DSL strategy,
                  whose axes this list cannot describe. */}
              {options.length > 0 ? (
                <select
                  className={`${inputClass} min-w-64 flex-1`}
                  aria-label={`Parameter ${String(at + 1)}`}
                  value={axis.path}
                  onChange={(event) => {
                    setAxis(at, { path: event.target.value, raw: '' })
                  }}
                >
                  <option value="">Choose a parameter…</option>
                  {options.map((option) => (
                    <option key={option.path} value={option.path}>
                      {option.label}
                    </option>
                  ))}
                </select>
              ) : (
                <input
                  className={`${inputClass} min-w-64 flex-1`}
                  placeholder="setup.params.period"
                  aria-label={`Parameter ${String(at + 1)} path`}
                  value={axis.path}
                  onChange={(event) => {
                    setAxis(at, { path: event.target.value })
                  }}
                />
              )}
              {/* The control the parameter deserves — checkboxes for a set of names, arrows for
                  a number with bounds — chosen from what the JSON Schema says it is. The values
                  still travel as the same comma-separated line underneath. */}
              <AxisValues
                option={chosen}
                raw={axis.raw}
                label={`Parameter ${String(at + 1)} values`}
                onChange={(raw) => {
                  setAxis(at, { raw })
                }}
              />
            </div>
            {/* The format *and* what is legal, both read from the parameter's own schema — so
                the sentence tightens on its own the day a bound does. */}
            {chosen !== undefined && <p className="text-xs text-slate-500">{chosen.hint}</p>}
          </div>
        )
      })}
      <button
        type="button"
        className="text-xs text-sky-400 hover:text-sky-300"
        onClick={() => {
          props.onChange([...props.axes, { path: '', raw: '' }])
        }}
      >
        Add a parameter
      </button>
    </fieldset>
  )
}
