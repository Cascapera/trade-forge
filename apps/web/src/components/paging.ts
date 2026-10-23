import { useState } from 'react'

/** The slice of an in-memory list that a `Pager` at `offset` shows. */
export function pageOf<T>(items: readonly T[], offset: number, limit: number): T[] {
  return items.slice(offset, offset + limit)
}

/**
 * The offset to show once the list has shrunk to `total`: a page past the end steps back to the
 * last one rather than showing an empty page under a pager that says "26–25 of 25".
 */
export function clampOffset(offset: number, limit: number, total: number): number {
  if (offset < total) return offset
  return Math.max(0, Math.floor((total - 1) / limit) * limit)
}

/**
 * One page of a list the screen already holds whole, and the props its `Pager` takes (04/10).
 *
 * For payloads that arrive in one piece — a holdout's points, a Monte Carlo's rows — where paging
 * on the server would be a second request for what is already here. What it saves is the DOM: a
 * table of 1400 rows is slow to draw and impossible to read.
 */
export function usePaged<T>(
  items: readonly T[],
  limit: number,
): {
  page: T[]
  pager: { offset: number; limit: number; total: number; onOffset: (offset: number) => void }
} {
  const [offset, setOffset] = useState(0)
  const shown = clampOffset(offset, limit, items.length)
  return {
    page: pageOf(items, shown, limit),
    pager: { offset: shown, limit, total: items.length, onOffset: setOffset },
  }
}
