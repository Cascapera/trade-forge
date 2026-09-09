import { useState } from 'react'

import type { AxisOption } from '../study/axes'
import { OFF, parseValues, textOf, type AxisValue } from '../study/settings'

import type { NumericParam } from '../strategy/stepping'

import { NumberStepper } from './NumberStepper'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Rebuild the axis line from the values it should now hold.
 *
 * ⚠️ **The comma-separated string stays the one representation, and the controls are a view over
 * it.** Holding a typed list beside it would create a second place for an axis to be true — and
 * the free-text field below has to keep existing anyway, for a DSL strategy whose axes this
 * dropdown cannot describe. One shape means `parseValues`, `axesOf`, `combinationCount` and
 * `toStudyRequest` are untouched by any of this.
 */
function lineOf(values: readonly AxisValue[]): string {
  // Through `textOf`, so a switched-off point is written as the word rather than as the empty
  // string `join` would give it — which `parseValues` drops as a half-typed comma.
  return values.map(textOf).join(', ')
}

/** Numeric axes read in ascending order, whatever order they were clicked in.
 *
 *  Not tidiness: the grid's axis order is the order a heatmap lays its cells out and the order
 *  `grid.coordinates` breaks ties by, so an axis of `20, 5, 9` draws a chart whose rows climb,
 *  fall and climb again over a parameter that only goes one way.
 *
 *  ⚠️ **Off comes first**, ahead of every number. It is the control the rest of the axis is being
 *  compared against — "does this rule earn its keep?" reads down the chart from the run without
 *  it — and it has no place on a number line to be sorted onto. */
function sorted(values: readonly AxisValue[]): AxisValue[] {
  return [...values].sort((left, right) => {
    if (left === null || right === null) return Number(left !== null) - Number(right !== null)
    return typeof left === 'number' && typeof right === 'number' ? left - right : 0
  })
}

function Choices(props: {
  label: string
  options: readonly AxisValue[]
  chosen: readonly AxisValue[]
  onChange: (next: AxisValue[]) => void
}): React.JSX.Element {
  const { label, options, chosen, onChange } = props
  return (
    <span className="flex flex-wrap items-center gap-3">
      {options.map((option) => {
        const on = chosen.includes(option)
        return (
          <label key={textOf(option)} className="flex items-center gap-1 text-sm text-slate-300">
            <input
              type="checkbox"
              // Named with the axis in front of the value: two axes over `side` would otherwise
              // put two controls called "long" on one screen, which is a defect in the page and
              // not merely in a test's ability to find them.
              aria-label={`${label} ${textOf(option)}`}
              checked={on}
              onChange={() => {
                // Rebuilt from the declared order rather than appended in click order, so the
                // axis reads `long, short` however the boxes were ticked.
                onChange(options.filter((each) => (each === option ? !on : chosen.includes(each))))
              }}
            />
            {textOf(option)}
          </label>
        )
      })}
    </span>
  )
}

/**
 * The values one grid axis will be searched over, in the shape the parameter deserves.
 *
 * Three controls behind one component, chosen by what the JSON Schema says the parameter is —
 * so a parameter added in Python arrives here already knowing whether it is a set of names, a
 * flag, or a number with bounds. Nothing below lists a parameter, a value or an option.
 */
