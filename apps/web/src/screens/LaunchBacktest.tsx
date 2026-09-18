import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCreateBacktest, useInstruments, useStrategy } from '../api/hooks'
import {
  emptyBacktestForm,
  toBacktestRequest,
  whyNotRunnable,
  type BacktestForm,
} from '../backtest/settings'
import { useMissingDataGate } from '../collect/gate'
import { anythingToRun } from '../collect/missing'
import { BacktestSettings } from '../components/BacktestSettings'
import { MissingDataPrompt } from '../components/MissingDataPrompt'
import { StrategyPicker } from '../components/StrategyPicker'
import { useSession } from '../store'

/**
 * The chart a saved document was written for, or null when it does not say.
 *
 * ⚠️ **Read off the document, never asked for again.** This screen used to offer its own
 * timeframe field, defaulting to H1, because it held an id and not a document. That let a
 * strategy written for H4 be run on H1 — accepted by the server unless a higher-timeframe filter
 * is involved, and no longer the strategy that was built. Running one saved strategy over a
 * market is what this screen is for. ⚠️ Varying the chart is the sweep's job **only for entries
 * on the shelf**; a saved strategy kept off it can still be pointed at another chart through
 * `/basket` and `/study`, whose own timeframe fields default to H1 (in `specs/backlog.md`).
 */
function documentTimeframe(definition: Record<string, unknown> | undefined): string | null {
  const timeframe = definition?.timeframe
  return typeof timeframe === 'string' ? timeframe : null
}

/**
 * Run a saved strategy over one market and one window.
 *
 * ⚠️ **Chosen from what the server holds, not from what this tab remembers.** It used to open
 * only on the strategy saved in the current browser session and told everybody else to build
 * something first — after a reload that meant every saved strategy was unreachable from here.
 * The session still preselects the last one saved, which is the second run this screen began as.
 */
export function LaunchBacktest(): React.JSX.Element {
  const strategyId = useSession((state) => state.strategyId)
  const setStrategy = useSession((state) => state.setStrategy)
  const instruments = useInstruments()
  const opened = useStrategy(strategyId ?? undefined)
  const create = useCreateBacktest()
  const navigate = useNavigate()

  const [form, setForm] = useState<BacktestForm>(emptyBacktestForm)

  const timeframe = documentTimeframe(opened.data?.definition)

  // ⚠️ The strategy is asked about **before** the form, and each unanswered state is said as
  // itself. A document still loading and a document that failed to load both leave `timeframe`
  // null — different facts, and pooling them would call a slow network a broken document.
  //
  // A document naming no chart is **not reachable**: the schema makes `timeframe` a required
  // literal. The branch stays anyway, because of how its absence would fail — `launch` returns
  // on a null chart, so without a reason here the button would be live and the click would do
  // nothing, silently. The empty-string guard went for the opposite reason: it failed loudly.
  const noStrategy =
    strategyId === null
      ? 'choose a strategy'
      : opened.isError
        ? 'the strategy could not be read'
        : opened.data === undefined
          ? 'reading the strategy'
          : timeframe === null
            ? 'the strategy does not say which chart it was written for'
            : null
  const blocked = noStrategy ?? whyNotRunnable(form, instruments.data)

  const launch = (collectMissing = false): void => {
    if (strategyId === null || timeframe === null) return
    create.mutate(
      { ...toBacktestRequest(form, strategyId, timeframe), collect_missing: collectMissing },
      {
        onSuccess: (created) => {
          void navigate(`/results/${created.id}`)
        },
      },
    )
  }
  // ⚠️ The plan is asked with the flag off: nothing missing means launch as an ordinary run.
  const gate = useMissingDataGate(() => {
    launch()
  })

  // Asked first, launched only if nothing is missing — otherwise the prompt below decides.
  const run = (): void => {
    if (strategyId === null || timeframe === null) return
    const request = toBacktestRequest(form, strategyId, timeframe)
    gate.check({
      symbols: [request.symbol],
      timeframes: [timeframe],
      date_from: request.date_from,
      date_to: request.date_to,
    })
  }

  // A prompt answers the form it was asked about; any edit closes it.
  const edit = (next: BacktestForm): void => {
    gate.dismiss()
    setForm(next)
  }

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h2 className="text-xl font-semibold">New backtest</h2>
        <p className="text-sm text-slate-400">
          Pick a strategy saved in the catalogue, then the market and the window to run it over.
        </p>
      </header>

      <div className="space-y-4 rounded-lg border border-slate-800 bg-slate-900/40 p-4">
        <div className="flex flex-wrap items-end gap-4">
          <StrategyPicker
            value={strategyId ?? ''}
            onChange={(picked) => {
              gate.dismiss()
              setStrategy(picked.id, picked.name)
            }}
          />
          {timeframe !== null && (
            <p className="pb-1 text-sm text-slate-300">
              Chart <span className="font-mono text-sky-400">{timeframe}</span>{' '}
              <span className="text-slate-500">— as the strategy was written</span>
            </p>
          )}
        </div>

        <BacktestSettings
          form={form}
          instruments={instruments.data}
          onChange={edit}
          {...(timeframe === null ? {} : { timeframe })}
        />
      </div>

      {blocked !== null && <p className="text-sm text-amber-300">Before running: {blocked}.</p>}

      <MissingDataPrompt
        gate={gate}
        onRunAnyway={() => {
          launch()
        }}
        // The server plans, queues the downloads and links them to the run, which then starts by
        // itself. ⚠️ The screen does not send windows of its own: a client-chosen window could be
        // part of a year, and a partial year erases the rest of that year's partition.
        onCollectAndRun={() => {
          launch(true)
        }}
        launching={create.isPending}
        canRun={
          gate.missing === null ||
          // Unreachable while the prompt is open — `run` asks the plan only once the chart is
          // known — and said rather than asserted, so a null never reaches the plan's key.
          timeframe === null ||
          anythingToRun([{ symbol: form.symbol, timeframe }], gate.missing)
        }
      />

      {create.isError && (
        <p className="text-sm text-red-400">
          {apiFailure(create.error, 'Could not enqueue the backtest. Check the fields.')}
        </p>
      )}

      <button
        type="button"
        disabled={blocked !== null || create.isPending || gate.plan.isPending}
        onClick={run}
        className="rounded bg-sky-600 px-4 py-2 font-medium text-white enabled:hover:bg-sky-500 disabled:opacity-40"
      >
        {create.isPending
          ? 'Enqueuing…'
          : gate.plan.isPending
            ? 'Checking the data…'
            : 'Run backtest'}
      </button>
    </div>
  )
}
