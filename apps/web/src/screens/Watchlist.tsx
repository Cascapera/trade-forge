import { Link } from 'react-router-dom'

import { useChangeWatchItem, useRemoveWatchItem, useWatchItems } from '../api/hooks'
import type { WatchItem } from '../api/types'
import { BrokerTag } from '../components/BrokerTag'

/**
 * What the live signals follow (signals PR 4). Each item is a run's setup on a market, copied
 * from the run — added with "Watch" on a run's result. Off keeps the item and stops its signals;
 * remove forgets it.
 */
export function Watchlist(): React.JSX.Element {
  const items = useWatchItems()
  const change = useChangeWatchItem()
  const remove = useRemoveWatchItem()
  const rows = items.data ?? []
  const error = change.error ?? remove.error

  return (
    <section className="space-y-3">
      <header className="space-y-1">
        <h2 className="text-sm font-semibold">Watched setups</h2>
        <p className="text-xs text-slate-500">
          Each line is followed live once signals are on. Add one with “Watch” on a run’s result.
          A setup with no target closes its signal at the R shown, the stop, or its own exit.
        </p>
      </header>
      {error && <p className="text-sm text-red-400">{error.message}</p>}
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500">
          {items.isPending ? 'Loading…' : 'Nothing is watched yet.'}
        </p>
      ) : (
        <table className="w-full text-sm">
          <thead>
            <tr className="text-left text-xs text-slate-500">
              <th className="px-3 py-1 font-normal">Symbol</th>
              <th className="px-3 py-1 font-normal">TF</th>
              <th className="px-3 py-1 font-normal">Setup</th>
              <th className="px-3 py-1 font-normal">No-target R</th>
              <th className="px-3 py-1 font-normal">Note</th>
              <th className="px-3 py-1 font-normal">State</th>
              <th className="px-3 py-1 font-normal" />
            </tr>
          </thead>
          <tbody>
            {rows.map((item) => (
              <WatchRow
                key={item.id}
                item={item}
                busy={change.isPending || remove.isPending}
                onToggle={() => {
                  change.mutate({ id: item.id, patch: { active: !item.active } })
                }}
                onRemove={() => {
                  remove.mutate(item.id)
                }}
              />
            ))}
          </tbody>
        </table>
      )}
    </section>
  )
}

function WatchRow(props: {
  item: WatchItem
  busy: boolean
  onToggle: () => void
  onRemove: () => void
}): React.JSX.Element {
  const { item, busy, onToggle, onRemove } = props
  return (
    <tr className={`border-t border-slate-800 ${item.active ? '' : 'opacity-50'}`}>
      <td className="px-3 py-1">
        <span className="flex items-center gap-2">
          <span className="font-mono">{item.symbol}</span>
          <BrokerTag broker={item.broker} />
        </span>
      </td>
      <td className="px-3 py-1 font-mono">{item.timeframe}</td>
      <td className="px-3 py-1">
        {item.source_backtest_id ? (
          <Link to={`/results/${item.source_backtest_id}`} className="underline">
            {item.strategy_name}
          </Link>
        ) : (
          item.strategy_name
        )}
      </td>
      <td className="px-3 py-1">{Number(item.no_target_r)}R</td>
      <td className="px-3 py-1 text-slate-400">{item.note}</td>
      <td className="px-3 py-1">{item.active ? 'on' : 'off'}</td>
      <td className="space-x-2 px-3 py-1 text-right">
        <button
          type="button"
          disabled={busy}
          onClick={onToggle}
          className="rounded border border-slate-700 px-2 text-xs hover:bg-slate-800 disabled:opacity-40"
        >
          {item.active ? 'Turn off' : 'Turn on'}
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onRemove}
          aria-label={`Remove ${item.symbol} ${item.timeframe} ${item.strategy_name}`}
          className="rounded border border-slate-700 px-2 text-xs text-red-300 hover:bg-slate-800 disabled:opacity-40"
        >
          Remove
        </button>
      </td>
    </tr>
  )
}
