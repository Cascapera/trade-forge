import { useState } from 'react'
import { Link } from 'react-router-dom'

import { SWEEPS_PER_PAGE, useSweeps } from '../api/hooks'
import type { SweepListItem } from '../api/types'
import { count } from '../format'
import { settled, summarise } from '../sweep/progress'

/** The launch instant in the reader's own clock, to the minute — which is how a person remembers one. */
function launched(iso: string): string {
  const moment = new Date(iso)
  const pad = (value: number): string => String(value).padStart(2, '0')
  return (
    `${String(moment.getFullYear())}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())} ` +
    `${pad(moment.getHours())}:${pad(moment.getMinutes())}`
  )
}

function entryNames(item: SweepListItem): string {
  return item.entries.map((entry) => entry.name ?? 'removed entry').join(', ')
}

function Line(props: { item: SweepListItem }): React.JSX.Element {
  const { item } = props
  const done = settled(item.runs)
  return (
    <li className="rounded-lg border border-slate-800 bg-slate-900/40 hover:border-slate-600">
      {/* The whole line is the link: the reader is looking for one sweep among many, and a target
          the size of a date would make them aim at it. */}
      <Link to={`/sweeps/${item.id}`} className="block space-y-1 px-4 py-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4">
          <span className="font-medium text-sky-400">{entryNames(item)}</span>
          <span className="text-xs text-slate-500">{launched(item.created_at)}</span>
        </div>
        <p className="text-sm text-slate-400">
          {item.symbols.join(', ')} · {item.timeframes.join(', ')} · {item.date_from.slice(0, 10)}{' '}
          → {item.date_to.slice(0, 10)}
        </p>
        <p className={`text-sm ${done ? 'text-slate-400' : 'text-sky-300'}`}>
          {summarise(item.runs)}
        </p>
      </Link>
    </li>
  )
}

/**
 * Every sweep ever launched, newest first, ten at a time.
 *
 * ⚠️ **The way back to a sweep once its tab is closed.** Before this screen a sweep was reachable
 * only at the moment it was launched: the run log hides the runs a grid generated, and
 * `/sweeps/:id` wants an id nobody writes down.
 *
 * A line says what was asked and how far it got — never a result. A sweep's entries are
 * alternatives, so the only honest summary is per entry, and that lives on the sweep's own page.
 */
export function SweepHistory(): React.JSX.Element {
  const [offset, setOffset] = useState(0)
  const page = useSweeps(offset)

  if (page.isPending) return <p className="text-slate-400">Loading the sweep history…</p>
  if (page.isError) return <p className="text-red-400">Could not load the sweep history.</p>

  // ⚠️ Numbered from the page on screen, not from the one asked for. While the next page loads
  // the previous one stays up, and a label taken from the request would read "Page 2" over the
  // lines of page 1.
  const { total, items, offset: shown } = page.data
  const pages = Math.max(1, Math.ceil(total / SWEEPS_PER_PAGE))
  const current = Math.floor(shown / SWEEPS_PER_PAGE) + 1

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">Sweep history</h2>
        <p className="text-sm text-slate-400">
          {count(total)} sweep{total === 1 ? '' : 's'}, newest first.
        </p>
      </div>

      {/* By the total, not by the page: a page past the end is empty in a history that is not. */}
      {total === 0 ? (
        <p className="rounded-lg border border-slate-800 bg-slate-900/40 p-8 text-center text-sm text-slate-400">
          No sweep has been launched yet.{' '}
          <Link to="/sweep" className="text-sky-400 underline hover:text-sky-300">
            Launch one
          </Link>
          .
        </p>
      ) : (
        <ul className="space-y-2">
          {items.map((item) => (
            <Line key={item.id} item={item} />
          ))}
        </ul>
      )}

      {pages > 1 && (
        <nav aria-label="Sweep history pages" className="flex items-center gap-3 text-sm">
          <button
            type="button"
            disabled={shown === 0}
            onClick={() => {
              setOffset(Math.max(0, shown - SWEEPS_PER_PAGE))
            }}
            className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40"
          >
            ← Newer
          </button>
          <span className="text-slate-400">
            Page {String(current)} of {String(pages)}
          </span>
          <button
            type="button"
            disabled={current >= pages}
            onClick={() => {
              setOffset(shown + SWEEPS_PER_PAGE)
            }}
            className="rounded border border-slate-700 px-3 py-1 disabled:opacity-40"
          >
            Older →
          </button>
        </nav>
      )}
    </div>
  )
}
