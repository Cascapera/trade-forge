import type { Trade } from '../api/types'
import { money, ratio, sign, signedMoney } from '../format'

const netClass = { up: 'text-emerald-400', down: 'text-red-400', flat: 'text-slate-300' } as const

function Cell({ children }: { children: React.ReactNode }): React.JSX.Element {
  return <td className="px-3 py-2 tabular-nums">{children}</td>
}

export function TradesTable({ trades }: { trades: Trade[] }): React.JSX.Element {
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
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800">
          {trades.map((trade) => (
            <tr key={trade.id}>
              <Cell>{trade.direction}</Cell>
              <Cell>{money(trade.entry_price)}</Cell>
              <Cell>{trade.exit_price === null ? '—' : money(trade.exit_price)}</Cell>
              <Cell>{trade.exit_reason ?? '—'}</Cell>
              <td className={`px-3 py-2 tabular-nums ${netClass[trade.net_pnl === null ? 'flat' : sign(trade.net_pnl)]}`}>
                {trade.net_pnl === null ? '—' : signedMoney(trade.net_pnl)}
              </td>
              <Cell>{trade.r_multiple === null ? '—' : `${ratio(trade.r_multiple)}R`}</Cell>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
