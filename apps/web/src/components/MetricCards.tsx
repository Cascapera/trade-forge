import type { Metrics } from '../api/types'
import { count, duration, money, percent, ratio, sign, signedMoney } from '../format'

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
//
// ⚠️ **Two groups, because seventeen tiles in one grid is a wall nobody reads.** The first eight
// are the ones a run is judged by at a glance. The rest answer the questions that come *after*
// that glance — where the net came from, whether the method is one-sided, how long the pain
// lasted — and every one of them was already being computed and thrown away.
export function MetricCards({ metrics }: { metrics: Metrics }): React.JSX.Element {
  return (
    <div className="space-y-4">
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
        <Tile label="Trades" value={count(metrics.total_trades)} />
      </div>

      <section className="space-y-2">
        {/* ⚠️ `h3`, not `h4`. The screens that render this put an `h2` above and their own
              `h3`s below — "Equity curve", "Trades" — so this group is their sibling, and
              an `h4` both skips a level and claims to be subordinate to a heading that
              comes after it. */}
          <h3 className="text-xs tracking-wide text-slate-500 uppercase">The rest of it</h3>
        <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-4">
          {/* ⚠️ Side by side on purpose. A net of +2,000 is a different result when it is
              10,000 won against 8,000 lost than when it is 2,100 against 100, and the net on
              its own cannot tell those apart. */}
          {/* ⚠️ `money`, not `signedMoney`. These two carry their sign structurally — one is
              never negative and the other never positive — so a forced `+` adds nothing, and on
              a run with no losing trades `gross_loss` is exactly zero, where `signedMoney` reads
              `+0.00`: a plus on the one number the engine documents as `<= 0`. */}
          <Tile label="Gross profit" value={money(metrics.gross_profit)} />
          <Tile label="Gross loss" value={money(metrics.gross_loss)} />
          {/* Likewise: a method that only ever went long is a different claim from one tested
              on both sides, and the trade count alone hides which was run. */}
          <Tile label="Long trades" value={count(metrics.long_trades)} />
          <Tile label="Short trades" value={count(metrics.short_trades)} />
          {/* The drawdown twice, in the two units it is felt in. The percentage is the headline
              above; this is what it cost. */}
          <Tile label="Drawdown in cash" value={money(metrics.max_drawdown_abs)} />
          {/* Peak to the bar that climbs back above it — or to the end of the run, when it never
              did. Depth is what people quote and duration is what they sit through.

              ⚠️ The API sends whole days, **truncated**, so every intraday run reports `0` — and
              a bare `0 d` reads as "never underwater", which is a different claim from "under
              water for less than a day". The drawdown beside it is what tells those apart. */}
          <Tile
            label="Longest underwater"
            value={
              metrics.max_dd_duration_days === 0 && Number(metrics.max_drawdown_abs) > 0
                ? '< 1 d'
                : `${count(metrics.max_dd_duration_days)} d`
            }
          />
          <Tile label="Sortino" value={ratio(metrics.sortino)} />
          <Tile label="CAGR" value={percent(metrics.cagr)} />
          <Tile label="Average trade" value={duration(metrics.avg_trade_duration)} />
        </div>
        {/* ⚠️ Said out loud because an em dash on its own reads as "missing", and this one is a
            refusal: annualising four months of trading produces a number that is enormous and
            means nothing.

            ⚠️ **Both conditions, because there are two and the second one matters more.** The
            engine also declines to annualise a run whose equity ended at or below zero — there
            is no growth rate through ruin — and a note naming only the span would print the
            wrong explanation on exactly the result nobody should misread. */}
        <p className="text-xs text-slate-500">
          CAGR needs at least a year of history and equity still above zero at the end.
        </p>
      </section>
    </div>
  )
}
