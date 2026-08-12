import type { Instrument } from '../api/types'
import { MAX_SYMBOLS, measuredSpread } from '../basket/settings'

/**
 * The catalogue as a grid of checkboxes, each showing what that market will be charged.
 *
 * The cost is rendered *beside the tick*, not in a footnote, because on this screen the user is
 * not choosing a cost — the server charges each instrument its own measured spread — and the only
 * way that decision stays honest is if it is visible at the moment the market is picked.
 *
 * ⚠️ An unmeasured symbol says **"no spread measured"**, never "0 ticks". Zero is the claim that
 * an instrument is free to trade; the truth is that nobody has looked. Ticking it is allowed —
 * refusing would make the catalogue's gaps invisible — and the launch screen says which ones they
 * were before anything is enqueued.
 *
 * The rows keep the catalogue's order however the ticks come and go: a list that re-sorted the
 * chosen markets to the top would move a row out from under the cursor mid-click.
 */
export function SymbolPicker(props: {
  instruments: Instrument[] | undefined
  chosen: readonly string[]
  onToggle: (symbol: string) => void
}): React.JSX.Element {
  const { instruments, chosen, onToggle } = props
  const full = chosen.length >= MAX_SYMBOLS

  if (instruments === undefined) {
    return <p className="text-sm text-slate-400">Loading the catalogue…</p>
  }
  if (instruments.length === 0) {
    return <p className="text-sm text-slate-400">No instruments catalogued yet.</p>
  }

  return (
    <fieldset className="space-y-2">
      <legend className="text-sm text-slate-300">
        Markets{chosen.length > 0 && <span className="text-slate-500"> — {chosen.length} chosen</span>}
      </legend>
      <div className="grid gap-2 sm:grid-cols-2">
        {instruments.map((instrument) => {
          const spread = measuredSpread(instrument)
          const picked = chosen.includes(instrument.symbol)
          const blocked = !picked && full
          // The accessible name carries the cost too: a screen-reader user picking markets is
          // making the same decision by ear that a sighted one makes by reading the column.
          const cost = spread === null ? 'no spread measured' : `${spread} ticks`

          return (
            <label
              key={instrument.id}
              className={`flex items-center gap-3 rounded border px-3 py-2 text-sm ${
                picked
                  ? 'border-sky-700 bg-sky-950/30'
                  : 'border-slate-800 bg-slate-900/40 hover:border-slate-700'
              } ${blocked ? 'opacity-40' : ''}`}
            >
              <input
                type="checkbox"
                checked={picked}
                disabled={blocked}
                onChange={() => {
                  onToggle(instrument.symbol)
                }}
                aria-label={`${instrument.symbol}, ${cost}`}
                title={
                  blocked ? `Already at ${String(MAX_SYMBOLS)} markets — untick one first` : undefined
                }
                className="size-4 accent-sky-500 disabled:opacity-30"
              />
              <span className="font-medium">{instrument.symbol}</span>
              <span
                className={`ml-auto text-xs ${spread === null ? 'text-amber-300' : 'text-slate-400'}`}
              >
                {cost}
              </span>
            </label>
          )
        })}
      </div>
    </fieldset>
  )
}
