import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useCatalog, useCreateSweep, useInstruments } from '../api/hooks'
import type { CatalogEntry } from '../api/types'
import { useMissingDataGate } from '../collect/gate'
import { anythingToRun, pairsOf } from '../collect/missing'
import { MissingDataPrompt } from '../components/MissingDataPrompt'
import { SymbolPicker } from '../components/SymbolPicker'
import { TIMEFRAMES } from '../strategy/builder'
import { useSweepRehearsal } from '../sweep/preview'
import {
  emptySweepForm,
  launchFailure,
  runCount,
  toSweepRequest,
  whyNotLaunchable,
  type SweepForm,
} from '../sweep/settings'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

function toggle(list: readonly string[], value: string): string[] {
  return list.includes(value) ? list.filter((one) => one !== value) : [...list, value]
}

/**
 * Run several shelf entries, over several charts, over several markets — at once.
 *
 * The product a study and a basket each refuse to take. A study lists parameter values and holds
 * the market still; a basket lists markets and holds the parameters still. This screen lists
 * **entries**, and lets both of the others' axes vary at the same time.
 *
 * ⚠️ **The shelf is inside this screen rather than linking out to it.** The catalogue at
 * `/catalog` is a curation screen — add, describe, remove — and the sweep is a launch. Folding
 * the markets, charts, window, capital and cost model into the catalogue would make the two
 * compete for the same page, which is what `/study` and `/basket` avoided by having their own.
 * Ticking happens here so that "pick the ones worth running and press run" is still one motion.
 */
