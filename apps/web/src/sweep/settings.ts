// The sweep form, and everything about it that is arithmetic rather than rendering.
//
// A study varies the parameters and holds the market still; a basket varies the market and holds
// the parameters still. A sweep takes both at once and adds a third axis, the chart. What that
// makes easy is also the mistake it makes easy, so the counting lives here, in functions a test
// can call without a browser.
//
// ⚠️ **Nothing here decides whether a combination is legal.** That is the DSL's semantics, and
// they live in Python once (`/sweeps/preview`). This file knows only what the form can see: an
// empty field, a window that runs backwards, an entry that has left the shelf.

import type { CatalogEntry, CreateSweepRequest, PreviewSweepRequest } from '../api/types'
import { apiFailure } from '../api/failure'

export interface SweepForm {
  /** Shelf entries to sweep, by id, in the order they were ticked. */
  entryIds: string[]
  symbols: string[]
  timeframes: string[]
  dateFrom: string
  dateTo: string
  initialCapital: string
  /** Blank means charge nothing, which is `{"type": "none"}` and never a spread of zero. */
  spreadTicks: string
}

export const emptySweepForm: SweepForm = {
  entryIds: [],
  symbols: [],
  timeframes: [],
  dateFrom: '',
  dateTo: '',
  initialCapital: '10000',
  spreadTicks: '',
}

/**
 * How many backtests this form describes.
 *
 * ⚠️ **The entries add; the charts and the markets multiply.** Three entries of ten points each
 * is thirty documents, not a thousand — a combination picks *one* entry, so entries are
 * alternatives rather than an axis. Getting that backwards is the arithmetic mistake this
 * function exists to make once, and the intuition pulls the wrong way because everything else
 * on the form does multiply.
 *
 * Counted from the shelf's own `points`, which the server derives from each entry's grid. An
 * entry that varies nothing counts as **one**: the empty product, not zero.
 *
 * ⚠️ **`null` when a ticked entry is not in the list, and that is not the same as `0`.** The
 * first draft treated an unknown id as contributing nothing, which reads as a measurement: a
 * form with three entries ticked would say `0 backtests` beside them and the reader would have
 * no way to tell "nothing to run" from "I could not count". It is reachable — the shelf is a
 * live query, and an entry removed in another tab disappears from it on the next refetch —
 * and the silent version simply under-counts the sweep it is about to launch.
 */
export function runCount(form: SweepForm, entries: readonly CatalogEntry[]): number | null {
  const byId = new Map(entries.map((entry) => [entry.id, entry]))
  let documents = 0
  for (const id of form.entryIds) {
    const entry = byId.get(id)
    if (entry === undefined) return null
    documents += entry.points
  }
  return documents * form.timeframes.length * form.symbols.length
}

/**
 * Why this sweep cannot be launched yet, or null.
 *
 * One reason at a time and in a fixed order, so the message is stable while a form is being
 * filled in — a list that reshuffles as fields are ticked reads as the form arguing back.
 *
 * ⚠️ Only what the form can decide on its own. The DSL's refusals and the coverage gaps come
 * from the preview and are shown beside this, never folded into it: they answer different
 * questions and have different fixes.
 */
export function whyNotLaunchable(form: SweepForm, entries: readonly CatalogEntry[]): string | null {
  if (form.entryIds.length === 0) return 'Choose at least one entry from the shelf.'
  if (form.symbols.length === 0) return 'Choose at least one market.'
  if (form.timeframes.length === 0) return 'Choose at least one chart.'
  if (form.dateFrom === '' || form.dateTo === '') return 'Choose a period.'
  // ⚠️ `<=`, not `<`: the sweeps table refuses a window of zero duration with a CHECK, so a form
  // that allowed it would trade a readable refusal here for a 422 after the click.
  if (form.dateTo <= form.dateFrom) return 'The end of the period must be after its start.'
  if (Number(form.initialCapital) <= 0) return 'Initial capital must be positive.'

  // ⚠️ Refused rather than waved through: the entry is gone from the shelf, so the launch would
  // come back as a 404 after the click. Said here, where the reader can see which tick to undo.
  //
  // ⚠️ **The cap is not checked here, and an earlier draft did.** It capped this count, which is
  // gross — every combination, including the ones the DSL refuses — while the server caps the
  // net count, what would actually be enqueued. 3100 combinations with 200 refused is 2900 runs:
  // the server starts that sweep and the old form refused it. Only the preview knows the net
  // number, and its sentence already blocks the button.
  if (runCount(form, entries) === null) return 'An entry you chose is no longer on the shelf.'
  return null
}

/**
 * The question to ask the preview, or null when there is not enough of a form to ask about.
 *
 * ⚠️ **The window is in the question.** Coverage is per (symbol, timeframe), so the dates decide
 * whether a run can happen at all and not merely how much history it reads — which is why this
 * returns null until they are filled in rather than asking about an open-ended window.
 */
export function toPreviewRequest(form: SweepForm): PreviewSweepRequest | null {
  if (form.entryIds.length === 0 || form.symbols.length === 0) return null
  if (form.timeframes.length === 0) return null
  if (form.dateFrom === '' || form.dateTo === '' || form.dateTo <= form.dateFrom) return null
  return {
    entry_ids: [...form.entryIds],
    symbols: [...form.symbols],
    timeframes: [...form.timeframes],
    date_from: new Date(form.dateFrom).toISOString(),
    date_to: new Date(form.dateTo).toISOString(),
  }
}

/**
 * The request body, from a form `whyNotLaunchable` has already approved.
 *
 * One cost model for the whole sweep, like a study and for the same reason: points measured
 * under different costs are not comparable to each other, and comparing them is the only reason
 * to run them together.
 */
export function toSweepRequest(form: SweepForm, collectMissing = false): CreateSweepRequest {
  return {
    entry_ids: [...form.entryIds],
    symbols: [...form.symbols],
    timeframes: [...form.timeframes],
    date_from: new Date(form.dateFrom).toISOString(),
    date_to: new Date(form.dateTo).toISOString(),
    initial_capital: form.initialCapital,
    cost_model:
      form.spreadTicks.trim() === ''
        ? { type: 'none' }
        : { type: 'spread', spread_points: form.spreadTicks.trim() },
    collect_missing: collectMissing,
  }
}

/**
 * What to tell the reader when the server refuses a sweep.
 *
 * The unpacking lives in `api/failure.ts`, shared with every other screen that writes: the reason
 * is in `detail`, never in `ApiError.message`, and there are three shapes of it.
 */
export function launchFailure(error: unknown): string {
  return apiFailure(error, 'The sweep was refused. Check the entries, markets and window.')
}
