import { Link } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import type { MissingDataGate } from '../collect/gate'
import { missingLine } from '../collect/missing'

function plural(count: number, one: string, many: string): string {
  return count === 1 ? one : many
}

/**
 * The prompt: what is missing, and the two answers.
 *
 * - **Collect what is missing** queues the collections the plan names. ⚠️ It does not launch: until
 *   a run can wait for its collection, the person runs again once the collection has finished.
 * - **Run with what there is** launches now. The server runs the part of the window that exists;
 *   a single backtest with nothing in the window is refused, and a basket leaves such a market out
 *   and names it.
 *
 * A plan that could not be asked says so and still offers the run: the launch asks the same index
 * itself, so going ahead never runs blind.
 *
 * `launching` disables both run buttons while the launch is on its way — a double click would
 * otherwise create the run twice.
 */
export function MissingDataPrompt({
  gate,
  onRunAnyway,
  launching,
  canRun,
}: {
  gate: MissingDataGate
  onRunAnyway: () => void
  launching: boolean
  /** Whether any market being launched would read a candle. ⚠️ False hides the run: the launch
   *  would be refused, and offering it offers nothing. A plan that could not be asked cannot
   *  answer this, so the run is offered there regardless. */
  canRun: boolean
}): React.JSX.Element | null {
  const { missing, plan, collection, outstanding } = gate

  const runButton = (label: string): React.JSX.Element => (
    <button type="button" disabled={launching} onClick={onRunAnyway} className={secondary}>
      {launching ? 'Enqueuing…' : label}
    </button>
  )

  if (plan.isError) {
    return (
      <section aria-label="missing data" className={panel}>
        <p>
          Could not check which data is on disk:{' '}
          {apiFailure(plan.error, 'the server did not answer.')}
        </p>
        {runButton('Run anyway')}
      </section>
    )
  }
  if (missing === null) return null

  const queued = collection.isSuccess
    ? collection.data.length
    : collection.isError
      ? collection.error.queued.length
      : null
  return (
    <section aria-label="missing data" className={panel}>
      <p className="font-medium">Some of this window has not been collected:</p>
      <ul className="list-disc space-y-1 pl-5 font-mono text-xs">
        {missing.map((market) => (
          <li key={`${market.symbol}|${market.timeframe}`}>{missingLine(market)}</li>
        ))}
      </ul>

      {queued !== null && queued > 0 && (
        <p role="status" className="text-emerald-300">
          Queued {queued} {plural(queued, 'collection', 'collections')}. Follow{' '}
          {plural(queued, 'it', 'them')} on{' '}
          <Link to="/collect" className="underline">
            Collect
          </Link>{' '}
          and run again once {plural(queued, 'it has', 'they have')} finished.
        </p>
      )}
      {collection.isError && (
        <p className="text-red-400">
          {queued === 0 ? 'Nothing was queued: ' : 'The rest was not queued: '}
          {apiFailure(collection.error.refusal, 'the server refused the collection.')}
        </p>
      )}

      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          disabled={collection.isPending || outstanding === 0}
          onClick={gate.collect}
          className="rounded bg-sky-600 px-3 py-1.5 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
        >
          {collection.isPending
            ? 'Queueing…'
            : outstanding === 0
              ? 'Already queued'
              : 'Collect what is missing'}
        </button>
        {canRun ? (
          runButton('Run with what there is')
        ) : (
          <p className="self-center text-amber-300">
            Nothing would run until this is collected.
          </p>
        )}
      </div>
    </section>
  )
}

const panel =
  'space-y-3 rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200'

const secondary =
  'rounded border border-slate-600 px-3 py-1.5 font-medium text-slate-100 enabled:hover:border-slate-400 disabled:opacity-40'