export function AxisValues(props: {
  option: AxisOption | undefined
  raw: string
  onChange: (raw: string) => void
  label: string
}): React.JSX.Element {
  const { option, raw, onChange, label } = props
  const [draft, setDraft] = useState('')
  const chosen = parseValues(raw)

  // No spec for this path: a strategy built from indicators and conditions varies
  // `indicators.0.params.period`, which no setup describes. Typed values still reach the server,
  // which is the authority on whether the path leads anywhere.
  if (option === undefined) {
    return (
      <input
        className={`${inputClass} min-w-48 flex-1`}
        placeholder="5, 9, 20"
        aria-label={label}
        value={raw}
        onChange={(event) => {
          onChange(event.target.value)
        }}
      />
    )
  }

  const { param } = option

  if (param.kind === 'enum') {
    return (
      <Choices
        label={label}
        // ⚠️ `off` joins the schema's own options when the parameter is nullable, and it is the
        // point of the axis rather than a nicety: varying `htf` over `off, H4` is the one grid
        // that answers whether the region above earns its keep, and without this box the
        // comparison is two studies whose numbers are not on the same chart.
        options={param.nullable ? [null, ...param.options] : param.options}
        chosen={chosen}
        onChange={(next) => {
          onChange(lineOf(next))
        }}
      />
    )
  }

  if (param.kind === 'boolean') {
    return (
      <Choices
        label={label}
        options={[true, false]}
        chosen={chosen}
        onChange={(next) => {
          onChange(lineOf(next))
        }}
      />
    )
  }

  const numeric: NumericParam = param
  // The same parser the request is built with, so `2, 3, 4` pasted in becomes three values by
  // exactly the rule that will later read them — a second splitter here would be a second answer
  // to what a comma means.
  const drafted = parseValues(draft).filter((each) => typeof each === 'number')
  // ⚠️ A value already on the axis cannot be added again. Two identical points are two identical
  // backtests, which the server refuses — and a form exists to prevent what a server has to.
  const pending = drafted.filter((each) => !chosen.includes(each))
  const suggestions = parseValues(option.example).filter((each) => !chosen.includes(each))

  function add(...next: readonly AxisValue[]): void {
    onChange(lineOf(sorted([...chosen, ...next])))
  }

  return (
    <span className="flex flex-wrap items-center gap-2">
      <NumberStepper
        param={numeric}
        value={draft}
        label={label}
        onChange={(next) => {
          setDraft(next)
        }}
      />
      <button
        type="button"
        disabled={pending.length === 0}
        aria-label={`${label} add`}
        className="rounded border border-slate-700 px-2 py-1 text-xs text-sky-400 hover:bg-slate-800 disabled:cursor-not-allowed disabled:text-slate-700"
        onClick={() => {
          add(...pending)
          setDraft('')
        }}
      >
        + add
      </button>

      {chosen.map((each) => (
        <span
          key={textOf(each)}
          className="inline-flex items-center gap-1 rounded bg-slate-800 px-2 py-1 text-xs text-slate-200"
        >
          {textOf(each)}
          <button
            type="button"
            aria-label={`${label} remove ${textOf(each)}`}
            className="text-slate-400 hover:text-red-400"
            onClick={() => {
              onChange(lineOf(chosen.filter((value) => value !== each)))
            }}
          >
            ×
          </button>
        </span>
      ))}

      {/* ⚠️ **Off, for a rule that can be switched off.** A nullable number — `breakeven_at_r`,
          `max_bos`, `long_average_period` — has one value the stepper cannot reach, and it is the
          most interesting point on the axis: the run without the rule, which everything else is
          being compared against. Offered as a toggle rather than as a suggestion because it is
          not a number somebody might have meant to type. */}
      {numeric.nullable && (
        <button
          type="button"
          aria-label={`${label} ${OFF}`}
          aria-pressed={chosen.includes(null)}
          className={
            chosen.includes(null)
              ? 'rounded bg-slate-800 px-2 py-1 text-xs text-slate-200'
              : 'rounded border border-dashed border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-sky-300'
          }
          onClick={() => {
            if (chosen.includes(null)) onChange(lineOf(chosen.filter((each) => each !== null)))
            else add(null)
          }}
        >
          {OFF}
        </button>
      )}

      {/* The generated example, one value at a time: it is built around the parameter's own
          default and clamped to its bounds, so every one of these is a value that will run. */}
      {suggestions.length > 0 && (
        <span className="flex items-center gap-1">
          {suggestions.map((each) => (
            <button
              key={String(each)}
              type="button"
              aria-label={`${label} suggest ${String(each)}`}
              className="rounded border border-dashed border-slate-700 px-2 py-1 text-xs text-slate-400 hover:text-sky-300"
              onClick={() => {
                add(each)
              }}
            >
              +{String(each)}
            </button>
          ))}
        </span>
      )}
    </span>
  )
}
