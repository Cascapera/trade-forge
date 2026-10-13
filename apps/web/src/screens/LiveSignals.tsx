import { useState } from 'react'
import { Link } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import {
  useChangeLive,
  useInstruments,
  useLiveMarkets,
  useLiveSetups,
  useLiveSignals,
  useRestartMarket,
} from '../api/hooks'
import type {
  LiveAssetOut,
  LiveMarketSetupOut,
  LiveMetrics,
  LiveSessionState,
  LiveSetup,
  LiveTimeframeOut,
  SignalRow,
  SupervisorOut,
} from '../api/types'
import { BrokerTag } from '../components/BrokerTag'
import { MarketBrowser } from '../components/MarketBrowser'
import { MAX_MARKETS, NewLiveSetup } from '../components/NewLiveSetup'
import { SetupEditor } from '../components/SetupEditor'

type View = 'asset' | 'setup'

const VIEWS: { id: View; label: string }[] = [
  { id: 'asset', label: 'By asset' },
  { id: 'setup', label: 'By setup' },
]

/**
 * Live Signal (09/10): the setups the live signals follow, the markets each one is on, and what
 * it has posted — with a run's metrics, in R, as a running history. A setup comes from "Watch this
 * setup live" on a run; markets are added, switched off or dropped here at any time.
 *
 * Two readings of the same setups (13/10, ADR-0033): by asset, the way their sessions run — one
 * process per asset and timeframe — and by setup, with its record summed over every market, which
 * is the one that judges a setup.
 */
export function LiveSignals(): React.JSX.Element {
  const setups = useLiveSetups()
  const rows = setups.data ?? []
  const [creating, setCreating] = useState(false)
  const [view, setView] = useState<View>('asset')
  return (
    <section className="space-y-4">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold text-slate-100">Live Signal</h1>
        <p className="text-sm text-slate-400">
          The setups the live signals follow, by asset as their sessions run or by setup with its
          record over every market. Add one with “Watch this setup live” on a run; add or drop its
          markets here. A setup without a target closes its signal at the R shown, the stop, or its
          own exit.
        </p>
        {!creating && (
          <button
            type="button"
            onClick={() => {
              setCreating(true)
            }}
            className="rounded bg-emerald-800 px-3 py-1 text-sm text-white hover:bg-emerald-700"
          >
            New setup
          </button>
        )}
      </header>
      {creating && (
        <NewLiveSetup
          onClose={() => {
            setCreating(false)
          }}
        />
      )}
      <div
        role="tablist"
        aria-label="Live signal views"
        className="flex gap-1 border-b border-slate-800"
      >
        {VIEWS.map(({ id, label }) => (
          <button
            key={id}
            type="button"
            role="tab"
            id={`tab-${id}`}
            aria-selected={view === id}
            aria-controls={`panel-${id}`}
            onClick={() => {
              setView(id)
            }}
            className={`-mb-px border-b-2 px-4 py-2 text-sm focus-visible:outline-2 focus-visible:outline-sky-400 ${
              view === id
                ? 'border-sky-400 text-slate-100'
                : 'border-transparent text-slate-400 hover:text-slate-200'
            }`}
          >
            {label}
          </button>
        ))}
      </div>
      {view === 'asset' ? (
        <div id="panel-asset" role="tabpanel" aria-labelledby="tab-asset" className="space-y-4">
          <ByAsset setups={rows} />
        </div>
      ) : (
        <div id="panel-setup" role="tabpanel" aria-labelledby="tab-setup" className="space-y-4">
          {rows.length === 0 ? (
            <p className="text-sm text-slate-500">
              {setups.isPending ? 'Loading…' : 'No setup is followed yet.'}
            </p>
          ) : (
            rows.map((setup) => <SetupCard key={setup.id} setup={setup} />)
          )}
        </div>
      )}
    </section>
  )
}

