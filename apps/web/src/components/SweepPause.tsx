import { usePauseSweep } from '../api/hooks'
import type { SweepRunCounts } from '../api/types'
import { count } from '../format'

/**
 * Pause and resume a sweep (his ask, 02/10): pause, wait for the runs in hand to finish, turn the
 * machines off; turn them on, bring Docker up, resume.
 *
 * ⚠️ **The screen says when it is safe to turn off, and only then.** A run a worker holds when
 * its machine goes off is left `running`; resume queues it again, but the work it did is lost.
 * Paused with none running, nothing is.
 */
export function SweepPause({
  sweepId,
  pausedAt,
  counts,
}: {
  sweepId: string
  pausedAt: string | null
  counts: Pick<SweepRunCounts, 'queued' | 'running'>
}): React.JSX.Element | null {
  const action = usePauseSweep(sweepId)
  const paused = pausedAt !== null
  if (!paused && counts.queued === 0 && counts.running === 0) return null

  const pending = action.isPending
  return (
    <div className="flex flex-wrap items-center gap-3 text-sm">
      {paused ? (
        <button
          type="button"
          disabled={pending}
          onClick={() => {
            action.mutate('resume')
          }}
          className="rounded border border-emerald-600 px-3 py-1 text-emerald-300 hover:border-emerald-400 disabled:opacity-50"
        >
          {pending ? 'Resuming…' : 'Resume'}
        </button>
      ) : (
        <button
          type="button"
          disabled={pending}
          onClick={() => {
            action.mutate('pause')
          }}
          className="rounded border border-amber-600 px-3 py-1 text-amber-300 hover:border-amber-400 disabled:opacity-50"
        >
          {pending ? 'Pausing…' : 'Pause'}
        </button>
      )}
      {paused && (
        <span role="status" className={counts.running > 0 ? 'text-amber-300' : 'text-emerald-300'}>
          {counts.running > 0
            ? `Paused — ${count(counts.running)} run${counts.running === 1 ? '' : 's'} still finishing; wait before turning off.`
            : `Paused — safe to turn off. ${count(counts.queued)} run${counts.queued === 1 ? '' : 's'} waiting for resume.`}
        </span>
      )}
      {action.isError && (
        <span role="alert" className="text-red-400">
          Could not {paused ? 'resume' : 'pause'}: {action.error.message}
        </span>
      )}
      {action.data?.released !== undefined && action.data.released > 0 && (
        <span className="text-xs text-slate-400">
          {count(action.data.released)} run{action.data.released === 1 ? '' : 's'} left running by a
          machine turned off went back in the queue.
        </span>
      )}
    </div>
  )
}
