import { useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import {
  useBacktest,
  useCandles,
  useEquity,
  useOverlays,
  useRerunBacktest,
  useTrades,
} from '../api/hooks'
import type { BacktestStatus, OverlaySeries, Recorded, Zone } from '../api/types'
import { coverageNotice } from '../backtest/coverage'
import { count } from '../format'
import { EquityCurve } from '../components/EquityCurve'
import { MetricCards } from '../components/MetricCards'
import { PriceChart } from '../components/PriceChart'
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

type Tab = 'results' | 'price'

/** What a sweep's run left out, in a sentence — see `Recorded`. `full` needs none. */
const LEFT_OUT: Record<Exclude<Recorded, 'full'>, string> = {
  trades:
    'This run belongs to a sweep and kept its metrics and trades, but not the equity curve or the picture of each entry.',
  metrics:
    'This run belongs to a sweep and kept its metrics only: it did not pass the bar for keeping trades (a profit, and enough trades for its chart).',
}

// A module constant, not `[]` inline: a fresh array each render would recompute every curve
// and rebuild the chart, throwing away the zoom the reader had set.
const EMPTY_SERIES: OverlaySeries[] = []
const EMPTY_ZONES: Zone[] = []

const TABS: { id: Tab; label: string }[] = [
  { id: 'results', label: 'Results' },
  { id: 'price', label: 'Price' },
]

export function Results(): React.JSX.Element {
  const { id } = useParams()
  const backtest = useBacktest(id)
  const done = backtest.data?.status === 'done'
  const recorded = backtest.data?.recorded ?? 'full'
  // Asked for only when the run kept them: a sweep's run answers 404 for what it did not keep,
  // and a failed query there would read as a broken page rather than a deliberate absence.
  const trades = useTrades(id, done && recorded !== 'metrics')
  const equity = useEquity(id, done && recorded === 'full')
  const rerun = useRerunBacktest()
  const navigate = useNavigate()

  const [tab, setTab] = useState<Tab>('results')
  // Which trade the price chart is looking at. Lives here rather than in either component,
  // because the table that sets it and the chart that reads it are in different tabs.
  const [locatedTrade, setLocatedTrade] = useState<number | null>(null)

  // Fetched only once the price tab is open. It is by far the largest thing this page can ask
  // for — thousands of bars against a handful of metrics — and a reader who never leaves the
  // results tab should not pay for it.
  const candles = useCandles(id, done && tab === 'price')
  // Fetched on the same condition, and separately: a strategy may legitimately have no curves,
  // and a failure to compute them must not take the price chart down with it.
  const overlays = useOverlays(id, done && tab === 'price')

  // Held stable across renders. `?? []` inline would hand the chart a new array every time the
  // page re-rendered for any reason, and the chart would recompute and re-set every marker for
  // a list that had not changed.
  const tradeItems = useMemo(() => trades.data?.items ?? [], [trades.data])

  if (backtest.isPending) {
    return <p className="text-slate-400">Loading…</p>
  }
  if (backtest.isError) {
    return <p className="text-red-400">Could not load this backtest.</p>
  }

  const run = backtest.data
  const coverage = coverageNotice(run)
  // ⚠️ Only the unfinished ones. The list is kept after a download lands, and a finished
  // collection beside a run that is about to start is history, not something to wait for.
  const collecting = run.waiting_for.filter(
    (one) => one.status !== 'done' && one.status !== 'failed',
  )
  // ⚠️ A failed download no longer fails the run (his rule of 22/09): the run goes ahead on what
  // is on disk. What it owes the reader is the sentence saying so — before it runs, while it runs
  // and after, because every metric it shows is measured over whatever reached the disk.
  const brokenDownloads = run.waiting_for.filter((one) => one.status === 'failed')
  const underway = run.status === 'queued' || run.status === 'running'
  const price = candles.data

  return (
    <div className="space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-xl font-semibold">Backtest results</h2>
        <StatusBadge status={run.status} />
      </div>

      {coverage !== null && (
        <p
          role="status"
          className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
        >
          This run covers{' '}
          <strong>
            {coverage.actualFrom} to {coverage.actualTo}
          </strong>
          , not the {coverage.requestedFrom} to {coverage.requestedTo} that was requested — the
          collected data does not reach that far. Every metric below is measured over{' '}
          {count(coverage.candles)} candles in the shorter period.
        </p>
      )}

      {brokenDownloads.length > 0 && (
        <p
          role="status"
          className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
        >
          A download this run {underway ? 'needs' : 'needed'} failed:{' '}
          {brokenDownloads
            .map((one) => `${one.symbol} ${one.timeframe} — ${one.error ?? 'no reason recorded'}`)
            .join('; ')}
          . {underway ? 'The run goes ahead' : 'The run went ahead'} on the data already on disk.
        </p>
      )}

      {underway &&
        (collecting.length > 0 ? (
          <p
            role="status"
            className="rounded border border-sky-900 bg-sky-950/40 p-4 text-sm text-sky-200"
          >
            Waiting for the data this run needs:{' '}
            {collecting
              .map(
                (one) =>
                  `${one.symbol} ${one.timeframe} ${String(one.years_done)} of ${String(one.years_total)} years`,
              )
              .join(', ')}
            . It starts by itself once the download lands — this page updates itself.
          </p>
        ) : (
          <p className="text-slate-400">Running the backtest — this page updates itself.</p>
        ))}

      {run.status === 'failed' && (
        <p className="rounded border border-red-800 bg-red-950/40 p-4 text-sm text-red-300">
          The backtest failed: {run.error ?? 'unknown error'}
        </p>
      )}

      {run.status === 'done' && run.recorded !== 'full' && (
        <div
          role="status"
          aria-label="what this run kept"
          className="flex flex-wrap items-center justify-between gap-3 rounded border border-slate-700 bg-slate-900/60 p-4 text-sm text-slate-300"
        >
          <p>
            {LEFT_OUT[run.recorded]} Running the point again rebuilds everything. The engine is
            deterministic, so the new run is this one exactly — unless the candles for this window
            were re-collected or the engine changed since; the new run records both.
          </p>
          <button
            type="button"
            disabled={rerun.isPending}
            onClick={() => {
              rerun.mutate(run.id, {
                onSuccess: (created) => {
                  void navigate(`/results/${created.id}`)
                },
              })
            }}
            className="rounded bg-sky-700 px-3 py-1.5 font-medium text-white hover:bg-sky-600 disabled:opacity-50"
          >
            {rerun.isPending ? 'Queueing…' : 'Run this point again with everything'}
          </button>
          {rerun.isError && (
            <p className="w-full text-red-400">Could not queue the run: {rerun.error.message}</p>
          )}
        </div>
      )}

      {run.status === 'done' && run.metrics !== null && (
        <>
          <div role="tablist" aria-label="Backtest views" className="flex gap-1 border-b border-slate-800">
            {TABS.map(({ id: tabId, label }) => (
              <button
                key={tabId}
                type="button"
                role="tab"
                id={`tab-${tabId}`}
                aria-selected={tab === tabId}
                aria-controls={`panel-${tabId}`}
                onClick={() => {
                  setTab(tabId)
                }}
                className={`-mb-px border-b-2 px-4 py-2 text-sm focus-visible:outline-2 focus-visible:outline-sky-400 ${
                  tab === tabId
                    ? 'border-sky-400 text-slate-100'
                    : 'border-transparent text-slate-400 hover:text-slate-200'
                }`}
              >
                {label}
              </button>
            ))}
          </div>

          {tab === 'results' && (
            <div id="panel-results" role="tabpanel" aria-labelledby="tab-results" className="space-y-6">
              <MetricCards metrics={run.metrics} />
              <section>
                <h3 className="mb-2 font-medium">Equity curve</h3>
                {run.recorded !== 'full' ? (
                  <p className="text-sm text-slate-500">Not kept by this run.</p>
                ) : (
                  equity.data !== undefined && <EquityCurve points={equity.data} />
                )}
              </section>
              <section>
                <h3 className="mb-2 font-medium">Trades</h3>
                {run.recorded === 'metrics' && (
                  <p className="text-sm text-slate-500">
                    Not kept by this run — {count(run.metrics.total_trades)} were made.
                  </p>
                )}
                {trades.data !== undefined && (
                  <TradesTable
                    trades={trades.data.items}
                    backtestId={run.id}
                    onLocate={(tradeId) => {
                      setLocatedTrade(tradeId)
                      setTab('price')
                    }}
                  />
                )}
              </section>
            </div>
          )}

          {tab === 'price' && (
            <div id="panel-price" role="tabpanel" aria-labelledby="tab-price" className="space-y-4">
              {candles.isPending && <p className="text-slate-400">Loading the price…</p>}
              {candles.isError && (
                <p className="rounded border border-red-800 bg-red-950/40 p-4 text-sm text-red-300">
                  Could not load the price for this run:{' '}
                  {candles.error instanceof Error ? candles.error.message : 'unknown error'}
                </p>
              )}
              {price !== undefined && (
                <>
                  {/* Two answers to two different questions: what the run recorded reading, and
                      what the dataset holds for that window now. They are normally the same, and
                      when they are not the reader has to be told — the Parquet underneath a run
                      can be re-collected, and a chart quietly drawn from fewer bars than the
                      trades were made on looks exactly like a correct one. */}
                  {price.count !== price.candles_seen && (
                    <p
                      role="status"
                      className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
                    >
                      This run recorded reading {count(price.candles_seen)} candles, but{' '}
                      {count(price.count)} are on disk for that window today. The dataset has been
                      re-collected since the run — the trades below were decided on bars this chart
                      may no longer be showing.
                    </p>
                  )}
                  <PriceChart
                    candles={price.candles}
                    trades={tradeItems}
                    selectedTradeId={locatedTrade}
                    overlays={overlays.data?.series ?? EMPTY_SERIES}
                    zones={overlays.data?.zones ?? EMPTY_ZONES}
                    symbol={price.symbol}
                    timeframe={price.timeframe}
                  />
                  <p className="text-xs text-slate-500">
                    {count(price.count)} candles of {price.symbol} {price.timeframe} — the window
                    this run read, not the whole dataset. Pick a trade from the Results tab to
                    bring the chart to it.
                  </p>
                </>
              )}
            </div>
          )}
        </>
      )}

      <Link to="/" className="text-sm text-sky-400 hover:text-sky-300">
        Run another backtest
      </Link>
    </div>
  )
}
