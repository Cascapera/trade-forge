import { useState } from 'react'

import { ApiError } from '../api/client'
import { useYearCut } from '../api/hooks'
import type { Backtest } from '../api/types'
import { type EntryYear, entryYears, sumR, windowYears } from '../backtest/years'
import { inR } from '../format'

const head = 'px-3 py-2 text-right text-xs font-medium text-slate-400'
const cell = 'px-3 py-2 text-right tabular-nums'

/** Where an entry year's trades left, in a sentence: `Entered in 2021 — left in 2021: +2.50 R ·
 *  2022: -1.00 R`. The tooltip of a row whose trades did not all leave in the year they entered. */
function exitsOf(row: EntryYear): string {
  return `Entered in ${String(row.year)} — left in ${row.exits
    .map((exit) => `${String(exit.year)}: ${inR(exit.r)}`)
    .join(' · ')}`
}

/** What a 422 from `/years` says, as text. The server's detail is a sentence; anything else is
 *  not, and is not shown as one. */
function refusalOf(error: Error): string | null {
  if (!(error instanceof ApiError) || error.status !== 422) return null
  return typeof error.detail === 'string' ? error.detail : 'no reason was given'
}

/**
 * Pick two years of the run's window and read the R a run of those whole years would have made,
 * cut from this one (`GET /backtests/{id}/years`).
 *
 * ⚠️ **A refusal is an answer, shown as one.** The server refuses a cut it cannot vouch for —
 * outside the window, a first year the run did not start by, or sizing that a later start could
 * have changed (`year_cut`) — and says why; the screen passes that on instead of a generic error.
 */
function YearCutPicker(props: { runId: string; years: number[] }): React.JSX.Element {
  const { years } = props
  const [first, setFirst] = useState(years[0] ?? 0)
  const [last, setLast] = useState(years[years.length - 1] ?? 0)
  const cut = useYearCut(props.runId, first, last, years.length > 0)
  const refusal = cut.isError ? refusalOf(cut.error) : null

  const select = 'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100'
  return (
    <div className="space-y-2 text-sm">
      <div className="flex flex-wrap items-center gap-2 text-slate-300">
        <label htmlFor="cut-first">Cut from</label>
        <select
          id="cut-first"
          value={first}
          onChange={(event) => {
            setFirst(Number(event.target.value))
          }}
          className={select}
        >
          {years.map((year) => (
            <option key={year} value={year}>
              {year}
            </option>
          ))}
        </select>
        <label htmlFor="cut-last">to</label>
        <select
          id="cut-last"
          value={last}
          onChange={(event) => {
            setLast(Number(event.target.value))
          }}
          className={select}
        >
          {years.map((year) => (
            <option key={year} value={year}>
              {year}
            </option>
          ))}
        </select>
      </div>
      <div role="status" aria-label="R of the cut">
        {cut.isPending && cut.fetchStatus !== 'idle' && (
          <p className="text-slate-400">Cutting the run…</p>
        )}
        {cut.data !== undefined && (
          <div className="space-y-1">
            <p className="text-slate-100">
              {String(cut.data.first_year)} to {String(cut.data.last_year)}:{' '}
              <strong className="tabular-nums">{inR(cut.data.net_r)}</strong>
            </p>
            <p className="text-xs text-slate-500">
              {Object.keys(cut.data.yearly_r).length === 0
                ? 'No trade entered and left inside these years.'
                : Object.entries(cut.data.yearly_r)
                    .map(([year, r]) => `${year} ${inR(r)}`)
                    .join(' · ')}{' '}
              — what a run of these whole years would have made: the trades that entered from{' '}
              {String(cut.data.first_year)} and left by the end of {String(cut.data.last_year)}.
            </p>
          </div>
        )}
        {refusal !== null && (
          <p className="text-amber-200">
            This run cannot stand for {String(first)} to {String(last)} without running it again:{' '}
            {refusal}. Run that window on its own to measure it.
          </p>
        )}
        {cut.isError && refusal === null && (
          <p className="text-red-400">Could not cut this run: {cut.error.message}</p>
        )}
      </div>
    </div>
  )
}

/**
 * A run's R by year of entry (01/10), with the total, and a cut to any whole years of its window.
 *
 * A row sums every trade that **entered** in its year, wherever it left. ⚠️ A trade open across the
 * new year counts in the year it entered, so a row can hold R booked in the next calendar year: the
 * row says so with a mark, and its tooltip splits it by year of exit — the part a cut ending in that
 * year leaves out. In neutral ink with the sign spelled out, like the rest of the run's R.
 */
export function RByYear(props: { run: Backtest }): React.JSX.Element {
  const { run } = props
  if (run.r_by_years === null || run.r_by_years === undefined) {
    return (
      <p className="text-sm text-slate-500">
        This run was recorded before 29/09 and kept no R by year.
      </p>
    )
  }
  const rows = entryYears(run.r_by_years)
  if (rows.length === 0) {
    return <p className="text-sm text-slate-500">No trade was scored in R, so there is no R by year.</p>
  }
  const total = sumR(rows.map((row) => row.r))
  return (
    <div className="space-y-4">
      <div className="max-w-sm overflow-x-auto rounded-lg border border-slate-800">
        <table aria-label="R by year" className="w-full border-collapse text-sm">
          <thead>
            <tr className="border-b border-slate-800">
              <th scope="col" className={`${head} text-left`}>
                Year of entry
              </th>
              <th scope="col" className={head}>
                R
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.year} className="border-b border-slate-800/60">
                <th scope="row" className="px-3 py-2 text-left font-medium">
                  {row.year}
                </th>
                <td className={cell}>
                  {inR(row.r)}
                  {row.leftLater && (
                    <>
                      <span
                        aria-hidden="true"
                        title={exitsOf(row)}
                        className="ml-1 cursor-help text-xs text-slate-500"
                      >
                        ↷
                      </span>
                      <span className="sr-only">({exitsOf(row)})</span>
                    </>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
          <tfoot>
            <tr className="border-t border-slate-700">
              <th scope="row" className="px-3 py-2 text-left font-medium">
                Total
              </th>
              <td className={`${cell} font-medium`}>{inR(total)}</td>
            </tr>
          </tfoot>
        </table>
      </div>
      {rows.some((row) => row.leftLater) && (
        <p className="text-xs text-slate-500">
          ↷ some trades of that year left in a later one — hover it for the split by year of exit.
        </p>
      )}
      <YearCutPicker runId={run.id} years={windowYears(run.date_from, run.date_to)} />
    </div>
  )
}
