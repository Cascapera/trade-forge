// Turning what someone typed into the grid a study will search — and telling them how big it is
// before they commit to it.
//
// Pure, with no React in it, because the one number on this screen that matters is arithmetic:
// a grid's size is the **product** of its axes, and a fourth axis of five values does not add
// five runs, it multiplies by five. People consistently underestimate that, and a form that
// only reports it after the fact reports it too late.

import { ApiError } from '../api/client'
import type { CreateStudyRequest } from '../api/types'

/** Values a grid may try. Anything the DSL accepts for a parameter — a number, a flag, a name. */
export type AxisValue = number | boolean | string

export interface Axis {
  /** A dotted path into the strategy document: `setup.params.period`. */
  path: string
  /** What the person typed, kept verbatim so the field does not fight them while they type. */
  raw: string
}

export interface StudyForm {
  symbol: string
  timeframe: string
  dateFrom: string
  dateTo: string
  initialCapital: string
  spreadTicks: string
  axes: Axis[]
}

export const emptyStudyForm: StudyForm = {
  symbol: '',
  timeframe: 'H1',
  dateFrom: '',
  dateTo: '',
  initialCapital: '10000',
  spreadTicks: '',
  axes: [{ path: '', raw: '' }],
}

/** The server's own cap, repeated here so the form can refuse before the request is sent. */
export const MAX_POINTS = 500

/**
 * One axis's values, out of a comma-separated line.
 *
 * Numbers come back as numbers and everything else as a string, because a grid over
 * `side` is `long, short` and a grid over `period` is `5, 9, 20` — and `"5"` is not a period
 * the DSL will accept. `true`/`false` are flags, which several setups take.
 *
 * ⚠️ Blank entries are dropped rather than kept as empty strings. A trailing comma is what a
 * half-finished line looks like, and turning it into a value would send the server a parameter
 * of `""` — refused there, but refused with a message about a strategy rather than about a
 * comma.
 */
export function parseValues(raw: string): AxisValue[] {
  return raw
    .split(',')
    .map((piece) => piece.trim())
    .filter((piece) => piece.length > 0)
    .map((piece) => {
      if (piece === 'true') return true
      if (piece === 'false') return false
      // `Number('')` is 0 and `Number('  ')` is 0, which is why the blanks are gone by now.
      const asNumber = Number(piece)
      return Number.isFinite(asNumber) ? asNumber : piece
    })
}

/** The axes that are complete enough to mean something, keyed by path. */
export function axesOf(form: StudyForm): Record<string, AxisValue[]> {
  const grid: Record<string, AxisValue[]> = {}
  for (const axis of form.axes) {
    const path = axis.path.trim()
    const values = parseValues(axis.raw)
    if (path.length > 0 && values.length > 0) grid[path] = values
  }
  return grid
}

/**
 * How many backtests this grid will run.
 *
 * The **product**, and the whole reason this is on screen: three axes of five values is 125
 * runs, not 15. Zero when nothing is filled in yet, which reads as "nothing to launch" rather
 * than as the 1 an empty product would give.
 */
export function combinationCount(form: StudyForm): number {
  const values = Object.values(axesOf(form))
  if (values.length === 0) return 0
  return values.reduce((total, axis) => total * axis.length, 1)
}

/**
 * Why this study cannot be launched yet, or null.
 *
 * One reason at a time and in a fixed order, so the message is stable while a form is being
 * filled in — a list that reshuffles as fields are typed reads as the form arguing back.
 */
export function whyNotLaunchable(form: StudyForm): string | null {
  if (form.symbol === '') return 'Choose a market.'
  if (form.dateFrom === '' || form.dateTo === '') return 'Choose a period.'
  if (form.dateTo < form.dateFrom) return 'The end of the period precedes its start.'
  if (Number(form.initialCapital) <= 0) return 'Initial capital must be positive.'

  const grid = axesOf(form)
  const paths = Object.keys(grid)
  if (paths.length === 0) return 'Add at least one parameter to vary.'

  for (const [path, values] of Object.entries(grid)) {
    // Repeated values are two identical backtests, and on a heatmap they are one cell drawn
    // twice. The server refuses them too; catching it here costs a round trip nobody learns from.
    if (new Set(values.map((value) => String(value))).size !== values.length) {
      return `${path} repeats a value.`
    }
  }

  const total = combinationCount(form)
  if (total > MAX_POINTS) {
    return `That is ${String(total)} combinations, over the ${String(MAX_POINTS)} a study will run.`
  }
  return null
}

