import type { Collection } from '../api/types'

/**
 * The downloads that failed under a basket's or a sweep's runs — his rule of 22/09.
 *
 * A run whose download failed is not failed any more: it goes ahead on what is on disk, and
 * something has to say so. One run says it on its own screen; a group of them says it here, once,
 * at the top, because nobody opens three thousand runs to find the one whose download broke.
 *
 * ⚠️ **Not `skipped`.** A skipped market never ran. These did run, on less than was asked for,
 * and every metric below them is measured over whatever reached the disk — which is why the
 * sentence names the consequence and not only the failure.
 */
export function FailedDownloads(props: { downloads: Collection[] }): React.JSX.Element | null {
  const { downloads } = props
  if (downloads.length === 0) return null
  return (
    <div
      role="status"
      aria-label="failed downloads"
      className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
    >
      <p>
        {downloads.length === 1 ? 'A download failed' : `${String(downloads.length)} downloads failed`}
        {' '}— the runs that needed {downloads.length === 1 ? 'it' : 'them'} went ahead on the data
        already on disk:
      </p>
      <ul className="mt-1 space-y-0.5 text-xs text-amber-200/80">
        {downloads.map((one) => (
          <li key={one.id}>
            <span className="font-medium">
              {one.symbol} {one.timeframe}
            </span>{' '}
            — {one.error ?? 'no reason recorded'}
          </li>
        ))}
      </ul>
    </div>
  )
}
