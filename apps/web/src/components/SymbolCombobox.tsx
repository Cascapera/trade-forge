import { useId, useState } from 'react'

import type { BrokerSymbol } from '../api/types'
import { inputClass, useListboxKeys, useSymbolResults } from './symbolSearch'
import { SnapshotFooter, SymbolOptions } from './SymbolOptions'

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
 *
 * ## One symbol
 *
 * Picking here **replaces**. The collect screen needs picking to **toggle**, and that is
 * `SymbolMultiCombobox` — a separate component rather than a mode on this one, because the two
 * differ in what the input means after a pick (it keeps the choice; the other clears to let the
 * next one be typed) and a single component would branch on that in six places.
 */
export function SymbolCombobox(props: {
  value: string
  onChange: (symbol: string, chosen: BrokerSymbol | undefined) => void
}): React.JSX.Element {
  const { value, onChange } = props
  const [text, setText] = useState(value)
  const [open, setOpen] = useState(false)
  const listId = useId()

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

  const { debounced, results, snapshot } = useSymbolResults(text)

  const choose = (found: BrokerSymbol): void => {
    setText(found.symbol)
    setOpen(false)
    onChange(found.symbol, found)
  }

  const { highlighted, setHighlighted, onKeyDown } = useListboxKeys({
    results,
    open,
    setOpen,
    onPick: choose,
  })

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
        <SymbolOptions
          id={listId}
          results={results}
          highlighted={highlighted}
          debounced={debounced}
          snapshot={snapshot}
          onPick={choose}
        />
      )}

      <SnapshotFooter snapshot={snapshot} />
    </div>
  )
}
