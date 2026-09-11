import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCreateStudy, useInstruments, useStrategies } from '../api/hooks'
import { GridEditor } from '../components/GridEditor'
import { StrategyPicker } from '../components/StrategyPicker'
import { useSession } from '../store'
import { TIMEFRAMES } from '../strategy/builder'
import { useGridPreview } from '../study/preview'
import {
  MAX_POINTS,
  combinationCount,
  emptyStudyForm,
  launchFailure,
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
  const setStudy = useSession((state) => state.setStudy)
  const setStrategy = useSession((state) => state.setStrategy)
  const instruments = useInstruments()
  const create = useCreateStudy()
  const navigate = useNavigate()

  const [form, setForm] = useState<StudyForm>(emptyStudyForm)
  // ⚠️ **Derived from the list, never held as state.** It was state, set only inside the
  // picker's `onChange` — so a screen opened with a strategy *already* chosen (from the
  // builder, or from an earlier visit in this session) showed it selected while knowing
  // nothing about its setup, and fell back to the free-text field. The selection and what is
  // known about it cannot disagree if only one of them exists. The query is React Query's, so
  // this shares the picker's single request rather than making a second one.
  const strategies = useStrategies({ limit: 200 })
  const chosen = strategies.data?.items.find((item) => item.id === strategyId)

  // ⚠️ Two refusals, from two places, and they answer different questions. `whyNotLaunchable`
  // knows what this form can decide on its own — a missing market, a repeated value, a product
  // over the cap. The preview knows what only the DSL's semantics can say, and it is a round
  // trip away. Neither can be folded into the other without moving a rule to the wrong side.
  const preview = useGridPreview(strategyId, form)
  const local = strategyId === null ? 'Choose a strategy.' : whyNotLaunchable(form)
  const refused = !preview.settled
    ? null
    : (preview.gridError ??
      (preview.refusals.length === 0
        ? null
        : `${String(preview.refusals.length)} of these combinations cannot run, so the study will not start.`))
  // ⚠️ Shown side by side rather than one winning: the local message is about the run — a market,
  // a period, a product over the cap — and the server's is about the axes, which is what somebody
  // filling in axes needs to see. Suppressing the second until the first is answered would hide
  // the axis problem behind an unrelated blank field.
  const blocked = local ?? refused
  const total = combinationCount(form)

  const set = (patch: Partial<StudyForm>) => {
    setForm((current) => ({ ...current, ...patch }))
  }

  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Parameter study</h2>
        <p className="mt-1 text-sm text-slate-400">
          Run a saved strategy once for every combination of the parameters below, over one
          market.
        </p>
      </header>

      <form
        className="space-y-4"
        onSubmit={(event) => {
          event.preventDefault()
          if (blocked !== null || strategyId === null) return
          create.mutate(toStudyRequest(form, strategyId), {
            onSuccess: (created) => {
              setStudy(created.id, studyLabel(form))
              void navigate(`/studies/${created.id}`)
            },
          })
        }}
      >
        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
          {/* ⚠️ Chosen from what the server has, not from what this tab remembers. Before the
              strategy listing existed this screen simply refused to open unless you had built
              something since the last reload — with forty-five strategies in the database. */}
          <StrategyPicker
            value={strategyId ?? ''}
            onChange={(picked) => {
              setStrategy(picked.id, picked.name)
              // The axes belong to the setup that was just replaced, so keeping them would leave
              // paths pointing at a document that no longer has them — refused by the server,
              // but only after the reader had filled in values for nothing.
              set({ axes: [{ path: '', raw: '' }] })
            }}
          />
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

        {/* The same editor the catalogue uses. Extracted when the second caller appeared —
            seventy lines of controls copied is a second place to decide what a parameter may
            be, and the copy stops matching the day one of them learns a new control. */}
        <GridEditor
          setup={chosen?.setup ?? null}
          axes={form.axes}
          onChange={(axes) => {
            set({ axes })
          }}
        />

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

        {local !== null && total > 0 && <p className="text-sm text-amber-300">{local}</p>}
        {/* ⚠️ **Every refused point, named.** The server sends all of them because fixing a grid
            one round trip at a time is exactly what this endpoint exists to prevent — and the
            label is the one the heatmap and the run log already use, so the reader is not
            matching two descriptions of the same combination by eye. */}
        {refused !== null && total > 0 && (
          <div className="space-y-1">
            <p className="text-sm text-amber-300">{refused}</p>
            <ul className="space-y-1 text-xs text-amber-300/80">
              {preview.refusals.map((refusal) => (
                <li key={refusal.label}>
                  <span className="font-medium">{refusal.label}</span> — {refusal.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
        {create.isError && (
          <p className="text-sm text-red-400">{launchFailure(create.error)}</p>
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