export function LaunchSweep(): React.JSX.Element {
  const catalog = useCatalog()
  const instruments = useInstruments()
  const create = useCreateSweep()
  const navigate = useNavigate()

  const [form, setForm] = useState<SweepForm>(emptySweepForm)
  // Set when the plan came back empty for a sweep whose every pair lacks data: there is nothing
  // to collect either, and the all-skipped refusal stands. Cleared by any edit, like the prompt.
  const [nothingToFetch, setNothingToFetch] = useState(false)
  const entries: CatalogEntry[] = catalog.data?.items ?? []

  const rehearsal = useSweepRehearsal(form)
  const total = runCount(form, entries)
  const local = whyNotLaunchable(form, entries)

  // ⚠️ **Three refusals, kept apart, because they have three different fixes.** The local one is
  // about this form — a blank field, an entry that left the shelf — and the screen can decide it
  // alone. Never the size of the sweep: there is no cap (18/09).
  // The DSL's is about a combination and is fixed by editing the entry's grid. The coverage gap
  // is about *data*, and nothing on this form will make it go away: the fix is a backfill or a
  // different window. Pooling them into one "cannot run" would send somebody to edit a grid when
  // what they need is to collect candles.
  const stale = !rehearsal.settled
  const refusedByDsl =
    stale || rehearsal.refusals.length === 0
      ? null
      : `${String(rehearsal.refusals.length)} combination${rehearsal.refusals.length === 1 ? '' : 's'} cannot run and will be left out.`
  const uncovered = stale ? [] : rehearsal.uncovered
  const serverError = stale ? null : rehearsal.error

  // ⚠️ **The one server error that must not block: every pair without data.** Collecting is its
  // fix, and a disabled button would keep the reader from the prompt that offers it. It is told
  // apart by shape, not by wording: `uncovered` names only pairs on a chart that can run, so a
  // non-empty list with nothing left to run *is* "all skipped". With runs above zero the server
  // sends no error at all since the cap went (18/09): a sweep with something to run is never refused
  // for its size.
  //
  // ⚠️ **Until the plan says there is nothing to fetch.** A window wholly in the future, or older
  // than the broker's first bar, is "all skipped" too, and no download can mend it: the plan
  // comes back empty, and launching would meet the same refusal as a 422. Then it blocks again.
  const dataGap = uncovered.length > 0 && rehearsal.runs === 0
  const blocked = local ?? (dataGap && !nothingToFetch ? null : serverError)

  const launch = (collectMissing = false): void => {
    // Straight to the sweep, where its runs land section by section as the worker drains them —
    // the same move a study makes. The pairs left out are kept on the sweep and read there, so
    // nothing needs carrying across the navigation.
    create.mutate(toSweepRequest(form, collectMissing), {
      onSuccess: (created) => {
        void navigate(`/sweeps/${created.id}`)
      },
    })
  }
  // An empty plan launches with `collect_missing` off: nothing missing means an ordinary launch —
  // unless nothing would run either, which an empty plan cannot change (see `dataGap`).
  //
  // ⚠️ **Asked on the click, not read off the rehearsal.** `uncovered` lists only pairs with *no*
  // candle in the window; a pair covered in part is absent from it, and would run over half the
  // window without anybody being asked. The plan sees the gap — the same one the launch collects.
  const gate = useMissingDataGate(() => {
    if (dataGap) {
      setNothingToFetch(true)
      return
    }
    launch()
  })

  // A prompt answers the form it was asked about; any edit closes it.
  const set = (patch: Partial<SweepForm>) => {
    gate.dismiss()
    setNothingToFetch(false)
    setForm((current) => ({ ...current, ...patch }))
  }

  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Sweep</h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-400">
          Run several entries off the shelf, over several charts, over several markets — every
          combination at once. A study holds the market still and a basket holds the parameters
          still; this holds nothing still.
        </p>
      </header>

      <form
        className="space-y-5"
        onSubmit={(event) => {
          event.preventDefault()
          if (blocked !== null) return
          const request = toSweepRequest(form)
          gate.check({
            symbols: request.symbols,
            timeframes: request.timeframes,
            date_from: request.date_from,
            date_to: request.date_to,
          })
        }}
      >
        <fieldset className="space-y-2">
          <legend className="text-sm text-slate-300">
            Entries
            {form.entryIds.length > 0 && (
              <span className="text-slate-500"> — {form.entryIds.length} chosen</span>
            )}
          </legend>
          {/* ⚠️ A read that failed and an empty shelf are different facts, and `isError` comes
              first for that reason — falling through to "the shelf is empty" would print an
              unanswered question as an answer. The same order the catalogue screen uses. */}
          {catalog.isError ? (
            <p className="text-sm text-red-400">Could not load the catalogue.</p>
          ) : catalog.isPending ? (
            <p className="text-sm text-slate-400">Loading the shelf…</p>
          ) : entries.length === 0 ? (
            <p className="text-sm text-slate-400">
              The shelf is empty. Put a strategy on it first, under a name you will recognise.
            </p>
          ) : (
            <ul className="grid gap-2 sm:grid-cols-2">
              {entries.map((entry) => (
                <li key={entry.id}>
                  <label className="flex cursor-pointer items-start gap-2 rounded border border-slate-800 px-3 py-2 hover:border-slate-700">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={form.entryIds.includes(entry.id)}
                      onChange={() => {
                        set({ entryIds: toggle(form.entryIds, entry.id) })
                      }}
                    />
                    <span className="min-w-0">
                      <span className="block text-sm text-slate-100">{entry.name}</span>
                      {/* One backtest is said in words rather than as a bare `1`, because a
                          number beside a label reads as a count of results until it says what
                          it counts. The same sentence the catalogue uses. */}
                      <span className="block text-xs text-slate-500">
                        {entry.points === 1 ? 'one backtest' : `${String(entry.points)} backtests`}{' '}
                        each · <span className="font-mono">{entry.setup ?? 'DSL'}</span>
                      </span>
                    </span>
                  </label>
                </li>
              ))}
            </ul>
          )}
        </fieldset>

        {/* The basket's own picker, not a second one. A copy would be a second place to decide
            how many markets a request may name, and it would stop matching. */}
        <SymbolPicker
          instruments={instruments.data}
          chosen={form.symbols}
          onToggle={(symbol) => {
            set({ symbols: toggle(form.symbols, symbol) })
          }}
        />

        <fieldset className="space-y-2">
          <legend className="text-sm text-slate-300">
            Charts
            {form.timeframes.length > 0 && (
              <span className="text-slate-500"> — {form.timeframes.length} chosen</span>
            )}
          </legend>
          {/* ⚠️ Each chart becomes a **document** of its own, not merely a run parameter. Since a
              document and its run must agree under a higher-timeframe filter, the sweep writes
              the chart into the document — which is also what keeps the two from colliding on a
              name. Nothing to do here, but it is why a chart is an axis and not a setting. */}
          <div className="flex flex-wrap gap-2">
            {TIMEFRAMES.map((timeframe) => (
              <label
                key={timeframe}
                className="flex cursor-pointer items-center gap-1.5 rounded border border-slate-800 px-2.5 py-1 text-sm text-slate-200 hover:border-slate-700"
              >
                <input
                  type="checkbox"
                  checked={form.timeframes.includes(timeframe)}
                  onChange={() => {
                    set({ timeframes: toggle(form.timeframes, timeframe) })
                  }}
                />
                {timeframe}
              </label>
            ))}
          </div>
        </fieldset>

        <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
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
            Initial capital
            <input
              type="text"
              inputMode="decimal"
              className={inputClass}
              value={form.initialCapital}
              onChange={(event) => {
                set({ initialCapital: event.target.value })
              }}
            />
          </label>
          <label className="flex flex-col gap-1 text-sm text-slate-300">
            Spread (points)
            <input
              type="text"
              inputMode="decimal"
              placeholder="none"
              className={inputClass}
              value={form.spreadTicks}
              onChange={(event) => {
                set({ spreadTicks: event.target.value })
              }}
            />
          </label>
        </div>

        <div className="flex flex-wrap items-center gap-4">
          <button
            type="submit"
            disabled={blocked !== null || create.isPending || gate.plan.isPending}
            className="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-700"
          >
            {create.isPending
              ? 'Launching…'
              : gate.plan.isPending
                ? 'Checking the data…'
                : 'Run the sweep'}
          </button>
          {/* ⚠️ **A count it could not make is said in words, never as `0`.** `runCount` returns
              null when a ticked entry has left the shelf, and printing that as zero would claim
              a measurement — the reader could not tell "nothing to run" from "I could not
              count". */}
          <p className="text-sm text-slate-300" role="status">
            {total === null
              ? 'One of the entries you chose is no longer on the shelf.'
              : total === 0
                ? 'Nothing to run yet.'
                : `${String(total)} backtest${total === 1 ? '' : 's'}.`}
          </p>
          {rehearsal.asking && <p className="text-xs text-slate-500">Checking…</p>}
        </div>

        {local !== null && form.entryIds.length > 0 && (
          <p className="text-sm text-amber-300">{local}</p>
        )}

        {/* The prompt: what the plan says is missing, and the two answers — collect and run
            everything once the downloads land, or run now and leave out what has nothing to
            read. The same one the single backtest and the basket open. */}
        <MissingDataPrompt
          gate={gate}
          onRunAnyway={() => {
            launch()
          }}
          // One download per pair, and every run over it waits for it (PR-268).
          onCollectAndRun={() => {
            launch(true)
          }}
          launching={create.isPending}
          // ⚠️ By pair: a market can hold M15 and not H4. And `dataGap` first, because the
          // rehearsal knows what the plan cannot — which charts the DSL refuses — so "every pair
          // with a runnable point is empty" is its answer, not the plan's.
          canRun={
            gate.missing === null ||
            (!dataGap && anythingToRun(pairsOf(form.symbols, form.timeframes), gate.missing))
          }
        />

        {/* ⚠️ **The coverage gap is its own message, above the DSL refusals, and it names every
            pair.** It is the only one of the three whose fix is outside this screen. A pair
            never collected and one collected for other years are said differently, because one
            is a backfill to run and the other is a window to move. An early warning: pressing
            the button asks what to do about it. */}
        {uncovered.length > 0 && (
          <div className="space-y-1">
            <p className="text-sm text-amber-300">
              No candles in this window for {String(uncovered.length)}{' '}
              {uncovered.length === 1 ? 'pair' : 'pairs'}: running leaves{' '}
              {uncovered.length === 1 ? 'it' : 'them'} out, and pressing run asks whether{' '}
              {uncovered.length === 1 ? 'it' : 'they'} can be collected first.
            </p>
            <ul className="space-y-1 text-xs text-amber-300/80">
              {uncovered.map((pair) => (
                <li key={`${pair.symbol}-${pair.timeframe}`}>
                  <span className="font-medium">
                    {pair.symbol} {pair.timeframe}
                  </span>{' '}
                  — {pair.covers === null ? 'never collected' : `collected ${pair.covers}`}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* Refused combinations are **left out**, not refused: the launch drops them and runs the
            rest. Said in the present tense and beside the count, so it reads as a subtraction
            already made rather than as a problem to solve before pressing the button. */}
        {refusedByDsl !== null && (
          <div className="space-y-1">
            <p className="text-sm text-amber-300">{refusedByDsl}</p>
            <ul className="space-y-1 text-xs text-amber-300/80">
              {rehearsal.refusals.map((refusal) => (
                <li key={`${refusal.entryName}-${refusal.label}`}>
                  <span className="font-medium">{refusal.entryName}</span>{' '}
                  <span className="font-mono">{refusal.label}</span> — {refusal.reason}
                </li>
              ))}
            </ul>
          </div>
        )}

        {/* ⚠️ Printed unless it is the all-skipped sentence, which the list above already says.
            Keyed on `dataGap` rather than on the list being empty, so any other refusal the
            server sends beside a partial gap still reaches the reader instead of leaving a
            disabled button with no reason given. */}
        {serverError !== null && !dataGap && (
          <p className="text-sm text-amber-300">{serverError}</p>
        )}
        {/* The reason the button went back to disabled: said, because the list above promised a
            question about collecting, and the answer is that there is nothing to collect. */}
        {nothingToFetch && dataGap && (
          <p role="status" className="text-sm text-amber-300">
            Nothing to collect for this window either — the broker holds no bars in it for these
            pairs. Move the window.
          </p>
        )}

        {/* ⚠️ **A failed check is not a passed one.** Without this line a preview that errored
            fell through to the empty answer — no warning, no "Checking…", a live button — which
            is exactly what "nothing wrong with this sweep" looks like. It does not block: the
            launch is checked again by the server, and a flaky preview must not hold that hostage. */}
        {rehearsal.failure !== null && (
          <p className="text-sm text-amber-300">
            {rehearsal.failure} The launch will still be checked when you press it.
          </p>
        )}

        {create.isError && <p className="text-sm text-red-400">{launchFailure(create.error)}</p>}
      </form>

      {/* ⚠️ **The warning that belongs beside this screen and nowhere else.** A study searches one
          space; a sweep searches several at once, and the more it searches the better the best
          result looks for reasons that have nothing to do with the method. Measured on this
          project's own data: a grid of 4 points degraded 4 percentage points out of sample, one
          of 50 degraded 9. */}
      <p className="max-w-3xl text-xs text-slate-500">
        Fifty points over five markets and three charts is seven hundred and fifty measurements,
        and the best of them is the best of seven hundred and fifty draws. Re-running the winner
        over the same window returns the identical number — the engine is deterministic, so that
        is not a second opinion. The only honest follow-up is data the winner was not chosen on: a
        walk-forward, another market, a window you held back.
      </p>
    </section>
  )
}
