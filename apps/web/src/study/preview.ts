// Asking the server what a grid would produce, while the grid is still being typed.
//
// ⚠️ **Nothing here decides whether a point is legal.** That is the DSL's semantics — `htf`
// coarser than the document's own timeframe and a whole number of its bars, a broker clock
// required beside a filter — and they live in Python, once. A copy here would be a second version
// of the contract, and the day either changed the screen would confidently say the wrong thing.
// So this file asks, waits, and reports. It is plumbing on purpose.

import { useEffect, useState } from 'react'

import { useStudyPreview } from '../api/hooks'
import type { GridRefusal } from '../api/types'

import { axesOf, type StudyForm } from './settings'

/**
 * How long typing has to pause before the grid is sent.
 *
 * Longer than the symbol combobox's 150ms, and for a different reason. Nobody is waiting on this
 * answer the way they wait on a dropdown — it is a warning that arrives beside a form somebody is
 * still filling in — and an axis line is typed in bursts (`5`, `5,`, `5, 9`), where the
 * intermediate states are values nobody meant to ask about.
 */
const DEBOUNCE_MS = 400

export interface GridPreview {
  /** Every point that cannot run, or empty. Never a prefix of them — the server sends all. */
  refusals: GridRefusal[]
  /** Set when the grid cannot be applied to this strategy at all: no points exist to report on. */
  gridError: string | null
  /**
   * Whether the answer on hand is about the grid currently on screen.
   *
   * ⚠️ The screen must not act on a stale answer. Between a keystroke and the reply, the
   * refusals belong to a grid that no longer exists, and blocking a launch over them would be
   * refusing something nobody asked for.
   */
  settled: boolean
}

/**
 * The server's verdict on the grid this form describes.
 *
 * ⚠️ **An early warning, never the gate.** The gate is `POST /studies`, which validates every
 * point again and writes nothing if one fails — and it has to be, because a person can click
 * launch before this answer lands, and because a preview is a different request that could be
 * answered by a different deploy. This exists so the refusal arrives while the axis that caused
 * it is still on screen, instead of as a 422 after a click.
 */
export function useGridPreview(strategyId: string | null, form: StudyForm): GridPreview {
  const grid = axesOf(form)
  // Compared as text: `axesOf` builds a new object every render, so an identity check would
  // report a change on every keystroke including the ones that changed nothing.
  const current = JSON.stringify(grid)
  const [asked, setAsked] = useState(current)

  useEffect(() => {
    const timer = setTimeout(() => {
      setAsked(current)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [current])

  // The debounced grid, parsed back — so the query is keyed on exactly what was asked rather
  // than on a string that would make the key a different shape from every other query's.
  // ⚠️ The form's timeframe, undebounced, and that is deliberate: it changes by a click on a
  // select rather than by typing, so there is no half-finished value to wait out — and a stale
  // verdict about the timeframe the reader just left is exactly what this screen must not show.
  const preview = useStudyPreview(
    strategyId,
    JSON.parse(asked) as Record<string, unknown[]>,
    form.timeframe,
  )

  // ⚠️ **Which grid the answer on hand is about**, read off the answer rather than assumed from
  // the key. The debounce opens the gap this closes: for 400ms after a keystroke the query is
  // still keyed on the *previous* grid, so what is in hand is a true verdict about a grid nobody
  // is looking at any more — and blocking on it means the reader fixes an axis and the screen
  // goes on refusing what they just corrected.
  const answered = preview.data === undefined ? null : JSON.stringify(preview.data.grid)

  return {
    refusals: preview.data?.preview.refusals ?? [],
    gridError: preview.data?.preview.grid_error ?? null,
    settled: answered === current,
  }
}
