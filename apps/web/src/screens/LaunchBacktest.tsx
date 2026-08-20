import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { useCreateBacktest, useInstruments } from '../api/hooks'
import {
  emptyBacktestForm,
  toBacktestRequest,
  whyNotRunnable,
  type BacktestForm,
} from '../backtest/settings'
import { BacktestSettings } from '../components/BacktestSettings'
import { useSession } from '../store'
import { TIMEFRAMES } from '../strategy/builder'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Re-run a strategy already saved this session, over a different instrument or window.
 *
 * The builder saves and runs in one go; this exists for the second run, which needs no new version
 * of the strategy — the document has not changed, only where it is pointed. That is also why the
 * timeframe is asked for here: this screen holds an id, not a document, so it cannot read the one
 * the strategy was written for.
 */
export function LaunchBacktest(): React.JSX.Element {
  const strategyId = useSession((state) => state.strategyId)
  const strategyName = useSession((state) => state.strategyName)
  const instruments = useInstruments()
  const create = useCreateBacktest()
  const navigate = useNavigate()

  const [form, setForm] = useState<BacktestForm>(emptyBacktestForm)
  const [timeframe, setTimeframe] = useState('H1')

  if (strategyId === null) {
    return (
      <p className="text-sm text-slate-300">
        Build and run a strategy first.{' '}
        <Link to="/" className="text-sky-400 hover:text-sky-300">
          Go to the builder
        </Link>
        .
      </p>
    )
  }

  const blocked = whyNotRunnable(form, instruments.data)

  const launch = (): void => {
    create.mutate(toBacktestRequest(form, strategyId, timeframe), {
      onSuccess: (created) => {
        void navigate(`/results/${created.id}`)
      },
    })
  }

  return (
    <div className="space-y-6">
      <h2 className="text-xl font-semibold">
        Backtest <span className="text-sky-400">{strategyName}</span>
      </h2>

      <div className="rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <BacktestSettings
          form={form}
          instruments={instruments.data}
          onChange={setForm}
          timeframe={timeframe}
        >
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
        </BacktestSettings>
      </div>

      {blocked !== null && <p className="text-sm text-amber-300">Before running: {blocked}.</p>}

      {create.isError && (
        <p className="text-sm text-red-400">Could not enqueue the backtest. Check the fields.</p>
      )}

      <button
        type="button"
        disabled={blocked !== null || create.isPending}
        onClick={launch}
        className="rounded bg-sky-600 px-4 py-2 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
      >
        {create.isPending ? 'Enqueuing…' : 'Run backtest'}
      </button>
    </div>
  )
}
