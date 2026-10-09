import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useChangeLive, useInstruments, useLiveSetups, useLiveSignals } from '../api/hooks'
import type { LiveMetrics, LiveSetup, SignalRow } from '../api/types'
import { BrokerTag } from '../components/BrokerTag'

/**
 * Live Signal (09/10): the setups the live signals follow, the markets each one is on, and what
 * it has posted — with a run's metrics, in R, as a running history. A setup comes from "Watch this
 * setup live" on a run; markets are added, switched off or dropped here at any time.
 */
export function LiveSignals(): React.JSX.Element {
  const setups = useLiveSetups()
  const rows = setups.data ?? []
  return (
    <section className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold text-slate-100">Live Signal</h1>
        <p className="text-sm text-slate-400">
          The setups the live signals follow. Add one with “Watch this setup live” on a run; add or
          drop its markets here. A setup without a target closes its signal at the R shown, the
          stop, or its own exit.
        </p>
      </header>
      {rows.length === 0 ? (
        <p className="text-sm text-slate-500">
          {setups.isPending ? 'Loading…' : 'No setup is followed yet.'}
        </p>
      ) : (
        rows.map((setup) => <SetupCard key={setup.id} setup={setup} />)
      )}
    </section>
  )
}

function SetupCard(props: { setup: LiveSetup }): React.JSX.Element {
  const { setup } = props
  const change = useChangeLive()
  const [history, setHistory] = useState(false)
  const busy = change.isPending

  return (
    <article
      className={`space-y-3 rounded-lg border border-slate-800 p-4 ${setup.active ? '' : 'opacity-60'}`}
    >
      <header className="flex flex-wrap items-baseline gap-3">
        <h2 className="text-base font-semibold text-slate-100">{setup.name}</h2>
        <span className="font-mono text-sm text-slate-300">{setup.timeframe}</span>
        <span className="text-xs text-slate-500">
          no target: {Number(setup.no_target_r)}R
          {setup.source_backtest_id !== null && (
            <>
              {' · '}
              <Link to={`/results/${setup.source_backtest_id}`} className="underline">
                run it came from
              </Link>
            </>
          )}
        </span>
        <span className="ml-auto flex gap-2">
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              change.mutate({ kind: 'setup', id: setup.id, patch: { active: !setup.active } })
            }}
            className="rounded border border-slate-700 px-2 text-xs hover:bg-slate-800 disabled:opacity-40"
          >
            {setup.active ? 'Turn off' : 'Turn on'}
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              change.mutate({ kind: 'remove-setup', id: setup.id })
            }}
            aria-label={`Remove ${setup.name}`}
            className="rounded border border-slate-700 px-2 text-xs text-red-300 hover:bg-slate-800 disabled:opacity-40"
          >
            Remove
          </button>
        </span>
      </header>

      <Metrics metrics={setup.metrics} />

      <Markets setup={setup} busy={busy} change={change} />

      {change.error && <p className="text-sm text-red-400">{change.error.message}</p>}

      <button
        type="button"
        onClick={() => {
          setHistory(!history)
        }}
        className="text-xs text-sky-400 hover:text-sky-300"
        aria-expanded={history}
      >
        {history ? 'Hide' : 'Show'} the signals ({String(setup.metrics.signals)})
      </button>
      {history && <History setupId={setup.id} />}
    </article>
  )
}

function r(value: string | null): string {
  if (value === null) return '—'
  const n = Number(value)
  return `${n > 0 ? '+' : ''}${n.toFixed(2)}R`
}

