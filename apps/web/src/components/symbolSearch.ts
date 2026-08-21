import { useEffect, useState } from 'react'

import { useSymbolSearch } from '../api/hooks'
import type { BrokerSymbol, SymbolSnapshot } from '../api/types'

export const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * How long typing has to pause before the catalogue is asked. The query itself is a prefix
 * match on a table of tens to thousands of rows and costs microseconds — this is not about
 * load. It is about not putting a request in flight for `e`, `eu` and `eur` when the only
 * answer anybody wanted was the third.
 */
const DEBOUNCE_MS = 150

/**
 * The catalogue's answer for what is currently typed.
 *
 * Shared by the single-symbol field and the multi-symbol picker. The two differ in what a pick
 * *means* — replace versus toggle — and in nothing else, so the searching, the debounce and the
 * provenance live here rather than in two copies that drift.
 */
export function useSymbolResults(text: string): {
  debounced: string
  results: BrokerSymbol[]
  snapshot: SymbolSnapshot | null
} {
  // The query runs on the debounced text, the input shows the immediate text. Binding the input
  // to the debounced value instead would make typing feel like it was fighting back.
  const [debounced, setDebounced] = useState(text)
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebounced(text)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [text])

  const search = useSymbolSearch(debounced)
  return {
    debounced,
    results: search.data?.symbols ?? [],
    snapshot: search.data?.snapshot ?? null,
  }
}

/**
 * Arrow keys, Enter and Escape over a list of results.
 *
 * Wrapping is deliberate: a list of eighteen EUR pairs is scrolled with the keyboard, and
 * stopping dead at the last row makes a person reach for the mouse to get back to the top.
 */
export function useListboxKeys(args: {
  results: BrokerSymbol[]
  open: boolean
  setOpen: (open: boolean) => void
  onPick: (found: BrokerSymbol) => void
}): {
  highlighted: number
  setHighlighted: (index: number) => void
  onKeyDown: (event: React.KeyboardEvent<HTMLInputElement>) => void
} {
  const { results, open, setOpen, onPick } = args
  const [highlighted, setHighlighted] = useState(0)

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>): void => {
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      setOpen(true)
      const step = event.key === 'ArrowDown' ? 1 : -1
      setHighlighted(
        results.length === 0 ? 0 : (highlighted + step + results.length) % results.length
      )
      return
    }
    if (event.key === 'Enter') {
      const found = results[highlighted]
      if (open && found !== undefined) {
        event.preventDefault()
        onPick(found)
      }
      return
    }
    if (event.key === 'Escape') {
      setOpen(false)
    }
  }

  return { highlighted, setHighlighted, onKeyDown }
}
