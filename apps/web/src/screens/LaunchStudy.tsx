import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { useCreateStudy, useInstruments } from '../api/hooks'
import { useSession } from '../store'
import { TIMEFRAMES } from '../strategy/builder'
import {
  MAX_POINTS,
  combinationCount,
  emptyStudyForm,
  studyLabel,
  toStudyRequest,
  whyNotLaunchable,
  type StudyForm,
} from '../study/settings'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Search a strategy's own parameters over one market, to see the shape of the result.
 *
 * The mirror of the basket screen: that one lists **markets** and holds the parameters still,
 * this one lists **parameter values** and holds the market still. Holding the market still is
 * what makes the points comparable to each other at all.
 *
 * ⚠️ **The combination count is on screen while the form is being filled in, and that is the
 * point of this layout.** A grid's size is the product of its axes, so a fourth row of five
 * values does not add five runs — it multiplies by five. People underestimate that reliably,
 * and reporting it only after the launch reports it too late to matter.
 */
export function LaunchStudy(): React.JSX.Element {
  const strategyId = useSession((state) => state.strategyId)
  const strategyName = useSession((state) => state.strategyName)
  const setStudy = useSession((state) => state.setStudy)
  const instruments = useInstruments()
  const create = useCreateStudy()
  const navigate = useNavigate()

  const [form, setForm] = useState<StudyForm>(emptyStudyForm)

  if (strategyId === null || strategyName === null) {
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

  const blocked = whyNotLaunchable(form)
  const total = combinationCount(form)

  const set = (patch: Partial<StudyForm>) => {
    setForm((current) => ({ ...current, ...patch }))
  }
  const setAxis = (at: number, patch: Partial<StudyForm['axes'][number]>) => {
    setForm((current) => ({
      ...current,
      axes: current.axes.map((axis, index) => (index === at ? { ...axis, ...patch } : axis)),
    }))
  }

  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Parameter study</h2>
        <p className="mt-1 text-sm text-slate-400">
          Run <span className="text-slate-200">{strategyName}</span> once for every combination of
          the parameters below, over one market.
        </p>
      </header>

      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault()
          if (blocked !== null) return
          create.mutate(toStudyRequest(form, strategyId), {
            onSuccess: (created) => {
              setStudy(created.id, studyLabel(form))
              void navigate(`/studies/${created.id}`)
            },
          })
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            Market
            <select
              className={inputClass}
              value={form.symbol}
              onChange={(event) => {
                set({ symbol: event.target.value })
              }}
            >
              <option value="">Choose…</option>
              {(instruments.data ?? []).map((instrument) => (
                <option key={instrument.id} value={instrument.symbol}>
                  {instrument.symbol}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-sm text-slate-300">
            Timeframe
            <select
              className={inputClass}
              value={form.timeframe}
              onChange={(event) => {
                set({ timeframe: event.target.value })
              }}
            >
              {TIMEFRAMES.map((timeframe) => (
                <option key={timeframe} value={timeframe}>
                  {timeframe}
                </option>
              ))}
            </select>
          </label>

          <label className="flex flex-col gap-1 text-sm text-slate-300">
            Initial capital
            <input
              className={inputClass}
              value={form.initialCapital}
              onChange={(event) => {
                set({ initialCapital: event.target.value })
              }}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm text-slate-300">
            From
            <input
              type="date"
              className={inputClass}
              value={form.dateFrom}
              onChange={(event) => {
                set({ dateFrom: event.target.value })
              }}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm text-slate-300">
            To
            <input
              type="date"
              className={inputClass}
              value={form.dateTo}
              onChange={(event) => {
                set({ dateTo: event.target.value })
              }}
            />
          </label>

          <label className="flex flex-col gap-1 text-sm text-slate-300">
            Spread (ticks)
            <input
              className={inputClass}
              placeholder="none"
              value={form.spreadTicks}
              onChange={(event) => {
                set({ spreadTicks: event.target.value })
              }}
            />
            <span className="text-xs text-slate-500">
              One cost for every point, or the runs are not comparable.
            </span>
          </label>
        </div>

        <fieldset className="space-y-2 rounded border border-slate-800 p-4">
          <legend className="px-1 text-sm text-slate-300">Parameters to vary</legend>
          {form.axes.map((axis, at) => (
            <div key={at} className="flex flex-wrap items-center gap-2">
              <input
                className={`${inputClass} min-w-64 flex-1`}
                placeholder="setup.params.period"
                aria-label={`Parameter ${String(at + 1)} path`}
                value={axis.path}
                onChange={(event) => {
                  setAxis(at, { path: event.target.value })
                }}
              />
              <input
                className={`${inputClass} min-w-48 flex-1`}
                placeholder="5, 9, 20"
                aria-label={`Parameter ${String(at + 1)} values`}
                value={axis.raw}
                onChange={(event) => {
                  setAxis(at, { raw: event.target.value })
                }}
              />
            </div>
          ))}
          <button
            type="button"
            className="text-xs text-sky-400 hover:text-sky-300"
            onClick={() => {
              set({ axes: [...form.axes, { path: '', raw: '' }] })
            }}
          >
            Add a parameter
          </button>
        </fieldset>

        <div className="flex flex-wrap items-center gap-4">
          <button
            type="submit"
            disabled={blocked !== null || create.isPending}
            className="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-700"
          >
            {create.isPending ? 'Launching…' : 'Run the study'}
          </button>
          {/* The count is a live reading, not a validation message: it is worth seeing at 12 as
              well as at 600, because the jump between them is what people misjudge. */}
          <p className="text-sm text-slate-300" role="status">
            {total === 0
              ? 'Nothing to run yet.'
              : `${String(total)} combination${total === 1 ? '' : 's'}, so ${String(total)} backtest${total === 1 ? '' : 's'}.`}
          </p>
        </div>

        {blocked !== null && total > 0 && <p className="text-sm text-amber-300">{blocked}</p>}
        {create.isError && (
          <p className="text-sm text-red-400">{create.error.message || 'The study was refused.'}</p>
        )}
      </form>

      <p className="max-w-3xl text-xs text-slate-500">
        A grid always has a best point — a grid of pure noise has a best point. What a study can
        tell you is whether the good results form a broad region or a single lucky cell, and how
        much of the space works at all. It cannot tell you the winning parameters will work next
        month: every figure it produces is measured on the same data it searched. Up to{' '}
        {String(MAX_POINTS)} combinations.
      </p>
    </section>
  )
}
