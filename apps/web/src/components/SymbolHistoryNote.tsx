import { ApiError } from '../api/client'
import { useProbeSymbol, useSymbolHistory } from '../api/hooks'
import type { SymbolHistory } from '../api/types'

/**
 * How much history this series really has, and what is bounding the answer.
 *
 * ## Why this is several sentences and not a date
 *
 * "Available from 2009" is four different claims wearing one hat, and only one of them is ever
 * the binding one. Measured on EURUSD D1: the terminal's ceiling, the bars nobody traded and the
 * spread that was typed rather than observed give 1971, 1972 and 2009. A reader fixes the first
 * in a settings dialog, the second by starting later, the third by not trusting old costs at
 * all — and a single date makes all three of those unavailable.
 *
 * ## The one it cannot answer
 *
 * Whether the *instrument* is real. EURUSD's fabricated bars stop in 1973 and the euro dates
 * from 1999, so 1973 to 1998 is a reconstruction carrying plausible prices and volumes that no
 * property of a bar distinguishes from a market. The note says so where it matters rather than
 * letting "usable from" be read as a guarantee.
 */
export function SymbolHistoryNote(props: {
  symbol: string
  timeframe: string | undefined
}): React.JSX.Element | null {
  const { symbol, timeframe } = props
  const history = useSymbolHistory(symbol, timeframe)
  const probe = useProbeSymbol()

  if (symbol === '' || timeframe === undefined) return null

  const neverProbed = history.error instanceof ApiError && history.error.status === 404
  const measure = (): void => {
    probe.mutate({ symbol, timeframe })
  }

  // ⚠️ A read that failed is not a series nobody has measured, and the difference decides
  // whether clicking helps. Without this branch the 404 check below is unobservable — any error
  // leaves `data` undefined and falls into the invitation — so a broken API would answer "nobody
  // has measured how much D1 history EURUSD has", which is a claim about the data, from a request
  // that never reached it.
  if (history.error != null && !neverProbed) {
    return (
      <p className="col-span-full text-xs text-amber-300">
        ⚠️ could not read what has been measured for {symbol} {timeframe}. This is the read
        failing, not the series being empty — measuring again will not fix it.
      </p>
    )
  }

  if (neverProbed || history.data === undefined) {
    return (
      <p className="col-span-full flex items-center gap-2 text-xs text-slate-400">
        <span>
          {probe.isPending || probe.isSuccess
            ? `measuring ${symbol} ${timeframe}… this can take minutes the first time`
            : `nobody has measured how much ${timeframe} history ${symbol} has`}
        </span>
        {!probe.isPending && !probe.isSuccess && (
          <button
            type="button"
            className="rounded border border-slate-700 px-2 py-0.5 hover:border-sky-500"
            onClick={measure}
          >
            measure it
          </button>
        )}
      </p>
    )
  }

  return (
    <div className="col-span-full space-y-1 rounded border border-slate-800 bg-slate-900/60 p-3 text-xs">
      <p className="text-slate-300">{summary(history.data)}</p>
      {warnings(history.data).map((warning) => (
        <p key={warning} className="text-amber-300">
          ⚠️ {warning}
        </p>
      ))}
    </div>
  )
}

function summary(history: SymbolHistory): string {
  if (history.bar_count === 0) return `${history.symbol}: this terminal has no ${history.timeframe} bars at all`
  const bars = history.bar_count.toLocaleString()
  // The count *and* the start, because neither alone is actionable: "since 2009" says nothing
  // about how much signal that is, and "4,300 bars" says nothing about which market they are.
  const from = history.usable_from === null ? 'an unknown start' : day(history.usable_from)
  return `${bars} bars available · usable from ${from}`
}

function warnings(history: SymbolHistory): string[] {
  const notes: string[] = []

  if (history.bar_count_is_a_ceiling) {
    // Seen for real at exactly 10,000,000. Named first because it makes every other number on
    // this line unreliable rather than merely bounded.
    notes.push(
      `the count above is where the measurement stopped, not where the data does — ${history.symbol} has more than this`,
    )
  }

  if (history.capped_by_terminal) {
    notes.push(
      `limited by your terminal, not your broker: Max bars in chart is ${history.terminal_maxbars.toLocaleString()}. Raise it in Tools → Options → Charts and measure again`,
    )
  }

  if (history.last_fabricated !== null) {
    notes.push(
      `bars up to ${String(history.last_fabricated)} have no range and no ticks — nobody traded them, so no stop is ever hit inside one`,
    )
    // ⚠️ Shown only alongside the filler, because that is where it is load-bearing: a series
    // with fabricated bars usually has a reconstructed era after them that looks like a market.
    notes.push(
      'and a reconstruction with plausible prices is invisible to this measurement — check when the instrument was actually listed before trusting the early years',
    )
  }

  if (history.first_measured_cost !== null) {
    notes.push(
      `before ${String(history.first_measured_cost)} the broker stamped one spread across each whole year: costs there were typed, not observed`,
    )
  }

  return notes
}

/** Just the date. The time of a yearly floor is noise, and `oldest` is a bar's opening instant. */
function day(instant: string): string {
  return instant.slice(0, 10)
}
