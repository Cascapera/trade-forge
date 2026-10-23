import { type NumericParam, startingPoint, stepped } from '../strategy/stepping'

/**
 * A number field with arrows that know the parameter they are moving.
 *
 * The value is carried as **text**, not as a number, because an empty box is a legal state that
 * a number cannot hold: `undefined` and `0` are different answers on a nullable parameter, and
 * collapsing them is how "no break-even" becomes "break-even at zero R".
 */
export function NumberStepper(props: {
  param: NumericParam
  value: string
  onChange: (next: string) => void
  label: string
  className?: string
}): React.JSX.Element {
  const { param, value, onChange, label, className } = props

  const pieces = value
    .split(',')
    .map((piece) => piece.trim())
    .filter((piece) => piece !== '')

  // An arrow moves *a* value, so it has nothing to do while the box holds a list — disabled,
  // rather than replacing the list with one number, which is an edit the reader did not ask for
  // to a field they can see the whole of.
  const only = pieces.length === 1 ? Number(pieces[0]) : null
  const from = only === null || Number.isNaN(only) ? null : only
  const move = (direction: 1 | -1): number | null =>
    pieces.length > 1 ? null : from === null ? startingPoint(param) : stepped(param, from, direction)
  const up = move(1)
  const down = move(-1)

  return (
    <span className={`inline-flex items-stretch ${className ?? ''}`}>
      {/* ⚠️ **Text, not `type="number"`, and the arrows are the reason it can be.** The native
          spinner obeys `min`, which has no spelling for an exclusive bound — that is how the
          builder let `breakeven_at_r` be stepped to exactly 0. These arrows come from `stepped`,
          which reads the bound as declared, so nothing is lost by dropping the browser's.
          What is gained is that a grid axis can be pasted in as `2, 3, 4, 5`: a number field
          silently swallows the commas and leaves an empty box. */}
      <input
        aria-label={label}
        type="text"
        inputMode="decimal"
        className="w-24 rounded-l border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none"
        value={value}
        onChange={(event) => {
          onChange(event.target.value)
        }}
      />
      <span className="flex flex-col">
        <button
          type="button"
          aria-label={`${label} up`}
          disabled={up === null}
          className="h-1/2 rounded-tr border border-l-0 border-slate-700 px-1 text-[9px] leading-none text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:text-slate-700"
          onClick={() => {
            if (up !== null) onChange(String(up))
          }}
        >
          ▲
        </button>
        <button
          type="button"
          aria-label={`${label} down`}
          disabled={down === null}
          className="h-1/2 rounded-br border border-l-0 border-t-0 border-slate-700 px-1 text-[9px] leading-none text-slate-300 hover:bg-slate-800 disabled:cursor-not-allowed disabled:text-slate-700"
          onClick={() => {
            if (down !== null) onChange(String(down))
          }}
        >
          ▼
        </button>
      </span>
    </span>
  )
}
