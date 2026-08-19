import { useEffect, useId, useState } from 'react'

import { useSymbolSearch, useSyncSymbols } from '../api/hooks'
import type { BrokerSymbol } from '../api/types'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * How long typing has to pause before the catalogue is asked. The query itself is a prefix
 * match on a table of tens to thousands of rows and costs microseconds — this is not about
 * load. It is about not putting a request in flight for `e`, `eu` and `eur` when the only
 * answer anybody wanted was the third.
 */
const DEBOUNCE_MS = 150

/**
 * The symbol field: type a prefix, pick from what the broker actually offers.
 *
 * ## Why this is not a `<select>`
 *
 * It used to be, and the list was `GET /instruments` — the symbols somebody had already
 * collected through the collector's CLI. That is not "the assets you can test", it is "the
 * assets you already bothered to fetch", and the two diverge exactly when the question is which
 * asset to test next. Measured on this project's broker on 19/08/2026: 1 catalogued instrument
 * against 84 the account can see.
 *
 * ## Two things the list has to say that a plain option cannot
 *
 * * **Catalogued or not.** 83 of those 84 have no candles on disk, so a backtest on them fails.
 *   They are still offered — that is the point of the feature — but marked, because a screen
 *   that offers 84 identical choices and errors on 83 of them is teaching by clicking.
 * * **Where the list came from.** An empty result has two meanings: nothing starts with those
 *   letters, or nobody has ever synced this broker. Only the second one is fixed by the sync
 *   button, so only the second one shows it.
 */
export function SymbolCombobox(props: {
  value: string
  onChange: (symbol: string, chosen: BrokerSymbol | undefined) => void
}): React.JSX.Element {
  const { value, onChange } = props
  const [text, setText] = useState(value)
  const [open, setOpen] = useState(false)
  const [highlighted, setHighlighted] = useState(0)
  const listId = useId()

  // The query runs on the debounced text, the input shows the immediate text. Binding the input
  // to the debounced value instead would make typing feel like it was fighting back.
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(text)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [text])

  // ⚠️ Follows `value` when the form is loaded from elsewhere — opening a saved strategy, say.
  // Without this the input keeps whatever was typed and the form quietly disagrees with itself.
  //
  // Adjusted *during render* rather than in an effect, which is React's own recommendation for
  // "some state derives from a prop": an effect would render once with the stale text, commit
  // it, and then render again — a visible flash of the previous symbol every time a form loads.
  const [lastValue, setLastValue] = useState(value)
  if (value !== lastValue) {
    setLastValue(value)
    setText(value)
  }

  const search = useSymbolSearch(debounced)
  const sync = useSyncSymbols()
  const results = search.data?.symbols ?? []
  const snapshot = search.data?.snapshot ?? null

  const choose = (found: BrokerSymbol): void => {
    setText(found.symbol)
    setOpen(false)
    onChange(found.symbol, found)
  }

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      const step = event.key === 'ArrowDown' ? 1 : -1
      setHighlighted((current) => {
        if (results.length === 0) return 0
        return (current + step + results.length) % results.length
      })
      return
    }
    if (event.key === 'Enter') {
      const found = results[highlighted]
      if (open && found !== undefined) {
        event.preventDefault()
        choose(found)
      }
      return
    }
    if (event.key === 'Escape') {
      setOpen(false)
    }
  }

  return (
    <div className="relative flex flex-col gap-1 text-sm">
      <label className="flex flex-col gap-1" htmlFor={`${listId}-input`}>
        Symbol
        <input
          id={`${listId}-input`}
          role="combobox"
          aria-expanded={open}
          aria-controls={listId}
          aria-autocomplete="list"
          autoComplete="off"
          className={inputClass}
          placeholder="type a ticker…"
          value={text}
          onChange={(event) => {
            setText(event.target.value)
            setHighlighted(0)
            setOpen(true)
            // The form's symbol follows the text, so a half-typed ticker cannot leave the
            // previous instrument's costs behind attached to a symbol nobody chose.
            onChange(event.target.value, undefined)
          }}
          onFocus={() => {
            setOpen(true)
          }}
          onKeyDown={onKeyDown}
        />
      </label>

      {open && (
        <ul
          id={listId}
          role="listbox"
          aria-label="broker symbols"
          className="absolute top-full z-10 mt-1 max-h-64 w-full overflow-y-auto rounded border border-slate-700 bg-slate-900 shadow-lg"
        >
          {results.map((found, index) => (
            <li key={found.symbol}>
              <button
                type="button"
                role="option"
                aria-selected={index === highlighted}
                className={`flex w-full items-center justify-between gap-2 px-2 py-1 text-left ${
                  index === highlighted ? 'bg-slate-700' : ''
                }`}
                // `onMouseDown` and not `onClick`: the input's blur fires first otherwise and
                // closes the list out from under the pointer.
                onMouseDown={(event) => {
                  event.preventDefault()
                  choose(found)
                }}
              >
                <span className="font-mono text-slate-100">{found.symbol}</span>
                <span className="truncate text-xs text-slate-400">{found.description}</span>
                {/* Marked when it is *not* runnable, rather than badging the 1 that is. The
                    exception is what a reader needs to notice. */}
                {!found.catalogued && (
                  <span className="shrink-0 rounded bg-amber-900/60 px-1 text-[10px] text-amber-200">
                    no data
                  </span>
                )}
              </button>
            </li>
          ))}

          {results.length === 0 && (
            <li className="px-2 py-2 text-xs text-slate-400">
              {snapshot === null
                ? 'no broker catalogue yet — sync your terminal to see its symbols'
                : `no symbol starts with “${debounced}”`}
            </li>
          )}
        </ul>
      )}

      <div className="flex items-center justify-between gap-2 text-xs text-slate-500">
        <span>
          {snapshot === null
            ? 'never synced'
            : `${snapshot.server ?? 'unnamed server'} · ${new Date(snapshot.synced_at).toLocaleString()}`}
        </span>
        <button
          type="button"
          className="rounded border border-slate-700 px-2 py-0.5 hover:border-sky-500"
          disabled={sync.isPending}
          onClick={() => {
            sync.mutate()
          }}
        >
          {sync.isPending ? 'syncing…' : 'sync from MT5'}
        </button>
      </div>
    </div>
  )
}
