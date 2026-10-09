import { Link } from 'react-router-dom'

import { useWatchBacktest } from '../api/hooks'

/**
 * "Watch": follow this run's setup on this market live once signals are on (signals PR 4). The
 * item copies the run, so cleaning the run away later leaves it whole.
 */
export function WatchThisRun(props: { runId: string }): React.JSX.Element {
  const watch = useWatchBacktest()
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      <button
        type="button"
        disabled={watch.isPending || watch.isSuccess}
        onClick={() => {
          watch.mutate(props.runId)
        }}
        className="rounded border border-emerald-700 px-3 py-1 text-emerald-200 hover:bg-emerald-950 disabled:opacity-50"
      >
        {watch.isPending ? 'Adding…' : 'Watch this setup live'}
      </button>
      {watch.isSuccess && (
        <span className="text-slate-400">
          Watched —{' '}
          <Link to="/live-signals" className="underline">
            see Live Signal
          </Link>
        </span>
      )}
      {watch.isError && <span className="text-red-400">{watch.error.message}</span>}
    </div>
  )
}
