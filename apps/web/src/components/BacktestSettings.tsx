import type { Instrument } from '../api/types'
import { SymbolCombobox } from './SymbolCombobox'
import { costlessReason, withInstrumentCosts, type BacktestForm } from '../backtest/settings'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Where and when to run, shared by the builder and the standalone launch screen.
 *
 * The timeframe is not here: it belongs to the strategy document, and whoever owns that renders it.
 * `children` is the slot right after the instrument, so a screen that has no strategy in hand — the
 * launch route, which knows only an id — can put its own timeframe field where it reads naturally.
 */
export function BacktestSettings(props: {
  form: BacktestForm
  instruments: Instrument[] | undefined
  onChange: (next: BacktestForm) => void
  children?: React.ReactNode
}): React.JSX.Element {
  const { form, instruments, onChange, children } = props
  const patch = (update: Partial<BacktestForm>): void => {
    onChange({ ...form, ...update })
  }
  const chosen = instruments?.find((instrument) => instrument.symbol === form.symbol)
  const costless = costlessReason(form, chosen)
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
      {/* A combobox over the *broker's* catalogue, not a select over what has been collected.
          The old list was `GET /instruments` — measured on 19/08/2026, that was 1 symbol against
          the 84 the account can actually see. */}
      <SymbolCombobox
        value={form.symbol}
        onChange={(symbol) => {
          // Choosing an instrument chooses its costs too. The spread is a property of the
          // symbol, not of the run, so leaving the previous instrument's number behind — or
          // leaving `none` behind, which is what produced this project's costless history —
          // would be the form quietly keeping an answer to a question that just changed.
          const next = instruments?.find((instrument) => instrument.symbol === symbol)
          onChange(withInstrumentCosts({ ...form, symbol }, next))
        }}
      />
      {children}
      <label className="flex flex-col gap-1 text-sm">
        Initial capital
        <input
          aria-label="capital"
          type="number"
          className={inputClass}
          value={form.capital}
          onChange={(event) => {
            patch({ capital: event.target.value })
          }}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        From
        <input
          aria-label="from"
          type="date"
          className={inputClass}
          value={form.dateFrom}
          onChange={(event) => {
            patch({ dateFrom: event.target.value })
          }}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        To
        <input
          aria-label="to"
          type="date"
          className={inputClass}
          value={form.dateTo}
          onChange={(event) => {
            patch({ dateTo: event.target.value })
          }}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm">
        Costs
        <select
          aria-label="cost model"
          className={inputClass}
          value={form.cost}
          onChange={(event) => {
            patch({ cost: event.target.value as BacktestForm['cost'] })
          }}
        >
          <option value="none">none</option>
          <option value="spread">spread</option>
        </select>
      </label>
      {form.cost === 'spread' && (
        <label className="flex flex-col gap-1 text-sm">
          Spread (ticks)
          <input
            aria-label="spread points"
            type="number"
            className={inputClass}
            value={form.spreadPoints}
            onChange={(event) => {
              patch({ spreadPoints: event.target.value })
            }}
          />
          {chosen?.default_spread_points != null && (
            <span className="text-xs text-slate-400">
              {chosen.symbol} quotes {String(Number(chosen.default_spread_points))}
            </span>
          )}
        </label>
      )}
      {costless !== null && (
        <p
          role="status"
          className="col-span-full rounded border border-amber-800 bg-amber-950/40 p-3 text-xs text-amber-200"
        >
          This run charges no spread and no commission — {costless}. Its curve is an upper
          bound, not a P&amp;L, and it is not comparable with a run that paid costs.
        </p>
      )}
    </div>
  )
}
