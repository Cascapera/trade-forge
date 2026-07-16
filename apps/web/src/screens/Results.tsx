import { Link, useParams } from 'react-router-dom'

import { useBacktest, useEquity, useTrades } from '../api/hooks'
import type { BacktestStatus } from '../api/types'
import { EquityCurve } from '../components/EquityCurve'
import { MetricCards } from '../components/MetricCards'
import { TradesTable } from '../components/TradesTable'

const badge: Record<BacktestStatus, string> = {
  queued: 'bg-slate-700 text-slate-200',
  running: 'bg-sky-900 text-sky-200',
  done: 'bg-emerald-900 text-emerald-200',
  failed: 'bg-red-900 text-red-200',
}

function StatusBadge({ status }: { status: BacktestStatus }): React.JSX.Element {
  return (
    <span className={`rounded px-2 py-1 text-xs font-medium ${badge[status]}`}>{status}</span>
  )
}

export function Results(): React.JSX.Element {
  const { id } = useParams()
  const backtest = useBacktest(id)
  const done = backtest.data?.status === 'done'
  const trades = useTrades(id, done)
  const equity = useEquity(id, done)

  if (backtest.isPending) {
    return <p className="text-slate-400">Loading…</p>
  }
  if (backtest.isError) {
    return <p className="text-red-400">Could not load this backtest.</p>
  }

  const run = backtest.data

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">Backtest results</h2>
        <StatusBadge status={run.status} />
      </div>

      {(run.status === 'queued' || run.status === 'running') && (
        <p className="text-slate-400">Running the backtest — this page updates itself.</p>
      )}

      {run.status === 'failed' && (
        <p className="rounded border border-red-800 bg-red-950/40 p-4 text-sm text-red-300">
          The backtest failed: {run.error ?? 'unknown error'}
        </p>
      )}

      {run.status === 'done' && run.metrics !== null && (
        <>
          <MetricCards metrics={run.metrics} />
          <section>
            <h3 className="mb-2 font-medium">Equity curve</h3>
            {equity.data !== undefined && <EquityCurve points={equity.data} />}
          </section>
          <section>
            <h3 className="mb-2 font-medium">Trades</h3>
            {trades.data !== undefined && <TradesTable trades={trades.data.items} />}
          </section>
        </>
      )}

      <Link to="/launch" className="text-sm text-sky-400 hover:text-sky-300">
        Run another backtest
      </Link>
    </div>
  )
}
