import { useId, useState } from 'react'
import { Link } from 'react-router-dom'

import type { BrokerSymbol, Instrument } from '../api/types'
import { neverCollected } from '../basket/settings'
import { MarketBrowser } from './MarketBrowser'
import { Pager } from './Pager'
import { clampOffset, pageOf } from './paging'
import { inputClass, useListboxKeys, useSymbolResults } from './symbolSearch'
import { SnapshotFooter, SymbolOptions } from './SymbolOptions'

/** Chosen markets shown at once under the picker; the rest a page at a time. */
export const CHIPS_PER_PAGE = 40

/**
 * The chosen markets, and the two ways to choose more.
 *
 * ## By market, in a dialog (04/10)
 *
 * His ask: the catalogue grows from 18 to hundreds, so the grid of checkboxes this used to be
 * stops being something anyone can scan. "Choose markets…" opens `MarketBrowser` — a tab per
 * market, 25 rows a page, a whole market in one click. Each row still shows what that market will
 * be charged beside its tick: the server charges each instrument its own measured spread, and the
 * decision only stays honest if the cost is visible at the moment the market is picked.
 *
 * ## One ticker, typed
 *
 * The search box over the broker's whole list stays (24/09) for the one market somebody already
 * knows the name of. A result that was never collected is still chosen — finding it is the point —
 * and shows as an amber chip with the way to the collect screen. The launch screens refuse those
 * before the click: until the collector has run, nobody knows the market's tick or contract.
 *
 * The chips keep the order the markets were chosen in, a page at a time once there are many.
 */
export function SymbolPicker(props: {
  instruments: Instrument[] | undefined
  chosen: readonly string[]
  onChange: (next: string[]) => void
  /** How many markets the launch accepts: twenty for a basket, hundreds for a sweep. */
  max: number
}): React.JSX.Element {
  const { instruments, chosen, onChange, max } = props
  const [browsing, setBrowsing] = useState(false)
  const [offset, setOffset] = useState(0)
  const full = chosen.length >= max
  const outside = new Set(neverCollected(chosen, instruments))
  const shown = clampOffset(offset, CHIPS_PER_PAGE, chosen.length)

  const toggle = (symbol: string): void => {
    onChange(chosen.includes(symbol) ? chosen.filter((one) => one !== symbol) : [...chosen, symbol])
  }

  return (
    <fieldset className="space-y-2">
      <legend className="text-sm text-slate-300">
        Markets
        {chosen.length > 0 && <span className="text-slate-500"> — {chosen.length} chosen</span>}
      </legend>

      <div className="flex flex-wrap items-end gap-3">
        <button
          type="button"
          onClick={() => {
            setBrowsing(true)
          }}
          className="rounded border border-sky-700 bg-sky-950/40 px-3 py-1.5 text-sm text-sky-100 hover:border-sky-500"
        >
          Choose markets…
        </button>
        {chosen.length > 0 && (
          <button
            type="button"
            onClick={() => {
              onChange([])
            }}
            className="rounded border border-slate-700 px-3 py-1.5 text-sm text-slate-300 hover:border-slate-500"
          >
            Clear all
          </button>
        )}
        <div className="min-w-56 grow">
          <MarketSearch chosen={chosen} full={full} max={max} onToggle={toggle} />
        </div>
      </div>

      {chosen.length === 0 ? (
        <p className="text-sm text-slate-400">
          No market chosen yet.
          {instruments !== undefined && ` ${String(instruments.length)} collected so far.`}
        </p>
      ) : (
        <div className="space-y-2">
          <ul aria-label="Chosen markets" className="flex flex-wrap gap-1">
            {pageOf(chosen, shown, CHIPS_PER_PAGE).map((symbol) => {
              const never = outside.has(symbol)
              return (
                <li key={symbol}>
                  <button
                    type="button"
                    aria-label={never ? `Remove ${symbol}, never collected` : `Remove ${symbol}`}
                    className={`flex items-center gap-1 rounded border px-2 py-0.5 font-mono text-xs ${
                      never
                        ? 'border-amber-800 bg-amber-950/30 text-amber-200 hover:border-amber-600'
                        : 'border-sky-800 bg-sky-950/30 text-sky-100 hover:border-sky-600'
                    }`}
                    onClick={() => {
                      toggle(symbol)
                    }}
                  >
                    {symbol}
                    {never && <span className="font-sans text-amber-400">never collected</span>}
                    <span aria-hidden="true" className="text-slate-500">
                      ×
                    </span>
                  </button>
                </li>
              )
            })}
          </ul>
          <Pager
            label="Chosen market pages"
            offset={shown}
            limit={CHIPS_PER_PAGE}
            total={chosen.length}
            onOffset={setOffset}
          />
        </div>
      )}

      {outside.size > 0 && (
        <p className="text-xs text-amber-300">
          No candles yet for {[...outside].join(', ')} —{' '}
          <Link to="/collect" className="underline hover:text-amber-200">
            collect {outside.size === 1 ? 'it' : 'them'} first
          </Link>
          . The instrument is written by the collector, which is the only thing that can read its
          tick and contract from the terminal.
        </p>
      )}

      {browsing && (
        <MarketBrowser
          chosen={chosen}
          max={max}
          onChange={onChange}
          onClose={() => {
            setBrowsing(false)
          }}
        />
      )}
    </fieldset>
  )
}

/**
 * The search box over the broker's list. Picking toggles and the box clears for the next ticker
 * — the collect screen's multi-picker, speaking symbols rather than `BrokerSymbol`, because the
 * forms these screens hold are lists of names.
 *
 * ⚠️ At the ceiling, adding is refused and removing is not — the same rule as the browser.
 */
function MarketSearch(props: {
  chosen: readonly string[]
  full: boolean
  max: number
  onToggle: (symbol: string) => void
}): React.JSX.Element {
  const { chosen, full, max, onToggle } = props
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
          ⚠️ Already at {max} markets — remove one to add another.
        </p>
      )}

      <SnapshotFooter snapshot={snapshot} />
    </div>
  )
}
