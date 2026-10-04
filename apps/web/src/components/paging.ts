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
