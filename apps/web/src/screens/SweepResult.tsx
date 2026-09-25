import { useMemo, useState } from 'react'
import { useParams } from 'react-router-dom'

import { apiUrl } from '../api/client'
import { isSweepSettled, useEquityCurves, useSweep, useSweepRuns } from '../api/hooks'
import type { BacktestListItem, SweepEntryOut } from '../api/types'
import {
  EMPTY_SEATS,
  MAX_COMPARED,
  type Seats,
  buildSeries,
  selectedIds,
  toggleSeat,
} from '../backtest/compare'
import { ComparisonChart } from '../components/ComparisonChart'
import { FailedDownloads } from '../components/FailedDownloads'
import { HoldoutComparison } from '../components/HoldoutComparison'
import { HoldoutLauncher } from '../components/HoldoutLauncher'
import { SweepWalkForwardLauncher } from '../components/SweepWalkForwardLauncher'
import { RunTable } from '../components/RunTable'
import { StudyDispersion } from '../components/StudyDispersion'
import { SweepTargets } from '../components/TargetLadder'
import { money } from '../format'
import { settled, summarise, tally } from '../sweep/progress'
import { RANKINGS, RUNS_PER_PAGE, type RankKey, rankingOf } from '../sweep/ranking'

/** The calendar day of an ISO instant — the granularity a window is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

function backtests(count: number): string {
  return `${String(count)} backtest${count === 1 ? '' : 's'}`
}

function Pager(props: {
  label: string
  index: number
  pages: number
  onPage: (index: number) => void
}): React.JSX.Element | null {
  if (props.pages <= 1) return null
  const button = 'rounded border border-slate-700 px-3 py-1 disabled:opacity-40'
  return (
    <nav aria-label={props.label} className="flex items-center gap-3 text-sm">
      <button
        type="button"
        disabled={props.index === 0}
        onClick={() => {
          props.onPage(props.index - 1)
        }}
        className={button}
      >
        ← Better
      </button>
      <span className="text-slate-400">
        Page {String(props.index + 1)} of {String(props.pages)}
      </span>
      <button
        type="button"
        disabled={props.index >= props.pages - 1}
        onClick={() => {
          props.onPage(props.index + 1)
        }}
        className={button}
      >
        Worse →
      </button>
    </nav>
  )
}

/**
 * One entry of a sweep: its summary, then its runs a ranked page at a time.
 *
 * ⚠️ **The page comes from the server, ranked there** (24/09). The screen used to hold every run
 * and sort them itself; on a sweep of 22 thousand runs that was 89 MB a poll. The summary above
 * the table is still the whole entry's — the server computes it over every run.
 */
function EntryRuns(props: {
  sweepId: string
  entry: SweepEntryOut
  rankBy: RankKey
  index: number
  onPage: (index: number) => void
  polling: boolean
  seats: Seats
  onToggle: (run: BacktestListItem) => void
}): React.JSX.Element {
  const { entry } = props
  const runs = useSweepRuns(
    props.sweepId,
    {
      entryId: entry.entry_id,
      rankBy: props.rankBy,
      offset: props.index * RUNS_PER_PAGE,
      limit: RUNS_PER_PAGE,
    },
    props.polling,
  )
  const heading = `sweep-entry-${entry.entry_id}`
  const name = entry.entry_name ?? 'An entry since removed from the shelf'
  const total = runs.data?.total ?? 0
  const items = (runs.data?.items ?? []).map((row) => row.run)
  const pages = Math.max(1, Math.ceil(total / RUNS_PER_PAGE))
  const first = items.length === 0 ? 0 : props.index * RUNS_PER_PAGE + 1
  const last = first === 0 ? 0 : first + items.length - 1
  return (
    <section aria-labelledby={heading} className="space-y-3 border-t border-slate-800 pt-5">
      <div>
        <h3 id={heading} className="text-lg font-medium text-sky-400">
          {name}
        </h3>
        <p className="text-sm text-slate-400">{backtests(total)}</p>
      </div>
      <StudyDispersion aggregate={entry.aggregate} />
      {/* Every target scored from how far the trades went: the sweep ran once, without one. */}
      <SweepTargets rungs={entry.targets} />
      {runs.isError ? (
        <p className="text-sm text-red-400">Could not load this entry&apos;s runs.</p>
      ) : (
        <p className="text-xs text-slate-500">
          {first === 0
            ? runs.isPending
              ? 'Loading the runs…'
              : 'No runs yet.'
            : `Runs ${String(first)}–${String(last)} of ${String(total)}, best ${rankingOf(props.rankBy).label.toLowerCase()} first. Runs with nothing to rank by — unfinished, failed, or without this measure — come last.`}
        </p>
      )}
      <RunTable
        runs={items}
        seats={props.seats}
        onToggle={(runId) => {
          const run = items.find((one) => one.id === runId)
          if (run !== undefined) props.onToggle(run)
        }}
      />
      <Pager label={`${name} pages`} index={props.index} pages={pages} onPage={props.onPage} />
    </section>
  )
}

