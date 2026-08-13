import { useState } from 'react'

import { useTradeSnapshot } from '../api/hooks'
import type { Trade } from '../api/types'
import { money, ratio, sign, signedMoney } from '../format'
import { TradeSnapshot } from './TradeSnapshot'

const netClass = { up: 'text-emerald-400', down: 'text-red-400', flat: 'text-slate-300' } as const

const COLUMNS = 7

function Cell({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <td className="px-3 py-2 tabular-nums">{children}</td>
}

/**
 * The row that opens under a trade, holding its entry chart.
 *
 * A component of its own so the fetch lives inside it: mounted only when the row is open, which
 * is what makes "only the ones I want" true at the network layer rather than just visually. A
 * screen full of these would otherwise pull every window on load and discard almost all of them.
 */
function SnapshotRow({
  backtestId,
  trade,
}: {
  backtestId: string
  trade: Trade
}): React.JSX.Element {
  const { data, isPending, isError, error } = useTradeSnapshot(backtestId, trade.id)

  return (
    <tr className="bg-slate-950/40">
      <td colSpan={COLUMNS} className="px-3 py-4">
        {isPending && <p className="text-sm text-slate-400">Loading the entry…</p>}
        {isError && (
          <p className="text-sm text-red-400">
            Could not load this entry: {error instanceof Error ? error.message : 'unknown error'}
          </p>
        )}
        {data !== undefined && (
          <div className="space-y-2">
            <TradeSnapshot
              snapshot={data}
              entryPrice={trade.entry_price}
              stopLoss={trade.stop_loss}
              context={trade.context}
            />
            <p className="text-xs text-slate-500">
              {data.bars.length} bars around the entry, recorded by the engine when it decided.
              Hollow candles close up, filled close down. The x axis counts bars, not clock time.
            </p>
          </div>
        )}
      </td>
    </tr>
  )
}

export function TradesTable({
  trades,
  backtestId,
  onLocate,
}: {
  trades: Trade[]
  backtestId: string
  /**
   * Take the reader to this trade on the run's price chart.
   *
   * Optional, and absent is the honest default: the run log renders this same table for runs
   * whose candles it never fetched, and a button that navigates nowhere is worse than no button.
   * The results screen passes it because it owns both the chart and the tab holding it.
   */
  onLocate?: (tradeId: number) => void
}): React.JSX.Element {
  // One open at a time. Several charts at once turns a table you scan into a page you scroll,
  // and the question being asked here is about one entry.
  const [openTrade, setOpenTrade] = useState<number | null>(null)

  if (trades.length === 0) {
    return <p className="text-sm text-slate-400">This run produced no trades.</p>
  }
  return (
    <div className="overflow-x-auto rounded-lg border border-slate-800">
      <table className="min-w-full text-left text-sm">
        <thead className="bg-slate-900/60 text-xs text-slate-400 uppercase">
          <tr>
            <th className="px-3 py-2">Side</th>
            <th className="px-3 py-2">Entry</th>
            <th className="px-3 py-2">Exit</th>
            <th className="px-3 py-2">Reason</th>
            <th className="px-3 py-2">Net</th>
            <th className="px-3 py-2">R</th>
            <th className="px-3 py-2 text-right">Chart</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {trades.map((trade) => {
            const open = openTrade === trade.id
            return (
              <>
                <tr key={trade.id}>
                  <Cell>{trade.direction}</Cell>
                  <Cell>{money(trade.entry_price)}</Cell>
                  <Cell>{trade.exit_price === null ? '—' : money(trade.exit_price)}</Cell>
                  <Cell>{trade.exit_reason ?? '—'}</Cell>
                  <td
                    className={`px-3 py-2 tabular-nums ${netClass[trade.net_pnl === null ? 'flat' : sign(trade.net_pnl)]}`}
                  >
                    {trade.net_pnl === null ? '—' : signedMoney(trade.net_pnl)}
                  </td>
                  <Cell>{trade.r_multiple === null ? '—' : `${ratio(trade.r_multiple)}R`}</Cell>
                  <td className="px-3 py-2 text-right whitespace-nowrap">
                    {/* ⚠️ Every accessible name here names *this* trade. A research run enters the
                        same strategy on the same instrument dozens of times, so "Show on the price
                        chart" repeated down the column would be several controls sharing one name
                        — which a screen reader cannot tell apart, and which is a defect in the
                        page rather than an inconvenience in a test. */}
                    {onLocate && (
                      <button
                        type="button"
                        aria-label={`Show the ${trade.direction} entered at ${trade.entry_time} on the price chart`}
                        onClick={() => {
                          onLocate(trade.id)
                        }}
                        className="mr-1 rounded border border-slate-700 px-2 py-1 text-slate-300 hover:border-slate-500 hover:text-slate-100 focus-visible:outline-2 focus-visible:outline-sky-400"
                      >
                        <svg
                          viewBox="0 0 16 16"
                          className="h-4 w-4"
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          aria-hidden="true"
                        >
                          {/* A target: "take me to this one", not "show me a chart" — the row
                              already has a button for the second thing. */}
                          <circle cx="8" cy="8" r="4.5" />
                          <path d="M8 1v2M8 13v2M1 8h2M13 8h2" strokeLinecap="round" />
                        </svg>
                      </button>
                    )}
                    {/* The button is the affordance. Making the whole row clickable would mean a
                        row that responds to a click without looking like it does — guesswork.
                        Absent, not disabled, when there is no window: a control that can never
                        do anything is noise, and runs older than the feature have none. */}
                    {trade.has_snapshot && (
                      <button
                        type="button"
                        aria-expanded={open}
                        aria-label={open ? 'Hide the entry chart' : 'Show the entry chart'}
                        onClick={() => {
                          setOpenTrade(open ? null : trade.id)
                        }}
                        className="rounded border border-slate-700 px-2 py-1 text-slate-300 hover:border-slate-500 hover:text-slate-100 focus-visible:outline-2 focus-visible:outline-sky-400"
                      >
                        <svg
                          viewBox="0 0 16 16"
                          className={`h-4 w-4 ${open ? 'text-sky-400' : ''}`}
                          fill="none"
                          stroke="currentColor"
                          strokeWidth="1.5"
                          aria-hidden="true"
                        >
                          {/* A candle with a wick — the thing the button opens, not a generic
                              chevron, so the icon says what it shows. */}
                          <path d="M4 2v12M4 5h0M8 1v14M12 4v10" strokeLinecap="round" />
                          <rect x="2.2" y="5" width="3.6" height="6" rx="0.5" />
                          <rect x="6.2" y="3.5" width="3.6" height="9" rx="0.5" />
                          <rect x="10.2" y="6.5" width="3.6" height="5" rx="0.5" />
                        </svg>
                      </button>
                    )}
                  </td>
                </tr>
                {open && (
                  <SnapshotRow key={`${String(trade.id)}-chart`} backtestId={backtestId} trade={trade} />
                )}
              </>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}