/**
 * The setups by asset, the way their sessions run (ADR-0033): an asset, its markets — one per
 * timeframe, each one process of the supervisor — and the setups on each, with what they have
 * under way and their record on that asset alone.
 */
function ByAsset(props: { setups: LiveSetup[] }): React.JSX.Element {
  const markets = useLiveMarkets()
  if (markets.data === undefined) {
    return markets.isError ? (
      <p className="text-sm text-red-400">
        {apiFailure(markets.error, 'Could not read the markets.')}
      </p>
    ) : (
      <p className="text-sm text-slate-500">Loading…</p>
    )
  }
  const { supervisor, assets } = markets.data
  return (
    <>
      <Supervisor supervisor={supervisor} />
      {assets.length === 0 ? (
        <p className="text-sm text-slate-500">No market is followed yet.</p>
      ) : (
        assets.map((asset) => (
          <AssetCard
            key={asset.instrument_id}
            asset={asset}
            alive={supervisor.alive}
            setups={props.setups}
          />
        ))
      )}
    </>
  )
}

/**
 * The supervisor's own word, at the top. ⚠️ Loud when it lapsed: a stopped supervisor restarts
 * nothing, so a session that fails stays down, and every state below is unknown.
 */
function Supervisor(props: { supervisor: SupervisorOut }): React.JSX.Element {
  const { supervisor } = props
  if (supervisor.alive) {
    return (
      <p className="text-xs text-slate-500">
        <span className="text-emerald-400">●</span> Signals supervisor running · last word{' '}
        {when(supervisor.at)}
      </p>
    )
  }
  return (
    <p
      role="alert"
      className="rounded border border-amber-700 bg-amber-950/40 px-3 py-2 text-sm text-amber-200"
    >
      {supervisor.reachable
        ? 'Signals supervisor stopped — run tradeforge-sinais.ps1'
        : 'Cannot reach Redis — nothing is known about the sessions'}
    </p>
  )
}

function AssetCard(props: {
  asset: LiveAssetOut
  alive: boolean
  setups: LiveSetup[]
}): React.JSX.Element {
  const { asset } = props
  const rows = asset.timeframes.flatMap((market) => market.setups)
  const on = rows.filter((row) => row.setup_active && row.active).length
  const { armed, inTrade } = underWay(rows)
  return (
    <article className="space-y-3 rounded-lg border border-slate-800 p-4">
      <header className="flex flex-wrap items-baseline gap-3">
        <h2 className="font-mono text-base font-semibold text-slate-100">{asset.symbol}</h2>
        <BrokerTag broker={asset.broker} />
        <span className="ml-auto text-xs text-slate-400">
          {`${plural(on, 'setup')} on · ${String(armed)} armed · ${String(inTrade)} in a trade`}
        </span>
      </header>
      {asset.timeframes.map((market) => (
        <MarketBlock
          key={market.timeframe}
          asset={asset}
          market={market}
          alive={props.alive}
          setups={props.setups}
        />
      ))}
    </article>
  )
}

/** How many of these setups' signals are armed, and how many are in a trade. */
function underWay(rows: LiveMarketSetupOut[]): { armed: number; inTrade: number } {
  const signals = rows.flatMap((row) => row.under_way)
  return {
    armed: signals.filter((one) => one.status === 'armed').length,
    inTrade: signals.filter((one) => one.status === 'triggered').length,
  }
}

function plural(count: number, noun: string): string {
  return `${String(count)} ${noun}${count === 1 ? '' : 's'}`
}

/**
 * One market — an asset on a timeframe — which is one process of the supervisor, and the setups
 * it runs. Restarting it is how a setup added since the last restart joins the others.
 */
