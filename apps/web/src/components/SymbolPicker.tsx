import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import type { BrokerSymbol, Instrument } from '../api/types'
import { MAX_SYMBOLS, measuredSpread, neverCollected } from '../basket/settings'
import { inputClass, useListboxKeys, useSymbolResults } from './symbolSearch'
import { SnapshotFooter, SymbolOptions } from './SymbolOptions'

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
 *
 * ## Any market the broker offers, not only the catalogued ones
 *
 * Above the grid, a search over the broker's whole list (his ask, 24/09: "even when I collect
 * several assets, the same four always show"). A catalogued result ticks its row in the grid;
 * one that was never collected is still chosen — finding it is the point — and shows below the
 * grid as a chip that says so, with the way to the collect screen. The launch screens refuse
 * those before the click: until the collector has run, nobody knows the market's tick or contract.
 *
 * ⚠️ The grid stays because the catalogue is small today and one click beats typing. With
 * hundreds of instruments it stops being a grid anyone can scan (specs/backlog.md).
 */
export function SymbolPicker(props: {
  instruments: Instrument[] | undefined
  chosen: readonly string[]
  onToggle: (symbol: string) => void
}): React.JSX.Element {
  const { instruments, chosen, onToggle } = props
  const full = chosen.length >= MAX_SYMBOLS
  const outside = neverCollected(chosen, instruments)

  return (
    <fieldset className="space-y-2">
      <legend className="text-sm text-slate-300">
        Markets{chosen.length > 0 && <span className="text-slate-500"> — {chosen.length} chosen</span>}
      </legend>

      <MarketSearch chosen={chosen} full={full} onToggle={onToggle} />

      {instruments === undefined ? (
        <p className="text-sm text-slate-400">Loading the catalogue…</p>
      ) : instruments.length === 0 ? (
        <p className="text-sm text-slate-400">No instruments catalogued yet.</p>
      ) : (
        <CatalogueGrid instruments={instruments} chosen={chosen} full={full} onToggle={onToggle} />
      )}

      {outside.length > 0 && (
        <div className="space-y-1">
          <div className="flex flex-wrap gap-1">
            {outside.map((symbol) => (
              <button
                key={symbol}
                type="button"
                aria-label={`Remove ${symbol}, never collected`}
                className="flex items-center gap-1 rounded border border-amber-800 bg-amber-950/30 px-2 py-0.5 font-mono text-xs text-amber-200 hover:border-amber-600"
                onClick={() => {
                  onToggle(symbol)
                }}
              >
                {symbol}
                <span className="font-sans text-amber-400">never collected</span>
                <span aria-hidden="true" className="text-amber-500">
                  ×
                </span>
              </button>
            ))}
          </div>
          <p className="text-xs text-amber-300">
            No candles yet for {outside.join(', ')} —{' '}
            <Link to="/collect" className="underline hover:text-amber-200">
              collect {outside.length === 1 ? 'it' : 'them'} first
            </Link>
            . The instrument is written by the collector, which is the only thing that can read its
            tick and contract from the terminal.
          </p>
        </div>
      )}
    </fieldset>
  )
}

/**
 * The search box over the broker's list. Picking toggles and the box clears for the next ticker
 * — the collect screen's multi-picker, speaking symbols rather than `BrokerSymbol`, because the
 * forms these screens hold are lists of names.
 *
 * ⚠️ At the ceiling, adding is refused and removing is not — the same rule as the grid.
 */
function MarketSearch(props: {
  chosen: readonly string[]
  full: boolean
  onToggle: (symbol: string) => void
}): React.JSX.Element {
  const { chosen, full, onToggle } = props
  const [text, setText] = useState('')
  const [open, setOpen] = useState(false)
  const [refused, setRefused] = useState(false)
  const listId = useId()

  const { debounced, results, snapshot } = useSymbolResults(text)

  const pick = (found: BrokerSymbol): void => {
    if (full && !chosen.includes(found.symbol)) {
      setRefused(true)
      return
    }
    setRefused(false)
    setText('')
    setOpen(false)
    onToggle(found.symbol)
  }

  const { highlighted, setHighlighted, onKeyDown } = useListboxKeys({
    results,
    open,
    setOpen,
    onPick: pick,
  })

  return (
    <div className="relative flex flex-col gap-1 text-sm">
      <label className="flex flex-col gap-1 text-xs text-slate-400" htmlFor={`${listId}-input`}>
        Find any market the broker offers
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
          onPick={pick}
          badge={(found) =>
            chosen.includes(found.symbol) ? (
              <span className="shrink-0 rounded bg-sky-900/60 px-1 text-[10px] text-sky-200">
                chosen
              </span>
            ) : null
          }
        />
      )}

      {refused && (
        <p className="text-xs text-amber-300">
          ⚠️ Already at {MAX_SYMBOLS} markets — remove one to add another.
        </p>
      )}

      <SnapshotFooter snapshot={snapshot} />
    </div>
  )
}

function CatalogueGrid(props: {
  instruments: Instrument[]
  chosen: readonly string[]
  full: boolean
  onToggle: (symbol: string) => void
}): React.JSX.Element {
  const { instruments, chosen, full, onToggle } = props
  return (
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
  )
}
