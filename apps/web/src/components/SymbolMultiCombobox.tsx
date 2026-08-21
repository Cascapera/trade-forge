import { useId, useState } from 'react'

import type { BrokerSymbol } from '../api/types'
import { inputClass, useListboxKeys, useSymbolResults } from './symbolSearch'
import { SnapshotFooter, SymbolOptions } from './SymbolOptions'

/**
 * Search the broker's catalogue and pick **several** symbols for one batch.
 *
 * The sibling of `SymbolCombobox`, and the difference is what a pick means. There, picking
 * replaces a value and the input keeps it. Here picking **toggles** membership and the input
 * clears, because it is a search box rather than a field — leaving `eur` in it would make every
 * symbol after the first require a manual delete before anything else could be found.
 *
 * ## The ceiling blocks adding, never removing
 *
 * ⚠️ A full list still lets a chosen symbol be clicked off in the results. Refusing every click
 * while full would trap somebody who filled the list and then wanted to swap one out — with the
 * only escape being the chips, which is not where they are looking when the list is open.
 *
 * ## Chips carry the whole symbol, not its name
 *
 * The caller needs `asset_class_from_path` per chosen symbol to know which ones it must ask a
 * question about before sending. Keeping `BrokerSymbol` here means the screen never has to look
 * a chosen symbol back up in a result list that has since been replaced by another search.
 */
export function SymbolMultiCombobox(props: {
  chosen: readonly BrokerSymbol[]
  onToggle: (found: BrokerSymbol) => void
  max: number
}): React.JSX.Element {
  const { chosen, onToggle, max } = props
  const [text, setText] = useState('')
  const [open, setOpen] = useState(false)
  const [refused, setRefused] = useState(false)
  const listId = useId()

  const { debounced, results, snapshot } = useSymbolResults(text)
  const names = new Set(chosen.map((found) => found.symbol))
  const full = chosen.length >= max

  const toggle = (found: BrokerSymbol): void => {
    // Removing is always allowed; only adding is capped. `has` is what tells the two apart,
    // and getting it backwards would make a full list unrecoverable without a reload.
    if (full && !names.has(found.symbol)) {
      setRefused(true)
      return
    }
    setRefused(false)
    setText('')
    setOpen(false)
    onToggle(found)
  }

  const { highlighted, setHighlighted, onKeyDown } = useListboxKeys({
    results,
    open,
    setOpen,
    onPick: toggle,
  })

  return (
    <div className="relative flex flex-col gap-2 text-sm">
      <label className="flex flex-col gap-1" htmlFor={`${listId}-input`}>
        Symbols
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
          }}
          onFocus={() => {
            setOpen(true)
          }}
          onKeyDown={onKeyDown}
        />
      </label>

      {open && (
        <SymbolOptions
          id={listId}
          results={results}
          highlighted={highlighted}
          debounced={debounced}
          snapshot={snapshot}
          onPick={toggle}
          badge={(found) =>
            names.has(found.symbol) ? (
              <span className="shrink-0 rounded bg-sky-900/60 px-1 text-[10px] text-sky-200">
                chosen
              </span>
            ) : null
          }
        />
      )}

      <div className="flex flex-wrap items-center gap-1">
        {chosen.length === 0 ? (
          <span className="text-xs text-slate-500">No symbols chosen yet.</span>
        ) : (
          chosen.map((found) => (
            <button
              key={found.symbol}
              type="button"
              // ⚠️ Named for its own symbol. Twenty buttons all called "Remove" would be twenty
              // controls a screen reader cannot tell apart — and a defect, not test friction.
              aria-label={`Remove ${found.symbol}`}
              className="flex items-center gap-1 rounded bg-slate-800 px-2 py-0.5 font-mono text-xs text-slate-200 hover:bg-slate-700"
              onClick={() => {
                toggle(found)
              }}
            >
              {found.symbol}
              <span aria-hidden="true" className="text-slate-500">
                ×
              </span>
            </button>
          ))
        )}
        <span className="ml-auto text-xs text-slate-500">
          {chosen.length} of {max}
        </span>
      </div>

      {refused && (
        <p className="text-xs text-amber-300">
          ⚠️ Choose at most {max} symbols in one batch — remove one to add another.
        </p>
      )}

      <SnapshotFooter snapshot={snapshot} />
    </div>
  )
}
