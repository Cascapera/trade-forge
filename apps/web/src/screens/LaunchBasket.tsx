import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateBasket, useInstruments } from '../api/hooks'
import {
  basketLabel,
  emptyBasketForm,
  launchFailure,
  toBasketRequest,
  toggleSymbol,
  uncostedAmong,
  whyNotLaunchable,
  type BasketForm,
} from '../basket/settings'
import { StrategyPicker } from '../components/StrategyPicker'
import { SymbolPicker } from '../components/SymbolPicker'
import { useSession } from '../store'
import { TIMEFRAMES } from '../strategy/builder'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Run one strategy over several markets at once, to see whether it travels.
 *
 * The experiment this screen exists for: a strategy tuned on one instrument's history and never
 * tried elsewhere is indistinguishable from a strategy *fitted* to that instrument's history.
 * Same strategy, same window, same capital, N markets, results side by side.
 *
 * ⚠️ **There is no cost control here, unlike the single-backtest screen.** Each run is charged
 * its own instrument's measured spread, decided by the server — one figure across a basket would
 * be meaningless, since 8 ticks of EURUSD and 4 of AAPL are counted in tick sizes three orders of
 * magnitude apart. So the picker previews what each market will pay, and the warning below names
 * the ones nobody has measured, which run uncosted.
 */
export function LaunchBasket(): React.JSX.Element {
  const strategyId = useSession((state) => state.strategyId)
  const strategyName = useSession((state) => state.strategyName)
  const setBasket = useSession((state) => state.setBasket)
  const setStrategy = useSession((state) => state.setStrategy)
  const instruments = useInstruments()
  const create = useCreateBasket()
  const navigate = useNavigate()

  const [form, setForm] = useState<BasketForm>(emptyBasketForm)
  const [timeframe, setTimeframe] = useState('H1')

  // ⚠️ This screen used to refuse to open unless a strategy had been created since the last
  // reload, because there was no way to ask the server what existed — for a database holding
  // forty-five of them. The picker is what `GET /strategies` was missing for.
  const blocked = strategyId === null ? 'choose a strategy' : whyNotLaunchable(form)
  const uncosted = uncostedAmong(form.symbols, instruments.data)

  const launch = (): void => {
    if (strategyId === null || strategyName === null) return
    create.mutate(toBasketRequest(form, strategyId, timeframe), {
      onSuccess: (created) => {
        setBasket(created.id, basketLabel(strategyName, created.runs.length))
        void navigate(`/baskets/${created.id}`)
      },
    })
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">
          Basket of <span className="text-sky-400">{strategyName ?? 'a strategy'}</span>
        </h2>
        <p className="text-sm text-slate-400">
          One strategy, several markets, one run each — to see whether it travels.
        </p>
      </div>

      <StrategyPicker
        value={strategyId ?? ''}
        onChange={(id, name) => {
          setStrategy(id, name)
        }}
      />

      <div className="space-y-4 rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <SymbolPicker
          instruments={instruments.data}
          chosen={form.symbols}
          onToggle={(symbol) => {
            setForm(toggleSymbol(form, symbol))
          }}
        />

        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          <label className="flex flex-col gap-1 text-sm">
            Timeframe
            <select
              aria-label="timeframe"
              className={inputClass}
              value={timeframe}
              onChange={(event) => {
                setTimeframe(event.target.value)
              }}
            >
              {TIMEFRAMES.map((tf) => (
                <option key={tf} value={tf}>
                  {tf}
                </option>
              ))}
            </select>
          </label>
          <label className="flex flex-col gap-1 text-sm">
            Initial capital
            <input
              aria-label="capital"
              type="number"
              className={inputClass}
              value={form.capital}
              onChange={(event) => {
                setForm({ ...form, capital: event.target.value })
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
                setForm({ ...form, dateFrom: event.target.value })
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
                setForm({ ...form, dateTo: event.target.value })
              }}
            />
          </label>
        </div>

        {/* Every run in a basket starts with this same balance, which is what makes the returns
            comparable by construction rather than by the reader remembering to check. */}
        <p className="text-xs text-slate-500">
          Each market runs on its own {form.capital} — the runs are independent, not one shared
          account, so their curves are never added together.
        </p>
      </div>

      {uncosted.length > 0 && (
        <p
          role="status"
          className="rounded border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200"
        >
          <strong>{uncosted.join(', ')}</strong>{' '}
          {uncosted.length === 1 ? 'has' : 'have'} no measured spread, so{' '}
          {uncosted.length === 1 ? 'that run' : 'those runs'} will charge nothing. That is not the
          same as trading free — nobody has measured{' '}
          {uncosted.length === 1 ? 'it' : 'them'} yet — and{' '}
          {uncosted.length === 1 ? 'its result is an upper bound' : 'their results are upper bounds'}
          , not comparable with the markets in this basket that do pay a spread.
        </p>
      )}

      {blocked !== null && <p className="text-sm text-amber-300">Before running: {blocked}.</p>}

      {create.isError && <p className="text-sm text-red-400">{launchFailure(create.error)}</p>}

      <button
        type="button"
        disabled={blocked !== null || create.isPending}
        onClick={launch}
        className="rounded bg-sky-600 px-4 py-2 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
      >
        {create.isPending
          ? 'Enqueuing…'
          : `Run ${String(form.symbols.length)} market${form.symbols.length === 1 ? '' : 's'}`}
      </button>
    </div>
  )
}
