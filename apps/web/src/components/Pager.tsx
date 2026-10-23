import { usePaged } from './paging'

/**
 * Previous / next over a long list, by offset — the one pager every list on these screens uses
 * (04/10, his ask: "everything listed on every page paginated").
 *
 * By offset rather than by page index because the server's pages are asked for that way, and a
 * list paged in memory slices the same way (`pageOf`). Renders nothing when everything fits.
 */
export function Pager(props: {
  /** What the list is, for the navigation landmark: "Symbols pages". */
  label: string
  offset: number
  limit: number
  total: number
  onOffset: (offset: number) => void
}): React.JSX.Element | null {
  const { label, offset, limit, total, onOffset } = props
  if (total <= limit) return null
  const last = Math.min(offset + limit, total)
  const button =
    'rounded border border-slate-700 px-3 py-1 hover:border-slate-500 disabled:opacity-40 disabled:hover:border-slate-700'
  return (
    <nav aria-label={label} className="flex items-center gap-3 text-sm">
      <button
        type="button"
        disabled={offset === 0}
        onClick={() => {
          onOffset(Math.max(0, offset - limit))
        }}
        className={button}
      >
        ← Previous
      </button>
      <span className="text-slate-400 tabular-nums">
        {String(offset + 1)}–{String(last)} of {String(total)}
      </span>
      <button
        type="button"
        disabled={last >= total}
        onClick={() => {
          onOffset(offset + limit)
        }}
        className={button}
      >
        Next →
      </button>
    </nav>
  )
}

/**
 * A list the screen holds whole, drawn a page at a time inside `as` (a `div` unless said), with
 * its `Pager` below. A table pages with `usePaged` instead: its pager cannot sit in a `<tbody>`.
 */
export function PagedList<T>(props: {
  items: readonly T[]
  limit: number
  label: string
  as?: 'div' | 'ul'
  className?: string
  children: (item: T) => React.ReactNode
}): React.JSX.Element {
  const { page, pager } = usePaged(props.items, props.limit)
  const List = props.as ?? 'div'
  return (
    <>
      <List className={props.className}>{page.map(props.children)}</List>
      <Pager label={props.label} {...pager} />
    </>
  )
}