/**
 * One sweep read back: every entry it ran, each summarised on its own.
 *
 * ⚠️ **A section per entry, and never one summary for the whole sweep.** The entries of a sweep
 * are alternatives — `9.1 sem filtro` beside `choch com filtro H4` — so a median across them is the
 * median of two methods and describes neither. The server summarises each entry separately for
 * that reason, and this screen keeps the separation visible instead of adding the sections up.
 *
 * Inside a section the dispersion comes first, led by the median, and only then the runs, ranked
 * best first by the measure the reader picks, ten at a time. A sweep searches more than a study
 * does, so its best is the best of more draws: the list starts from it because that is what the
 * reader asked to browse, and the median above is what keeps it from reading as the result.
 *
 * ⚠️ **One chart for the whole screen, above the sections, rather than one per entry.** The
 * comparison worth making often crosses entries — the median run of one method against the
 * median of the other — and a chart inside each section could never hold both. Nothing seats
 * itself, as on a study: at this scale whichever runs finished first would fill every seat.
 */
export function SweepResult(): React.JSX.Element {
  const { id } = useParams<{ id: string }>()
  const sweep = useSweep(id)
  const [seats, setSeats] = useState(EMPTY_SEATS)
  const [rankBy, setRankBy] = useState<RankKey>('return')
  // One page per entry, by entry id. Changing the measure starts every entry from its best again.
  const [pageOfEntry, setPageOfEntry] = useState<Record<string, number>>({})

  // ⚠️ **The runs on the chart are kept here, not looked up in a page.** A page holds ten runs
  // and changes as the reader pages and the sweep runs; a run ticked on page one must stay on the
  // chart while page three is on screen.
  const [pinned, setPinned] = useState<ReadonlyMap<string, BacktestListItem>>(new Map())
  const picked = useMemo(() => selectedIds(seats), [seats])
  const { curves } = useEquityCurves(picked)
  const series = useMemo(
    () => buildSeries(seats, [...pinned.values()], curves),
    [seats, pinned, curves],
  )

  function toggle(run: BacktestListItem): void {
    setSeats((current) => toggleSeat(current, run.id))
    setPinned((current) => new Map(current).set(run.id, run))
  }

  if (sweep.isPending) return <p className="text-slate-400">Loading the sweep…</p>
  if (sweep.isError) {
    return <p className="text-red-400">Could not load this sweep.</p>
  }

  const data = sweep.data
  const counts = data.counts ?? tally(data.runs)

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-xl font-semibold">
          {data.entries.length} {data.entries.length === 1 ? 'entry' : 'entries'} over{' '}
          {backtests(counts.total)}
        </h2>
        <p className="text-sm text-slate-400">
          {data.symbols.join(', ')} · {data.timeframes.join(', ')} · {day(data.date_from)} →{' '}
          {day(data.date_to)} · {money(data.initial_capital)} per run
        </p>
        {/* ⚠️ **Right under the axes, because the axes are what was asked.** A pair left out for
            having no candles has no runs, and without this line the header above reads as the
            space that was measured — a map with a hole passing for a complete one. */}
        {data.skipped.length > 0 && (
          <div role="status" aria-label="left out" className="mt-2 text-sm text-amber-300">
            <p>
              Left out — no candles in this window for {data.skipped.length}{' '}
              {data.skipped.length === 1 ? 'pair' : 'pairs'}:
            </p>
            <ul className="mt-1 space-y-0.5 text-xs text-amber-300/80">
              {data.skipped.map((pair) => (
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
        <div className="mt-2">
          <FailedDownloads downloads={data.failed_collections} />
        </div>
        {/* ⚠️ The file and its legend, always together. A dataset handed over without the
            dictionary is the half an AI misreads — it cannot tell a parameter from a result, or
            an in-sample return from a forecast, by the column's name. Opened by the browser
            itself: a download and a document, not a response for React Query to hold. */}
        <p className="mt-2 text-sm">
          <a
            href={apiUrl(`/sweeps/${data.id}/dataset.csv`)}
            download
            className="text-sky-400 underline hover:text-sky-300"
          >
            Download the dataset (CSV)
          </a>
          <span className="text-slate-500"> · </span>
          <a
            href={apiUrl(`/sweeps/${data.id}/dataset/dictionary`)}
            target="_blank"
            rel="noreferrer"
            className="text-sky-400 underline hover:text-sky-300"
          >
            What each column means
          </a>
        </p>
      </div>

      {/* A test reads against the sweep it came from; a finished sweep offers to be tested. Not
          offered on a test itself — its points would be chosen on the reserved window — and not
          before every run has landed, when "the best" is still moving. */}
      {data.holdout_rule !== undefined && data.holdout_rule !== null ? (
        <HoldoutComparison sweepId={data.id} />
      ) : (
        isSweepSettled(data) && (
          <>
            <HoldoutLauncher sweep={data} />
            <SweepWalkForwardLauncher sweep={data} />
          </>
        )
      )}

      {/* ⚠️ **Always on screen, and waiting kept apart from executing.** The line this replaced
          read `1 of 10 backtests still running`, vanished once nothing was outstanding, and
          counted a queued run as running — so a run a worker will never pick up (one took its job
          before Postgres accepted connections) was "still running" for ever. Now it says
          `9 of 10 done · 1 queued` and keeps saying it.

          ⚠️ Not solved here: a stuck run still reads as queued, and the suffix below still says
          this updates on its own. Telling stuck from waiting needs the worker fix, not the screen.

          A live region only while something is in flight: a settled sweep has nothing left to
          announce, and a `role="status"` that never changes is noise for a screen reader. */}
      <p
        role={settled(counts) ? undefined : 'status'}
        className={`text-sm ${settled(counts) ? 'text-slate-400' : 'text-sky-300'}`}
      >
        {summarise(counts)}
        {settled(counts) ? '' : ' — this updates on its own.'}
      </p>

      {/* ⚠️ The median sits above every ranked list, in each entry's summary: the best of a
          sweep is the best of its whole search, and a list that starts from it reads as a result
          unless the typical run is on screen first. */}
      <label className="flex w-fit flex-col gap-1 text-xs text-slate-400">
        Rank each entry's runs by
        <select
          value={rankBy}
          onChange={(event) => {
            setRankBy(event.target.value as RankKey)
            setPageOfEntry({})
          }}
          className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
        >
          {RANKINGS.map((one) => (
            <option key={one.key} value={one.key}>
              {one.label}
            </option>
          ))}
        </select>
      </label>

      <section className="space-y-2">
        <h3 className="font-medium">Equity of the runs you pick, as percent of starting capital</h3>
        <ComparisonChart series={series} />
        <p className="text-xs text-slate-500">
          {series.length === 0
            ? `Tick up to ${String(MAX_COMPARED)} runs in the tables below to compare their curves — across entries too.`
            : `Up to ${String(MAX_COMPARED)} runs stay tellable apart by colour; untick one to add another.`}
        </p>
      </section>

      {data.entries.map((entry) => (
        <EntryRuns
          key={entry.entry_id}
          sweepId={data.id}
          entry={entry}
          rankBy={rankBy}
          index={pageOfEntry[entry.entry_id] ?? 0}
          onPage={(index) => {
            setPageOfEntry((current) => ({ ...current, [entry.entry_id]: index }))
          }}
          polling={!settled(counts)}
          seats={seats}
          onToggle={toggle}
        />
      ))}
    </div>
  )
}
