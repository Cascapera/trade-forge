import { useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCatalog, useCreateCatalogEntry, useDeleteCatalogEntry } from '../api/hooks'
import type { StrategyListItem } from '../api/types'
import { GridEditor } from '../components/GridEditor'
import { StrategyPicker } from '../components/StrategyPicker'
import { count } from '../format'
import { filterCatalogue, gridSummary } from '../strategy/catalogue'
import { gridOf, type Axis } from '../study/settings'

const inputClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1.5 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/** The calendar day of an ISO instant — the granularity a catalogue is read at. */
function day(iso: string): string {
  return iso.slice(0, 10)
}

/**
 * The shelf: strategies under names somebody wrote, each with the sweep it is meant to be asked.
 *
 * ⚠️ **This screen used to list `strategies`, and that was the wrong list.** It is the right
 * answer to "what documents does this database hold" and a useless answer to "what is worth
 * running": those names are generated columns projected out of the documents, so they are
 * whatever the builder stamped — eleven rows of `MME9-20260910-172055` here. That observation
 * is why `catalog_entries` exists.
 *
 * The strategy is still shown on every row, **beside** the label rather than instead of it.
 * Both are facts and they are different ones: a reader who cannot see the document behind a
 * label cannot tell two entries over one strategy apart, and a reader who sees only the
 * document is back where this screen started.
 */
