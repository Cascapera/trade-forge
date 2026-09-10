// Turning what someone typed into the grid a study will search — and telling them how big it is
// before they commit to it.
//
// Pure, with no React in it, because the one number on this screen that matters is arithmetic:
// a grid's size is the **product** of its axes, and a fourth axis of five values does not add
// five runs, it multiplies by five. People consistently underestimate that, and a form that
// only reports it after the fact reports it too late.

import { apiFailure } from '../api/failure'
import type { CreateStudyRequest } from '../api/types'

/** Values a grid may try. Anything the DSL accepts for a parameter — a number, a flag, a name,
 *  or `null` for a rule switched off. */
export type AxisValue = number | boolean | string | null

/**
 * How "the rule switched off" is written on an axis.
 *
 * ⚠️ **A word, because a grid line is text and `null` has no spelling in it.** The values of an
 * axis travel as `2, 3, 4` — comma-separated, typed by hand or built by a control — and an empty
 * slot there already means "a trailing comma somebody is still typing". So the absence of a value
 * needs a name of its own, and `off` is the one the strategy builder's own select already shows
 * for these parameters.
 *
 * It is safe against the DSL's vocabulary today: no enum in the schema has a member called `off`
 * — the entry points, the sides, the gift's stops, the timeframes are all other words. A schema
 * that ever added one would collide, which is why the token lives here, named, rather than being
 * spelled out at each of the four places that need it.
 */
export const OFF = 'off'

/** One axis value as it is written on a line — the inverse of `parseValues` for a single value. */
export function textOf(value: AxisValue): string {
  return value === null ? OFF : String(value)
}

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
 * comma. `off` is therefore a **word** rather than that blank: it is a value somebody asked for,
 * and it has to survive a line the blank is being stripped out of.
 */
export function parseValues(raw: string): AxisValue[] {
  return raw
    .split(',')
    .map((piece) => piece.trim())
    .filter((piece) => piece.length > 0)
    .map((piece) => {
      if (piece === OFF) return null
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
 * The unpacking itself lives in `api/failure.ts`, shared with every other screen that writes:
 * the reason is in `detail`, never in `ApiError.message`, and there are three shapes of it.
 * What stays here is the sentence for when the server sent nothing readable, which is the only
 * part that is about studies rather than about the API.
 */
export function launchFailure(error: unknown): string {
  return apiFailure(error, 'The study was refused. Check the parameters and their values.')
}
