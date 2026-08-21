import { useSyncSymbols } from '../api/hooks'
import type { BrokerSymbol, SymbolSnapshot } from '../api/types'

/**
 * The dropdown itself: one option per result, or the sentence that says why there are none.
 *
 * ⚠️ **The empty state has two meanings and they are opposite problems.** Nothing starts with
 * those letters means type fewer letters; no catalogue at all means nobody has ever synced this
 * broker. Only the second one is fixed by the sync button, so only the second one mentions it.
 */
export function SymbolOptions(props: {
  id: string
  results: BrokerSymbol[]
  highlighted: number
  debounced: string
  snapshot: SymbolSnapshot | null
  /** Rendered at the right of each row — the ticked state differs between the two pickers. */
  badge?: (found: BrokerSymbol) => React.ReactNode
  onPick: (found: BrokerSymbol) => void
}): React.JSX.Element {
  const { id, results, highlighted, debounced, snapshot, badge, onPick } = props

  return (
    <ul
      id={id}
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
              onPick(found)
            }}
          >
            <span className="font-mono text-slate-100">{found.symbol}</span>
            <span className="truncate text-xs text-slate-400">{found.description}</span>
            {/* Marked when it is *not* runnable, rather than badging the one that is. The
                exception is what a reader needs to notice. */}
            {!found.catalogued && (
              <span className="shrink-0 rounded bg-amber-900/60 px-1 text-[10px] text-amber-200">
                no data
              </span>
            )}
            {badge?.(found)}
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
  )
}

/** Where the list came from, and the button that refreshes it. */
export function SnapshotFooter(props: { snapshot: SymbolSnapshot | null }): React.JSX.Element {
  const sync = useSyncSymbols()

  return (
    <div className="flex items-center justify-between gap-2 text-xs text-slate-500">
      <span>
        {props.snapshot === null
          ? 'never synced'
          : `${props.snapshot.server ?? 'unnamed server'} · ${new Date(
              props.snapshot.synced_at
            ).toLocaleString()}`}
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
  )
}
