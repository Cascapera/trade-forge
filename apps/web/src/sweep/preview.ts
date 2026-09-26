// Asking the server what a sweep would enqueue, while the form is still being filled in.
//
// ⚠️ **Nothing here decides whether a combination is legal, or whether a market has data.** Both
// are the server's to answer — the first from the DSL's semantics, which live in Python once, the
// second from the `datasets` index. A copy of either here would be a second version of a contract,
// and the day one changed the screen would confidently say the wrong thing. This file asks, waits,
// and reports which of the three noes came back.

import { useEffect, useState } from 'react'

import { apiFailure } from '../api/failure'
import { useSweepPreview } from '../api/hooks'
import type { RefusalGroup, SweepEntryPreview, TimeEstimate, UncoveredMarket } from '../api/types'

import { toPreviewRequest, type SweepForm } from './settings'

/**
 * How long the form has to settle before the question is sent.
 *
 * The same 400ms the grid preview waits, and mostly for the same reason: the window is typed, and
 * `2025-0`, `2025-01`, `2025-01-0` are dates nobody meant to ask about. The ticked axes change by
 * click and have no half-finished states, but they ride the same timer — a second, shorter timer
 * for them would send two questions for one edit whenever somebody ticks a market and then fixes
 * a date, and the first answer would arrive about a sweep already superseded.
 */
const DEBOUNCE_MS = 400

export interface SweepRehearsal {
  /** How many backtests would be enqueued — refusals already subtracted by the server. */
  runs: number
  /** Per entry, so a refusal can be shown under the label that caused it. */
  entries: SweepEntryPreview[]
  /** Every reason the DSL refuses combinations for, pooled for the count but each carrying its
   *  own entry. */
  refusals: (RefusalGroup & { entryName: string })[]
  /** (symbol, timeframe) pairs with no candles in this window. ⚠️ Not a refusal to fix by
   *  editing: the fix is a backfill, or a different window. */
  uncovered: UncoveredMarket[]
  /**
   * Set when the sweep cannot be launched at all — over the cap, nothing runnable, or a market
   * with no candles. ⚠️ The server fills it on a coverage gap **too**, beside `uncovered`, which
   * is why the screen only prints it when `uncovered` is empty: otherwise the same no is said
   * twice, once as a list and once as a sentence. An unknown entry is not here — that is a 404.
   */
  error: string | null
  /** How long the runs would take one after another, or null with nothing to measure by. */
  backtestTime: TimeEstimate | null
  /**
   * Why the question itself could not be answered — the request failed — or null.
   *
   * ⚠️ **Not the same as a preview that found nothing wrong.** A failed request used to fall
   * through to the empty answer, and the empty answer is what "all clear" looks like: no warning,
   * no "Checking…", a live button. Reported apart from `error` because it says nothing about the
   * sweep — only that this early warning is missing. The launch is still checked by the server.
   */
  failure: string | null
  /**
   * Whether the answer on hand is about the sweep currently on screen.
   *
   * ⚠️ The screen must not act on a stale answer. Between an edit and the reply, what is in hand
   * is a **true** verdict about a sweep nobody is proposing any more, and blocking a launch over
   * it means the reader fixes a market and the screen goes on refusing what they just corrected.
   */
  settled: boolean
  /** Whether a question is on the wire, so the screen can say so rather than show a stale total. */
  asking: boolean
}

const NOTHING: Omit<SweepRehearsal, 'settled' | 'asking' | 'failure'> = {
  runs: 0,
  entries: [],
  refusals: [],
  uncovered: [],
  error: null,
  backtestTime: null,
}

/**
 * The server's verdict on the sweep this form describes.
 *
 * ⚠️ **An early warning, never the gate.** The gate is `POST /sweeps`, which expands every
 * combination again and writes nothing if the whole thing is refused — and it has to be, because
 * a person can click launch before this answer lands, and because a preview is a different
 * request that could be answered by a different deploy.
 */
export function useSweepRehearsal(form: SweepForm): SweepRehearsal {
  const wanted = toPreviewRequest(form)
  // Compared as text: `toPreviewRequest` builds a new object every render, so an identity check
  // would report a change on every keystroke including the ones that changed nothing.
  const current = JSON.stringify(wanted)
  const [asked, setAsked] = useState(current)

  useEffect(() => {
    const timer = setTimeout(() => {
      setAsked(current)
    }, DEBOUNCE_MS)
    return () => {
      clearTimeout(timer)
    }
  }, [current])

  const request = JSON.parse(asked) as ReturnType<typeof toPreviewRequest>
  const query = useSweepPreview(request)

  // ⚠️ **Which sweep the answer on hand is about**, read off the answer rather than assumed from
  // the key. The debounce opens the gap this closes: for 400ms after an edit the query is still
  // keyed on the *previous* question, so what is in hand is a real verdict about a sweep nobody
  // is looking at any more.
  const answered = query.data === undefined ? null : JSON.stringify(query.data.asked)
  const settled = answered === current
  const preview = query.data?.preview

  if (preview === undefined) {
    // ⚠️ A failure belongs to the question the query is keyed on, which is `asked`, not
    // `current`: during the debounce after an edit it is about a sweep nobody is proposing any
    // more, and it is dropped for the same reason a stale verdict is.
    const failure =
      query.isError && asked === current
        ? apiFailure(query.error, 'Could not check this sweep with the server.')
        : null
    return { ...NOTHING, settled: wanted === null, asking: query.isFetching, failure }
  }

  return {
    runs: preview.runs,
    entries: preview.entries,
    // Flattened with the label that produced each one. ⚠️ Pooling them without the label is the
    // failure this shape exists to prevent: `htf must be coarser than H4` says nothing about
    // which shelf entry it is about, and a sweep holds several.
    refusals: preview.entries.flatMap((entry) =>
      entry.refusals.map((refusal) => ({ ...refusal, entryName: entry.name })),
    ),
    uncovered: preview.uncovered,
    error: preview.error,
    backtestTime: preview.backtest_time,
    failure: null,
    settled,
    asking: query.isFetching,
  }
}
