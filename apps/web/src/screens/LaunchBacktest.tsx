import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { useCreateBacktest, useInstruments } from '../api/hooks'
import { useSession } from '../store'
import { TIMEFRAMES } from '../strategy/builder'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

interface LaunchForm {
  symbol: string
  timeframe: string
  dateFrom: string
  dateTo: string
  capital: string
  cost: 'none' | 'spread'
  spreadPoints: string
}

function toIso(date: string): string {
  // A date input gives `YYYY-MM-DD`; the API wants an instant. Midnight UTC is the honest
  // reading of "this day" for a backtest window.
  return `${date}T00:00:00Z`
}

export function LaunchBacktest(): React.JSX.Element {
  const strategyId = useSession((state) => state.strategyId)
  const strategyName = useSession((state) => state.strategyName)
  const instruments = useInstruments()
  const create = useCreateBacktest()
  const navigate = useNavigate()

  const [form, setForm] = useState<LaunchForm>({
    symbol: '',
    timeframe: 'H1',
    dateFrom: '2024-01-01',
    dateTo: '2024-12-31',
    capital: '10000',
    cost: 'none',
    spreadPoints: '10',
  })

  if (strategyId === null) {
    return (
      <p className="text-sm text-slate-300">
        Build and save a strategy first.{' '}
        <Link to="/" className="text-sky-400 hover:text-sky-300">
          Go to the builder
        </Link>
        .
      </p>
    )
  }

  const patch = (update: Partial<LaunchForm>): void => {
    setForm({ ...form, ...update })
  }

  const symbol = form.symbol !== '' ? form.symbol : (instruments.data?.[0]?.symbol ?? '')

  const launch = (): void => {
    const costModel =
      form.cost === 'spread'
        ? { type: 'spread', spread_points: Number(form.spreadPoints) }
        : { type: 'none' }
    create.mutate(
      {
        strategy_id: strategyId,
        symbol,
        timeframe: form.timeframe,
        date_from: toIso(form.dateFrom),
        date_to: toIso(form.dateTo),
        initial_capital: form.capital,
        cost_model: costModel,
      },
      {
        onSuccess: (created) => {
          void navigate(`/results/${created.id}`)
        },
      },
    )
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">
        Backtest <span className="text-sky-400">{strategyName}</span>
      </h2>

      <div className="grid grid-cols-2 gap-4 rounded-lg border border-slate-800 bg-slate-900/40 p-4 sm:grid-cols-3">
        <label className="flex flex-col gap-1 text-sm">
          Symbol
          <select
            aria-label="symbol"
            className={inputClass}
            value={symbol}
            onChange={(event) => {
              patch({ symbol: event.target.value })
            }}
          >
            {instruments.data?.map((instrument) => (
              <option key={instrument.id} value={instrument.symbol}>
                {instrument.symbol}
              </option>
            ))}
          </select>
        </label>
        <label className="flex flex-col gap-1 text-sm">
          Timeframe
          <select
            aria-label="timeframe"
            className={inputClass}
            value={form.timeframe}
            onChange={(event) => {
              patch({ timeframe: event.target.value })
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
              patch({ cost: event.target.value as LaunchForm['cost'] })
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

      {create.isError && (
        <p className="text-sm text-red-400">Could not enqueue the backtest. Check the fields.</p>
      )}

      <button
        type="button"
        disabled={symbol === '' || create.isPending}
        onClick={launch}
        className="rounded bg-sky-600 px-4 py-2 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
      >
        {create.isPending ? 'Enqueuing…' : 'Run backtest'}
      </button>
    </div>
  )
}