export function StrategyCatalog(): React.JSX.Element {
  const [text, setText] = useState('')
  const [adding, setAdding] = useState(false)

  const catalog = useCatalog()
  const items = useMemo(() => catalog.data?.items ?? [], [catalog.data])
  // The same matcher the strategy list used, generic over anything carrying a name and a
  // setup — reused rather than re-derived. A second matcher would agree with this one until
  // the day somebody fixed only one of them.
  const rows = useMemo(() => {
    return filterCatalogue(items, text)
  }, [items, text])

  return (
    <section className="flex flex-col gap-6">
      <header className="flex flex-col gap-1">
        <h2 className="text-xl font-semibold">Strategy catalogue</h2>
        <p className="max-w-2xl text-sm text-slate-400">
          The strategies worth running, under names you chose, each with the sweep it is meant to
          be asked. An entry with nothing to vary is a single backtest.
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
            className={`${inputClass} w-64`}
          />
        </label>
        <button
          type="button"
          onClick={() => {
            setAdding((open) => !open)
          }}
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-500"
        >
          {adding ? 'Cancel' : 'Add to the catalogue'}
        </button>
        {catalog.data !== undefined && (
          <p className="pb-1.5 text-xs text-slate-500">
            {count(rows.length)} of {count(items.length)} shown
          </p>
        )}
      </div>

      {adding && (
        <NewEntry
          onSaved={() => {
            setAdding(false)
          }}
        />
      )}

      {/* ⚠️ A read that failed and an empty shelf are different facts, and `isError` comes first
          for exactly that reason. Falling through to "the shelf is empty" would print an
          unanswered question as an answer, and invite adding to a catalogue that may be full. */}
      {catalog.isError ? (
        <p className="text-sm text-red-400">Could not load the catalogue.</p>
      ) : catalog.isPending ? (
        <p className="text-sm text-slate-400">Loading…</p>
      ) : items.length === 0 ? (
        <p className="text-sm text-slate-400">
          The shelf is empty. Add a strategy you have saved and give it a name you will recognise.
        </p>
      ) : rows.length === 0 ? (
        <p className="text-sm text-slate-400">No entry matches that.</p>
      ) : (
        <ul className="flex flex-col gap-2">
          {rows.map((entry) => (
            <li
              key={entry.id}
              className="flex flex-wrap items-start justify-between gap-3 rounded-lg border border-slate-800 px-4 py-3"
            >
              <div className="flex min-w-0 flex-col gap-1">
                <div className="flex flex-wrap items-baseline gap-2">
                  <span className="font-medium text-slate-100">{entry.name}</span>
                  {/* One backtest is said in words rather than as a bare `1`, because a number
                      beside a label reads as a count of results until it says what it counts. */}
                  <span className="text-xs text-slate-500">
                    {entry.points === 1 ? 'one backtest' : `${count(entry.points)} backtests`}
                  </span>
                </div>
                {entry.description !== null && (
                  <p className="max-w-2xl text-sm text-slate-400">{entry.description}</p>
                )}
                {Object.keys(entry.grid).length > 0 && (
                  <p className="font-mono text-xs text-slate-500">{gridSummary(entry.grid)}</p>
                )}
                <p className="text-xs text-slate-500">
                  {/* ⚠️ The setup comes from the document, never from either name. This database
                      holds `Structure — CHoCH 56454` running `mme9_breakout`. */}
                  <span className="font-mono">{entry.setup ?? 'DSL'}</span>{' '}
                  <Link
                    to={`/strategies/${entry.strategy_id}`}
                    className="text-sky-400 hover:underline"
                  >
                    {entry.strategy_name}
                  </Link>{' '}
                  v{entry.strategy_version} · saved {day(entry.created_at)}
                </p>
              </div>
              <RemoveEntry id={entry.id} name={entry.name} />
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

/**
 * Take a label off the shelf.
 *
 * Two clicks, and the second says what it removes. ⚠️ It also says what it does **not**: the
 * strategy and every run of it survive, because a run answers "what did I execute?" by pointing
 * at an immutable document. A reader who thinks this deletes their results will not press it,
 * and one who assumes it deletes nothing will press it expecting less than it does.
 */
function RemoveEntry(props: { id: string; name: string }): React.JSX.Element {
  const [asked, setAsked] = useState(false)
  const remove = useDeleteCatalogEntry()

  if (!asked) {
    return (
      <button
        type="button"
        onClick={() => {
          setAsked(true)
        }}
        className="text-xs text-slate-500 hover:text-red-400"
      >
        Remove
      </button>
    )
  }
  return (
    <div className="flex flex-col items-end gap-1">
      <span className="text-xs text-slate-400">
        Remove the label? The strategy and its runs stay.
      </span>
      <div className="flex gap-2">
        <button
          type="button"
          onClick={() => {
            remove.mutate(props.id)
          }}
          disabled={remove.isPending}
          className="rounded bg-red-700 px-2 py-1 text-xs text-white disabled:bg-slate-700"
        >
          {remove.isPending ? 'Removing…' : `Remove ${props.name}`}
        </button>
        <button
          type="button"
          onClick={() => {
            setAsked(false)
          }}
          className="text-xs text-slate-500 hover:text-slate-300"
        >
          Keep it
        </button>
      </div>
      {remove.isError && (
        <span className="text-xs text-red-400">
          {apiFailure(remove.error, 'Could not remove that entry.')}
        </span>
      )}
    </div>
  )
}

/** Put a saved strategy on the shelf, with a name, a description and the sweep to remember. */
function NewEntry(props: { onSaved: () => void }): React.JSX.Element {
  const [strategy, setStrategy] = useState<StrategyListItem | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [axes, setAxes] = useState<Axis[]>([{ path: '', raw: '' }])
  const create = useCreateCatalogEntry()

  const grid = gridOf(axes)
  // The empty product, which is 1: an entry with nothing to vary is still one backtest. The
  // same arithmetic the server reports back, so the number does not change on save.
  const points = Object.values(grid).reduce((total, values) => total * values.length, 1)
  const blocked =
    strategy === null
      ? 'Choose a strategy first.'
      : name.trim() === ''
        ? 'Give it a name you will recognise.'
        : null

  return (
    <form
      className="flex flex-col gap-4 rounded-lg border border-slate-800 p-4"
      onSubmit={(event) => {
        event.preventDefault()
        if (strategy === null || blocked !== null) return
        create.mutate(
          {
            name: name.trim(),
            // ⚠️ Omitted rather than sent empty. `null` means nobody wrote a description and
            // `''` means somebody wrote nothing; the column keeps them apart, and the list above
            // reads the difference when it decides whether to show a subtitle.
            ...(description.trim() === '' ? {} : { description: description.trim() }),
            strategy_id: strategy.id,
            grid,
          },
          { onSuccess: props.onSaved },
        )
      }}
    >
      <StrategyPicker
        value={strategy?.id ?? ''}
        onChange={(chosen) => {
          setStrategy(chosen)
          // The axes belong to the setup that was just replaced, so keeping them would leave
          // paths the new document has nothing at — and the save would be refused for a reason
          // the reader did not cause.
          setAxes([{ path: '', raw: '' }])
        }}
      />
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        Name
        <input
          className={inputClass}
          value={name}
          placeholder="9.1 sem filtro"
          onChange={(event) => {
            setName(event.target.value)
          }}
        />
      </label>
      <label className="flex flex-col gap-1 text-sm text-slate-300">
        Description
        <textarea
          className={inputClass}
          rows={2}
          value={description}
          placeholder="What this one is for, in your own words."
          onChange={(event) => {
            setDescription(event.target.value)
          }}
        />
      </label>

      <GridEditor
        setup={strategy?.setup ?? null}
        axes={axes}
        onChange={setAxes}
        legend="Parameters to sweep (optional)"
      />

      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          disabled={blocked !== null || create.isPending}
          className="rounded bg-sky-600 px-4 py-2 text-sm font-medium text-white disabled:cursor-not-allowed disabled:bg-slate-700"
        >
          {create.isPending ? 'Saving…' : 'Save to the catalogue'}
        </button>
        <span className="text-xs text-slate-500">
          {points === 1 ? 'One backtest per sweep' : `${count(points)} backtests per sweep`}
        </span>
        {blocked !== null && <span className="text-xs text-slate-500">{blocked}</span>}
      </div>
      {/* The server's own sentence, through the shared reader — a refused grid names the axis
          that caused it, and a taken name arrives as a 409 carrying the name. */}
      {create.isError && (
        <p className="text-sm text-red-400">
          {apiFailure(create.error, 'Could not save that entry.')}
        </p>
      )}
    </form>
  )
}
