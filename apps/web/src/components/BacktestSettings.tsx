import type { Instrument } from '../api/types'
import type { BacktestForm } from '../backtest/settings'

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
  return (
    <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
      <label className="flex flex-col gap-1 text-sm">
        Symbol
        <select
          aria-label="symbol"
          className={inputClass}
          value={form.symbol}
          onChange={(event) => {
            patch({ symbol: event.target.value })
          }}
        >
          {/* Blank until the instruments arrive, so the field never shows a symbol that is really
              just the first row of a list the user never saw. */}
          <option value="">choose…</option>
          {instruments?.map((instrument) => (
            <option key={instrument.id} value={instrument.symbol}>
              {instrument.symbol}
            </option>
          ))}
        </select>
      </label>
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
          Spread (points)
          <input
            aria-label="spread points"
            type="number"
            className={inputClass}
            value={form.spreadPoints}
            onChange={(event) => {
              patch({ spreadPoints: event.target.value })
            }}
          />
        </label>
      )}
    </div>
  )
}