function MarketBlock(props: {
  asset: LiveAssetOut
  market: LiveTimeframeOut
  alive: boolean
  setups: LiveSetup[]
}): React.JSX.Element {
  const { asset, market, alive } = props
  const change = useChangeLive()
  const restart = useRestartMarket()
  const [asking, setAsking] = useState(false)
  const name = `${asset.symbol} ${market.timeframe}`
  const { armed, inTrade } = underWay(market.setups)
  // Nothing would take the request without the supervisor, and one restart at a time.
  const blocked = !alive || market.restarting || restart.isPending
  const [state, tone] = processState(market, alive)

  return (
    <section
      aria-label={name}
      className="space-y-2 rounded border border-slate-800 bg-slate-900/40 p-3"
    >
      <header className="flex flex-wrap items-center gap-3">
        <h3 className="font-mono text-sm font-semibold text-slate-200">{market.timeframe}</h3>
        <span className={`text-xs ${tone}`}>{state}</span>
        <button
          type="button"
          disabled={blocked || asking}
          onClick={() => {
            setAsking(true)
          }}
          className="ml-auto rounded border border-amber-700 px-2 text-xs text-amber-200 hover:bg-slate-800 disabled:opacity-40"
        >
          {market.restarting ? 'Restarting…' : 'Restart'}
        </button>
      </header>

      {asking && (
        <div className="space-y-2 rounded border border-amber-800 bg-slate-900 p-3 text-sm">
          <p className="text-slate-300">
            Every session of {name} stops and starts again together, in one process, warming up
            for a few minutes.{' '}
            <strong className="text-amber-200">{consequence(armed, inTrade)}</strong>
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={blocked}
              onClick={() => {
                restart.mutate(
                  { instrument_id: asset.instrument_id, timeframe: market.timeframe },
                  {
                    onSuccess: () => {
                      setAsking(false)
                    },
                  },
                )
              }}
              className="rounded bg-amber-700 px-2 py-1 text-xs text-white disabled:bg-slate-700"
            >
              {restart.isPending ? 'Asking…' : `Restart ${name}`}
            </button>
            <button
              type="button"
              onClick={() => {
                setAsking(false)
              }}
              className="text-xs text-slate-500 hover:text-slate-300"
            >
              Keep it running
            </button>
          </div>
        </div>
      )}
      {restart.isSuccess && (
        <p className="text-xs text-amber-200">
          Asked at {when(new Date(restart.submittedAt).toISOString())}; the supervisor takes it
          within its next round (up to 15 s).
        </p>
      )}
      {restart.isError && (
        <p className="text-xs text-red-400">
          {apiFailure(restart.error, 'Could not ask for the restart.')}
        </p>
      )}

      <ul className="space-y-2">
        {market.setups.map((row) => (
          <SetupOnMarket key={row.setup_id} asset={asset} row={row} change={change} />
        ))}
      </ul>
      <AddSetup asset={asset} market={market} setups={props.setups} change={change} />
      {change.error && <p className="text-sm text-red-400">{change.error.message}</p>}
    </section>
  )
}

/** What the supervisor says of a market's processes, and the colour to say it in. */
function processState(market: LiveTimeframeOut, alive: boolean): [string, string] {
  if (market.restarting) return ['restarting…', 'text-amber-300']
  if (!alive) return ['unknown', 'text-slate-500']
  const { processes } = market
  if (processes.length === 0) {
    return market.setups.some((row) => row.setup_active && row.active)
      ? ['not running', 'text-red-400']
      : ['nothing switched on', 'text-slate-500']
  }
  const live = processes.every((one) => one.phase === 'live')
  const phase = live
    ? '● live'
    : processes.some((one) => one.phase === 'warming')
      ? 'warming'
      : 'starting'
  // ⚠️ More than one means a setup added since the last restart still runs apart (ADR-0033).
  const count =
    processes.length === 1
      ? '1 process'
      : `${String(processes.length)} processes — a setup added since the last restart runs apart`
  return [`${phase} · ${count}`, live ? 'text-emerald-400' : 'text-sky-300']
}

/** What a restart does to a market's signals under way — said before it is asked for. */
function consequence(armed: number, inTrade: number): string {
  if (armed === 0 && inTrade === 0) return 'No signal is under way here, so none is touched.'
  const cancelled = `${plural(armed, 'armed signal')} will be cancelled on the channel`
  return `${cancelled}; ${String(inTrade)} in a trade will be followed by price.`
}

