import { useParams } from 'react-router-dom'

import { useCluster } from '../api/hooks'
import type { ClusterSkip } from '../api/types'
import { EquityCurve } from '../components/EquityCurve'
import { money, percent } from '../format'

const SKIP_LABEL: Record<ClusterSkip, string> = {
  positions: 'too many open',
  risk: 'too much risk open',
  no_stop: 'no stop',
  empty: 'account empty',
}

function tone(value: string | null): string {
  if (value === null) return 'text-slate-400'
  return Number(value) > 0 ? 'text-emerald-400' : Number(value) < 0 ? 'text-red-400' : ''
}

function Tile(props: { label: string; value: string; className?: string }): React.JSX.Element {
  return (
    <div className="rounded border border-slate-800 p-3">
      <div className="text-xs text-slate-500">{props.label}</div>
      <div className={`text-lg font-semibold ${props.className ?? ''}`}>{props.value}</div>
    </div>
  )
}

/**
 * One cluster: its members on one account, what the account went through, and what each member
 * brought and had skipped (25/09).
 *
 * ⚠️ **The deepest fall is of the marked equity**, measured on every mark before the curve drawn
 * here was thinned to one point a day — the chart can look shallower than the number, never deeper.
 */
export function ClusterResult(): React.JSX.Element {
  const { id } = useParams()
  const cluster = useCluster(id)

  if (cluster.isPending) return <p className="text-slate-400">Loading the cluster…</p>
  if (cluster.isError) return <p className="text-red-400">Could not load the cluster.</p>

  const data = cluster.data
  const done = data.status === 'done'
  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">{data.name}</h2>
        <p className="mt-1 text-sm text-slate-400">
          {String(data.members.length)} members on {money(data.initial_capital)} · at most{' '}
          {String(data.max_open_positions)} open · at most {Number(data.max_open_risk_percent)}%
          at risk
        </p>
      </header>

      {data.status === 'failed' && (
        <p role="alert" className="text-red-400">
          The replay failed: {data.error}
        </p>
      )}
      {!done && data.status !== 'failed' && (
        <p className="text-slate-400">
          Replaying on one account… ({data.status})
          {data.members.some((member) => member.rerun_of != null) &&
            ' — members that kept no trades are being run again first.'}
        </p>
      )}

      {done && (
        <>
          <div className="grid gap-3 sm:grid-cols-4">
            <Tile
              label="Final balance"
              value={data.final_balance === null ? '—' : money(data.final_balance)}
            />
            <Tile label="Return" value={percent(data.net_return)} className={tone(data.net_return)} />
            <Tile
              label="Deepest fall (marked)"
              value={`${percent(data.max_drawdown_pct)} · ${data.max_drawdown_abs === null ? '—' : money(data.max_drawdown_abs)}`}
              className="text-amber-300"
            />
            <Tile label="Most open at once" value={String(data.most_open ?? '—')} />
          </div>

          {data.curve !== null && data.curve.length > 0 && <EquityCurve points={data.curve} />}

          {data.yearly_return !== null && (
            <div className="flex flex-wrap gap-2 text-sm">
              {Object.entries(data.yearly_return).map(([year, value]) => (
                <span key={year} className={`rounded border border-slate-800 px-2 py-1 ${tone(value)}`}>
                  {year} {percent(value)}
                </span>
              ))}
            </div>
          )}
        </>
      )}

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">Members</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Run
            </th>
            <th scope="col" className="px-3 py-2">
              Market
            </th>
            <th scope="col" className="px-3 py-2">
              Risk
            </th>
            <th scope="col" className="px-3 py-2">
              Taken
            </th>
            <th scope="col" className="px-3 py-2">
              Skipped
            </th>
            <th scope="col" className="px-3 py-2">
              Brought
            </th>
          </tr>
        </thead>
        <tbody>
          {data.members.map((member) => {
            const skipped = Object.entries(member.skipped ?? {}).filter(([, many]) => many > 0)
            return (
              <tr key={member.backtest_id} className="border-b border-slate-900">
                <td className="px-3 py-2">
                  {member.label}
                  {member.rerun_of != null && (
                    <span
                      className="ml-2 rounded bg-slate-800 px-1 text-[10px] text-slate-300"
                      title={`The run asked for (${member.rerun_of}) kept no trades; this is the same run again, keeping them.`}
                    >
                      run again
                    </span>
                  )}
                </td>
                <td className="px-3 py-2">
                  {member.symbol} {member.timeframe}
                </td>
                <td className="px-3 py-2">{Number(member.risk_percent)}%</td>
                <td className="px-3 py-2">
                  {member.taken === null
                    ? '—'
                    : `${String(member.taken)} of ${String(member.offered ?? 0)}`}
                </td>
                <td className="px-3 py-2 text-slate-400">
                  {skipped.length === 0
                    ? '—'
                    : skipped
                        .map(([why, many]) => `${String(many)} ${SKIP_LABEL[why as ClusterSkip]}`)
                        .join(', ')}
                </td>
                <td className={`px-3 py-2 ${tone(member.net_pnl)}`}>
                  {member.net_pnl === null ? '—' : money(member.net_pnl)}
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </section>
  )
}
