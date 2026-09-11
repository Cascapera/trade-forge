import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { useStrategies } from '../api/hooks'
import { count } from '../format'
import { filterCatalogue, setupsIn } from '../strategy/catalogue'

/** The calendar day of an ISO instant — the granularity a catalogue is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

/**
 * Every strategy that has been saved — the shelf the experiments are launched from.
 *
 * **Why this is a list of what is in the database rather than a list written in this file.** The
 * obvious version of a "ready-made strategies" page is an array of documents in the front end,
 * and it would put the screen on the wrong side of the one rule the rest of this project keeps:
 * the DSL has a single source of truth, and anything describing a strategy twice drifts from it
 * in silence. Strategies already live in Postgres, already carry a version, and are already
 * written by the builder. The catalogue reads that shelf; it does not keep its own.
 *
 * ⚠️ **A grid's own points are absent, and that is what makes this page readable at all.** A
 * hundred-point study writes a hundred strategies named `MME9 [period=5, rr=2]`, and the server
 * leaves them out by default — see `GET /strategies`. Without that, forty-five authored
 * strategies would sit under a thousand generated ones.
 *
 * ⚠️ **What is missing here is the ranges, and it is missing on purpose.** A catalogue entry that
 * says "9.1 over every average period from 5 to 21" is a strategy *and a grid*, and a grid today
 * is typed into the study screen and dies with the launch — nothing persists it. Saving one is
 * the next piece of work, not something this screen can fake by listing a document as if it
 * carried a search it does not carry.
 */
export function StrategyCatalog(): React.JSX.Element {
  const [text, setText] = useState('')
  const [setup, setSetup] = useState('')

  // One request for the whole shelf, filtered in the browser. The alternative is `?q=` on every
  // keystroke, which is a round trip per letter to re-sort forty-five rows — and it could only
  // ever filter by name, because the server has no filter for the setup. The cap is real rather
  // than assumed: `total` says how many lineages exist, and the page says so when it is holding
  // fewer than that instead of quietly presenting a slice as the whole catalogue.
  const strategies = useStrategies({ limit: 200 })
  const items = useMemo(() => strategies.data?.items ?? [], [strategies.data])
  const setups = useMemo(() => setupsIn(items), [items])
  const rows = useMemo(() => filterCatalogue(items, text, setup), [items, text, setup])
  const total = strategies.data?.total ?? 0
  const truncated = total > items.length

  return (
    <section className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-xl font-semibold">Strategy catalogue</h2>
        <p className="max-w-2xl text-sm text-slate-400">
          Every strategy saved in this database, newest first. Open one to read or edit it — an
          edit is saved as the next version, so what a past run executed never changes underneath
          it.
        </p>
      </header>

      <div className="flex flex-wrap items-end gap-3">
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Search
          <input
            type="search"
            value={text}
            onChange={(event) => {
              setText(event.target.value)
            }}
            placeholder="name or setup"
            className="w-64 rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100 focus:border-sky-500 focus:outline-none"
          />
        </label>
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Setup
          <select
            value={setup}
            onChange={(event) => {
              setSetup(event.target.value)
            }}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100"
          >
            <option value="">All</option>
            {setups.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>
        </label>
        {/* Only once there is something to count. "0 of 0 shown" beside a failed read is a
            measurement of a list nobody ever received. */}
        {strategies.data !== undefined && (
          <p className="pb-1.5 text-xs text-slate-500">
            {count(rows.length)} of {count(items.length)} shown
            {truncated && ` · ${count(total)} saved, showing the newest ${count(items.length)}`}
          </p>
        )}
      </div>

      {/* ⚠️ A read that failed and a shelf that is empty are different facts, and the branch below
          is written as `isError` first for exactly that reason. Falling through to "nothing saved
          yet" would print an unanswered question as an answer — it invites building a strategy
          over a database that may hold forty-five. */}
      {strategies.isError ? (
        <p className="text-sm text-red-400">Could not load the catalogue.</p>
      ) : strategies.isPending ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-400">
          Nothing saved yet. Build one under <Link to="/" className="text-sky-400">New backtest</Link>{' '}
          and it appears here.
        </p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">No strategy matches that.</p>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-800">
          <table className="w-full min-w-[44rem] border-collapse text-left text-sm">
            <thead>
              <tr className="border-b border-slate-800 text-xs font-medium text-slate-400">
                <th scope="col" className="px-3 py-2">
                  Strategy
                </th>
                <th scope="col" className="px-3 py-2">
                  Setup
                </th>
                <th scope="col" className="px-3 py-2 text-right">
                  Version
                </th>
                <th scope="col" className="px-3 py-2 text-right">
                  Runs
                </th>
                <th scope="col" className="px-3 py-2">
                  Saved
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((item) => (
                <tr key={item.id} className="border-b border-slate-900 last:border-0">
                  <td className="px-3 py-2">
                    <Link to={`/strategies/${item.id}`} className="text-sky-400 hover:underline">
                      {item.name}
                    </Link>
                  </td>
                  {/* ⚠️ The setup is read from the document, never from the name. A name is typed
                      by a person and a setup is executed by the engine, so only one of them is
                      evidence of what a row will actually do. */}
                  <td className="px-3 py-2 font-mono text-xs text-slate-300">
                    {item.setup ?? 'DSL'}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                    v{item.version}
                  </td>
                  {/* Never run is said in words rather than as a zero: it is the one number here
                      that separates a strategy from an abandoned draft. */}
                  <td className="px-3 py-2 text-right tabular-nums text-slate-300">
                    {item.runs === 0 ? <span className="text-slate-500">never run</span> : count(item.runs)}
                  </td>
                  <td className="px-3 py-2 text-slate-400">{day(item.created_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  )
}