const STATE: Record<Exclude<LiveSessionState, 'off'>, string> = {
  running: 'border-emerald-700 text-emerald-300',
  warming: 'border-sky-700 text-sky-300',
  starting: 'border-sky-700 text-sky-300',
  stopping: 'border-amber-700 text-amber-300',
  waiting: 'border-red-700 text-red-300',
  unknown: 'border-slate-700 text-slate-500',
}

/** A session's state as a pill; a switched-off one has none, its switch already says so. */
function StatePill(props: { row: LiveMarketSetupOut }): React.JSX.Element | null {
  const { state, retry_in: retryIn } = props.row
  if (state === 'off') return null
  return (
    <span className={`rounded-full border px-2 text-xs ${STATE[state]}`}>
      {state === 'waiting'
        ? `failed, retrying${retryIn === null ? '' : ` in ${String(retryIn)} s`}`
        : state}
    </span>
  )
}

/** One setup on one market: its switch here, its session, what it has under way, its record. */
function SetupOnMarket(props: {
  asset: LiveAssetOut
  row: LiveMarketSetupOut
  change: ReturnType<typeof useChangeLive>
}): React.JSX.Element {
  const { asset, row, change } = props
  const [history, setHistory] = useState(false)
  // Which destructive action is waiting for its confirmation, if any.
  const [asking, setAsking] = useState<'remove' | 'delete' | null>(null)
  const { armed, inTrade } = underWay([row])
  return (
    <li
      className={`space-y-1 border-t border-slate-800 pt-2 text-sm ${
        row.setup_active && row.active ? '' : 'opacity-60'
      }`}
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className="text-slate-100">{row.name}</span>
        {row.strategy_name !== row.name && (
          <span className="text-xs text-slate-500">{row.strategy_name}</span>
        )}
        <button
          type="button"
          role="switch"
          aria-checked={row.active}
          aria-label={`${row.name} on ${asset.symbol}`}
          disabled={change.isPending}
          onClick={() => {
            change.mutate({
              kind: 'market',
              id: row.setup_id,
              instrumentId: asset.instrument_id,
              active: !row.active,
            })
          }}
          title={
            row.active ? 'Followed here — click to switch off' : 'Off here — click to switch on'
          }
          className={`rounded border px-2 text-xs disabled:opacity-40 ${
            row.active ? 'border-emerald-700 text-emerald-300' : 'border-slate-700 text-slate-400'
          }`}
        >
          {row.active ? 'on' : 'off'}
        </button>
        {!row.setup_active && <span className="text-xs text-slate-500">setup switched off</span>}
        <StatePill row={row} />
        <span className="ml-auto text-xs tabular-nums text-slate-400">{record(row.metrics)}</span>
      </div>
      {row.under_way.length > 0 && (
        <ul className="space-y-0.5 text-xs tabular-nums">
          {row.under_way.map((signal) => (
            <li
              key={signal.number}
              className={signal.status === 'armed' ? 'text-sky-300' : 'text-emerald-300'}
            >
              {underWayLine(signal, row.no_target_r)}
            </li>
          ))}
        </ul>
      )}
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          onClick={() => {
            setHistory(!history)
          }}
          className="text-xs text-sky-400 hover:text-sky-300"
          aria-expanded={history}
        >
          {history ? 'Hide the history' : 'History'}
        </button>
        <button
          type="button"
          disabled={change.isPending}
          onClick={() => {
            setAsking('remove')
          }}
          className="text-xs text-red-300 hover:text-red-200 disabled:opacity-40"
        >
          Remove from {asset.symbol}
        </button>
        <button
          type="button"
          disabled={change.isPending}
          onClick={() => {
            setAsking('delete')
          }}
          className="text-xs text-red-300 hover:text-red-200 disabled:opacity-40"
        >
          Delete setup
        </button>
      </div>
      {asking !== null && (
        <div className="space-y-2 rounded border border-red-900 bg-slate-900 p-3 text-sm">
          <p className="text-slate-300">
            {asking === 'remove'
              ? `${row.name} stops following ${asset.symbol}; it stays on its other assets.`
              : `${row.name} is deleted from every asset it follows; its signals' history is kept.`}{' '}
            <strong className="text-amber-200">{consequence(armed, inTrade)}</strong>
          </p>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={change.isPending}
              onClick={() => {
                change.mutate(
                  asking === 'remove'
                    ? {
                        kind: 'remove-market',
                        id: row.setup_id,
                        instrumentId: asset.instrument_id,
                      }
                    : { kind: 'remove-setup', id: row.setup_id },
                  {
                    onSuccess: () => {
                      setAsking(null)
                    },
                  },
                )
              }}
              className="rounded bg-red-800 px-2 py-1 text-xs text-white disabled:bg-slate-700"
            >
              {asking === 'remove'
                ? `Yes, remove from ${asset.symbol}`
                : `Yes, delete ${row.name}`}
            </button>
            <button
              type="button"
              onClick={() => {
                setAsking(null)
              }}
              className="text-xs text-slate-500 hover:text-slate-300"
            >
              Keep it
            </button>
          </div>
        </div>
      )}
      {history && <History setupId={row.setup_id} symbol={asset.symbol} />}
    </li>
  )
}