/**
 * The request body, from a form `whyNotLaunchable` has already approved.
 *
 * `spreadTicks` becomes a cost model the same way the single-backtest screen builds one: blank
 * is `{"type": "none"}`, which is the honest shape for "charge nothing", never a spread of zero.
 * A study holds one market still, so unlike a basket there is one cost for all of its points —
 * and it has to be one, or the points would not be comparable to each other.
 */
export function toStudyRequest(form: StudyForm, strategyId: string): CreateStudyRequest {
  return {
    strategy_id: strategyId,
    symbol: form.symbol,
    timeframe: form.timeframe,
    date_from: new Date(form.dateFrom).toISOString(),
    date_to: new Date(form.dateTo).toISOString(),
    initial_capital: form.initialCapital,
    cost_model:
      form.spreadTicks.trim() === ''
        ? { type: 'none' }
        : { type: 'spread', spread_points: form.spreadTicks.trim() },
    grid: axesOf(form),
  }
}

/** A short caption for the nav's link back to the study just launched. */
export function studyLabel(form: StudyForm): string {
  const axes = Object.keys(axesOf(form)).map((path) => path.split('.').at(-1) ?? path)
  return `${form.symbol} ${form.timeframe} · ${axes.join(', ')}`
}

/**
 * What to tell the reader when the server refuses a study.
 *
 * ⚠️ **The screen showed `"API error 422"`, which is the one thing that cannot be acted on.**
 * `ApiError.message` is built from the status alone; the reason lives in `detail`, and the
 * server's reasons are specific by design — *"`setup.params.periodd`: this strategy has nothing
 * at `setup.params.periodd`"*, *"this grid expands to 900 combinations, over the 500 a study
 * will run"*. Replacing those with a house apology throws away the only part that says what to
 * change.
 *
 * Two shapes arrive here, because two different validators refuse. A grid that cannot be applied
 * gives a **sentence**; a grid whose *values* produce an unrunnable strategy gives the DSL
 * validator's own body — `{message, errors}` — where `errors` is a list of pydantic failures
 * naming the field and what it wanted. Both are read: the second one is the case where the path
 * is fine and the number is not, which is the more confusing of the two to meet blind.
 */
export function launchFailure(error: unknown): string {
  const detail = error instanceof ApiError ? error.detail : null
  if (typeof detail === 'string') return detail

  if (detail !== null && typeof detail === 'object' && 'message' in detail) {
    const body = detail as { message?: unknown; errors?: unknown }
    const message = typeof body.message === 'string' ? body.message : 'The study was refused'
    const first: unknown = Array.isArray(body.errors) ? body.errors[0] : undefined
    const because = reasonOf(first)
    return because === null ? message : `${message}: ${because}`
  }

  // Not an `ApiError` at all — the network died, or the response was not JSON. Its own message
  // is the only thing that knows what happened ("Failed to fetch"), and swallowing it for a
  // house sentence would leave a reader retrying a form that was never the problem.
  if (error instanceof Error && error.message !== '') return error.message

  return 'The study was refused. Check the parameters and their values.'
}

/**
 * One pydantic failure as a sentence: which field, and what it wanted.
 *
 * Only the first, deliberately. A grid of fifty points that names one illegal value produces one
 * failure repeated fifty times, and listing them all would bury the sentence under its own
 * echoes.
 */
function reasonOf(failure: unknown): string | null {
  if (failure === null || typeof failure !== 'object') return null
  const { loc, msg } = failure as { loc?: unknown; msg?: unknown }
  if (typeof msg !== 'string') return null
  // The last segment is the field; the ones before it are the union branch pydantic took, which
  // names an internal model and would only puzzle a reader.
  const field: unknown = Array.isArray(loc) ? loc.at(-1) : undefined
  return typeof field === 'string' ? `${field} ${msg.toLowerCase()}` : msg
}