function Metrics(props: { metrics: LiveMetrics }): React.JSX.Element {
  const m = props.metrics
  const items: [string, string][] = [
    ['Net', r(m.net_r)],
    ['Win rate', m.win_rate === null ? '—' : `${(Number(m.win_rate) * 100).toFixed(0)}%`],
    ['Profit factor', m.profit_factor === null ? '—' : Number(m.profit_factor).toFixed(2)],
    ['Average', r(m.average_r)],
    ['Max drawdown', `${Number(m.max_drawdown_r).toFixed(2)}R`],
    ['Closed / open / cancelled', `${String(m.closed)} / ${String(m.open)} / ${String(m.cancelled)}`],
  ]
  return (
    <dl className="grid grid-cols-2 gap-x-6 gap-y-1 text-sm sm:grid-cols-3 lg:grid-cols-6">
      {items.map(([label, value]) => (
        <div key={label}>
          <dt className="text-xs text-slate-500">{label}</dt>
          <dd className="tabular-nums text-slate-200">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Markets(props: {
  setup: LiveSetup
  busy: boolean
  change: ReturnType<typeof useChangeLive>
}): React.JSX.Element {
  const { setup, busy, change } = props
  const instruments = useInstruments()
  const [adding, setAdding] = useState('')
  const followed = new Set(setup.markets.map((market) => market.instrument_id))
  const choices = (instruments.data ?? []).filter((one) => !followed.has(one.id))

  return (
    <div className="flex flex-wrap items-center gap-2">
      {setup.markets.map((market) => (
        <span
          key={market.instrument_id}
          className={`flex items-center gap-1 rounded border px-2 py-0.5 text-sm ${
            market.active ? 'border-emerald-800' : 'border-slate-700 opacity-50'
          }`}
        >
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              change.mutate({
                kind: 'market',
                id: setup.id,
                instrumentId: market.instrument_id,
                active: !market.active,
              })
            }}
            title={market.active ? 'Followed — click to switch off' : 'Off — click to switch on'}
            className="font-mono"
          >
            {market.symbol}
          </button>
          <BrokerTag broker={market.broker} />
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              change.mutate({ kind: 'remove-market', id: setup.id, instrumentId: market.instrument_id })
            }}
            aria-label={`Drop ${market.symbol}`}
            className="text-slate-500 hover:text-red-300"
          >
            ×
          </button>
        </span>
      ))}
      <select
        value={adding}
        onChange={(event) => {
          setAdding(event.target.value)
        }}
        aria-label="Market to add"
        className="rounded border border-slate-700 bg-slate-900 px-2 py-0.5 text-sm"
      >
        <option value="">add a market…</option>
        {choices.map((one) => (
          <option key={one.id} value={one.id}>
            {one.symbol}
            {one.broker ? ` (${one.broker})` : ''}
          </option>
        ))}
      </select>
      <button
        type="button"
        disabled={busy || adding === ''}
        onClick={() => {
          change.mutate({ kind: 'add-market', id: setup.id, instrumentId: adding })
          setAdding('')
        }}
        className="rounded border border-slate-700 px-2 text-xs hover:bg-slate-800 disabled:opacity-40"
      >
        Add
      </button>
    </div>
  )
}

const STATUS: Record<SignalRow['status'], string> = {
  armed: 'armed',
  triggered: 'in trade',
  cancelled: 'cancelled',
  closed: 'closed',
}

function when(value: string | null): string {
  if (value === null) return '—'
  return new Date(value).toLocaleString('pt-BR', {
    timeZone: 'America/Sao_Paulo',
    day: '2-digit',
    month: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function History(props: { setupId: string }): React.JSX.Element {
  const signals = useLiveSignals(props.setupId)
  const rows = signals.data ?? []
  if (rows.length === 0) {
    return (
      <p className="text-sm text-slate-500">
        {signals.isPending ? 'Loading…' : 'No signal posted yet.'}
      </p>
    )
  }
  return (
    <table className="w-full text-sm">
      <thead>
        <tr className="text-left text-xs text-slate-500">
          <th className="px-2 py-1 font-normal">#</th>
          <th className="px-2 py-1 font-normal">Market</th>
          <th className="px-2 py-1 font-normal">Side</th>
          <th className="px-2 py-1 font-normal">State</th>
          <th className="px-2 py-1 text-right font-normal">Entry</th>
          <th className="px-2 py-1 text-right font-normal">Stop</th>
          <th className="px-2 py-1 text-right font-normal">Target</th>
          <th className="px-2 py-1 text-right font-normal">Exit</th>
          <th className="px-2 py-1 text-right font-normal">Result</th>
          <th className="px-2 py-1 font-normal">Armed</th>
          <th className="px-2 py-1 font-normal">Ended</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((row) => (
          <tr key={row.number} className="border-t border-slate-800">
            <td className="px-2 py-1 tabular-nums">{row.number}</td>
            <td className="px-2 py-1 font-mono">{row.symbol}</td>
            <td className="px-2 py-1">{row.side === 'long' ? 'buy' : 'sell'}</td>
            <td className="px-2 py-1">{STATUS[row.status]}</td>
            <td className="px-2 py-1 text-right tabular-nums">{row.entry ?? '—'}</td>
            <td className="px-2 py-1 text-right tabular-nums">{row.stop ?? '—'}</td>
            <td className="px-2 py-1 text-right tabular-nums">{row.target ?? '—'}</td>
            <td className="px-2 py-1 text-right tabular-nums">{row.exit_price ?? '—'}</td>
            <td
              className={`px-2 py-1 text-right tabular-nums ${
                row.result_r === null
                  ? ''
                  : Number(row.result_r) > 0
                    ? 'text-emerald-400'
                    : 'text-red-400'
              }`}
            >
              {r(row.result_r)}
            </td>
            <td className="px-2 py-1">{when(row.armed_at)}</td>
            <td className="px-2 py-1">{when(row.ended_at)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}