/** A signal under way in one line: armed with its order, or in a trade with its levels. */
function underWayLine(signal: SignalRow, noTargetR: string): string {
  const side = signal.side === 'long' ? 'buy' : 'sell'
  const entry = signal.entry ?? '—'
  const stop = `stop ${signal.stop ?? '—'}`
  const head = `#${String(signal.number)} ${STATUS[signal.status]}:`
  if (signal.status === 'armed') {
    return [head, side, signal.order_type, entry, '·', stop].filter(Boolean).join(' ')
  }
  const target =
    signal.target === null ? `no target (${String(Number(noTargetR))}R)` : `target ${signal.target}`
  return `${head} ${side} ${entry} · ${stop} · ${target}`
}

/** A setup's record on one market in one line: its signals, its net R and how often it won. */
function record(m: LiveMetrics): string {
  return `${plural(m.signals, 'signal')} · net ${r(m.net_r)} · win rate ${percent(m.win_rate)}`
}

/**
 * Put this asset in one more setup of the market's timeframe — the timeframe is the setup's, so
 * only those can be added here. ⚠️ It then runs apart, in a process of its own, until the market
 * is restarted (ADR-0033): restarting the others to fit it in would cancel their armed signals.
 */
function AddSetup(props: {
  asset: LiveAssetOut
  market: LiveTimeframeOut
  setups: LiveSetup[]
  change: ReturnType<typeof useChangeLive>
}): React.JSX.Element {
  const { asset, market, change } = props
  const here = new Set(market.setups.map((row) => row.setup_id))
  const candidates = props.setups.filter(
    (setup) => setup.timeframe === market.timeframe && !here.has(setup.id),
  )
  if (candidates.length === 0) {
    return (
      <p className="text-xs text-slate-500">
        Every {market.timeframe} setup is already on {asset.symbol} — a new one comes from “Watch
        this setup live” on a run, or “New setup”.
      </p>
    )
  }
  return (
    <select
      aria-label={`Add a setup to ${asset.symbol} ${market.timeframe}`}
      value=""
      disabled={change.isPending}
      onChange={(event) => {
        change.mutate({
          kind: 'add-market',
          id: event.target.value,
          instrumentId: asset.instrument_id,
        })
      }}
      className="rounded border border-sky-700 bg-slate-900 px-2 py-0.5 text-xs text-sky-100 disabled:opacity-40"
    >
      <option value="">Add setup…</option>
      {candidates.map((setup) => (
        <option key={setup.id} value={setup.id}>
          {setup.name}
        </option>
      ))}
    </select>
  )
}

