import type { Metrics } from '../api/types'
import { percent, ratio, sign, signedMoney } from '../format'

const toneClass = { up: 'text-emerald-400', down: 'text-red-400', flat: 'text-slate-100' } as const

function Tile(props: { label: string; value: string; tone?: keyof typeof toneClass }): React.JSX.Element {
  return (
    <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
      <div className="text-xs tracking-wide text-slate-400 uppercase">{props.label}</div>
      <div className={`mt-1 text-2xl font-semibold tabular-nums ${toneClass[props.tone ?? 'flat']}`}>
        {props.value}
      </div>
    </div>
  )
}

// Stat tiles, not a chart: each is a single headline number. Only the P&L wears a status colour —
// green up, red down — because its sign is the one thing a glance should catch. Everything else
// stays in neutral ink; a wall of coloured numbers would make none of them mean anything.
export function MetricCards({ metrics }: { metrics: Metrics }): React.JSX.Element {
  return (
    <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
      <Tile label="Net profit" value={signedMoney(metrics.net_profit)} tone={sign(metrics.net_profit)} />
      <Tile label="Win rate" value={percent(metrics.win_rate)} />
      <Tile label="Profit factor" value={ratio(metrics.profit_factor)} />
      <Tile
        label="Expectancy"
        value={metrics.expectancy === null ? '—' : signedMoney(metrics.expectancy)}
      />
      <Tile label="Max drawdown" value={percent(metrics.max_drawdown_pct)} />
      <Tile label="Sharpe" value={ratio(metrics.sharpe)} />
      <Tile label="Payoff" value={ratio(metrics.payoff)} />
      <Tile label="Trades" value={String(metrics.total_trades)} />
    </div>
  )
}
