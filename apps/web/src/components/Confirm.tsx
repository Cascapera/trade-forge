/**
 * An inline confirmation, asked where the action was clicked: what the action reaches, what it
 * does to the signals under way, and a button that names the action (lesson 451) — never a
 * browser dialog, which freezes the screen and the count it would show. Its owner closes it on
 * the action's success, not on the click, so a failure leaves the question and its button there
 * to try again.
 */
export function Confirm(props: {
  /** What the action reaches, in a sentence. */
  reach: string
  /** What it does to the signals under way, in a sentence — the part to read before the click. */
  price: string
  yes: string
  keep: string
  busy: boolean
  onYes: () => void
  onKeep: () => void
}): React.JSX.Element {
  return (
    <div className="space-y-2 rounded border border-red-900 bg-slate-900 p-3 text-sm">
      <p className="text-slate-300">
        {props.reach} <strong className="text-amber-200">{props.price}</strong>
      </p>
      <div className="flex gap-2">
        <button
          type="button"
          disabled={props.busy}
          onClick={props.onYes}
          className="rounded bg-red-800 px-2 py-1 text-xs text-white disabled:bg-slate-700"
        >
          {props.yes}
        </button>
        <button
          type="button"
          onClick={props.onKeep}
          className="text-xs text-slate-500 hover:text-slate-300"
        >
          {props.keep}
        </button>
      </div>
    </div>
  )
}