function SetupCard(props: { setup: LiveSetup }): React.JSX.Element {
  const { setup } = props
  const change = useChangeLive()
  const [history, setHistory] = useState(false)
  const [editing, setEditing] = useState(false)
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
            onClick={() => {
              setEditing(!editing)
            }}
            aria-expanded={editing}
            className="rounded border border-slate-700 px-2 text-xs hover:bg-slate-800"
          >
            Edit
          </button>
          <button
            type="button"
            disabled={busy}
            onClick={() => {
              change.mutate({
                kind: 'setup',
                id: setup.id,
                patch: { active: !setup.active },
              })
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

      {editing && (
        <SetupEditor
          setup={setup}
          onClose={() => {
            setEditing(false)
          }}
        />
      )}

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

function percent(value: string | null): string {
  return value === null ? '—' : `${(Number(value) * 100).toFixed(0)}%`
}

function Metrics(props: { metrics: LiveMetrics }): React.JSX.Element {
  const m = props.metrics
  const items: [string, string][] = [
    ['Net', r(m.net_r)],
    ['Win rate', percent(m.win_rate)],
    ['Profit factor', m.profit_factor === null ? '—' : Number(m.profit_factor).toFixed(2)],
    ['Average', r(m.average_r)],
    ['Max drawdown', `${Number(m.max_drawdown_r).toFixed(2)}R`],
    [
      'Closed / open / cancelled',
      `${String(m.closed)} / ${String(m.open)} / ${String(m.cancelled)}`,
    ],
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
  const [browsing, setBrowsing] = useState(false)
  // ⚠️ While the browser is open its ticks are kept here: the next tick must be compared with the
  // last one, not with a list the server has not sent back yet.
  const [ticked, setTicked] = useState<string[] | null>(null)
  const chosen = ticked ?? setup.markets.map((market) => market.symbol)

  /** The browser's ticks as calls: a new tick follows the market, an untick drops it. */
  const apply = (next: string[]): void => {
    const now = new Set(chosen)
    const wanted = new Set(next)
    const byName = new Map((instruments.data ?? []).map((one) => [one.symbol, one.id]))
    for (const symbol of next) {
      const instrumentId = byName.get(symbol)
      if (!now.has(symbol) && instrumentId !== undefined) {
        change.mutate({ kind: 'add-market', id: setup.id, instrumentId })
      }
    }
    for (const symbol of chosen) {
      const instrumentId = byName.get(symbol)
      if (!wanted.has(symbol) && instrumentId !== undefined) {
        change.mutate({ kind: 'remove-market', id: setup.id, instrumentId })
      }
    }
    setTicked(next.filter((symbol) => byName.has(symbol)))
  }

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
              change.mutate({
                kind: 'remove-market',
                id: setup.id,
                instrumentId: market.instrument_id,
              })
            }}
            aria-label={`Drop ${market.symbol}`}
            className="text-slate-500 hover:text-red-300"
          >
            ×
          </button>
        </span>
      ))}
      <button
        type="button"
        disabled={busy}
        onClick={() => {
          setBrowsing(true)
        }}
        className="rounded border border-sky-700 px-2 py-0.5 text-xs text-sky-100 hover:border-sky-500 disabled:opacity-40"
      >
        Add markets…
      </button>
      {browsing && (
        <MarketBrowser
          chosen={chosen}
          max={MAX_MARKETS}
          onChange={apply}
          onClose={() => {
            setBrowsing(false)
            setTicked(null)
          }}
        />
      )}
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

/** A setup's signals, newest first — by asset, only that asset's: the others have their blocks. */
function History(props: { setupId: string; symbol?: string }): React.JSX.Element {
  const signals = useLiveSignals(props.setupId)
  const rows = (signals.data ?? []).filter(
    (row) => props.symbol === undefined || row.symbol === props.symbol,
  )
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
