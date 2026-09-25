import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCatalog, useCreateSweepTemplate, useSweepTemplates } from '../api/hooks'
import { TIMEFRAMES } from '../strategy/builder'

function toggle(list: readonly string[], value: string): string[] {
  return list.includes(value) ? list.filter((one) => one !== value) : [...list, value]
}

/**
 * Templates (26/09): a sweep without its markets, kept to run one market at a time.
 *
 * His ask, after a sweep over thirteen markets turned into a day and a half of queue: fix what
 * makes the answers comparable — the entries, the charts, the window, the capital — and run the
 * markets one after the other, some today and others another day, then read them together.
 */
export function Templates(): React.JSX.Element {
  const navigate = useNavigate()
  const templates = useSweepTemplates()
  const catalog = useCatalog()
  const create = useCreateSweepTemplate()
  const [name, setName] = useState('')
  const [entryIds, setEntryIds] = useState<string[]>([])
  const [timeframes, setTimeframes] = useState<string[]>([])
  const [dateFrom, setDateFrom] = useState('2009-01-01')
  const [dateTo, setDateTo] = useState('2020-01-01')
  const [capital, setCapital] = useState('10000')

  const why =
    name.trim() === ''
      ? 'Give the template a name.'
      : entryIds.length === 0
        ? 'Choose at least one entry from the shelf.'
        : timeframes.length === 0
          ? 'Choose at least one chart.'
          : dateTo <= dateFrom
            ? 'The window must end after it starts.'
            : !(Number(capital) > 0)
              ? 'The capital must be positive.'
              : null

  const keep = (): void => {
    create.mutate(
      {
        name: name.trim(),
        entry_ids: entryIds,
        timeframes,
        date_from: `${dateFrom}T00:00:00Z`,
        date_to: `${dateTo}T00:00:00Z`,
        initial_capital: capital,
      },
      {
        onSuccess: (made) => {
          void navigate(`/templates/${made.id}`)
        },
      },
    )
  }

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm'
  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Templates</h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-400">
          A sweep without its markets. Keep the entries, charts and window once; queue markets to
          run one after the other — today, tomorrow — and read the finished ones together.
        </p>
      </header>

      <div className="space-y-4 rounded border border-slate-800 p-4">
        <h3 className="font-semibold">New template</h3>
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <label className="flex flex-col gap-1">
            Name
            <input
              value={name}
              onChange={(event) => {
                setName(event.target.value)
              }}
              className={`${input} w-64`}
            />
          </label>
          <label className="flex flex-col gap-1">
            From
            <input
              type="date"
              value={dateFrom}
              onChange={(event) => {
                setDateFrom(event.target.value)
              }}
              className={input}
            />
          </label>
          <label className="flex flex-col gap-1">
            To
            <input
              type="date"
              value={dateTo}
              onChange={(event) => {
                setDateTo(event.target.value)
              }}
              className={input}
            />
          </label>
          <label className="flex flex-col gap-1">
            Capital
            <input
              inputMode="decimal"
              value={capital}
              onChange={(event) => {
                setCapital(event.target.value)
              }}
              className={`${input} w-28`}
            />
          </label>
        </div>

        <fieldset className="space-y-2 text-sm">
          <legend className="text-slate-300">Entries</legend>
          <div className="grid gap-1 sm:grid-cols-2">
            {(catalog.data?.items ?? []).map((entry) => (
              <label key={entry.id} className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={entryIds.includes(entry.id)}
                  onChange={() => {
                    setEntryIds(toggle(entryIds, entry.id))
                  }}
                />
                {entry.name}
                <span className="text-xs text-slate-500">
                  {entry.points === 1 ? 'one point' : `${String(entry.points)} points`}
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <fieldset className="flex flex-wrap gap-3 text-sm">
          <legend className="mb-1 text-slate-300">Charts</legend>
          {TIMEFRAMES.map((timeframe) => (
            <label key={timeframe} className="flex items-center gap-1">
              <input
                type="checkbox"
                checked={timeframes.includes(timeframe)}
                onChange={() => {
                  setTimeframes(toggle(timeframes, timeframe))
                }}
              />
              {timeframe}
            </label>
          ))}
        </fieldset>

        {why !== null && <p className="text-xs text-amber-300">{why}</p>}
        {create.isError && (
          <p role="alert" className="text-sm text-red-400">
            {apiFailure(create.error, 'Could not keep the template.')}
          </p>
        )}
        <button
          type="button"
          disabled={why !== null || create.isPending}
          onClick={keep}
          className="rounded bg-sky-600 px-4 py-2 text-sm font-medium disabled:opacity-40"
        >
          Keep the template
        </button>
      </div>

      <div className="space-y-2">
        <h3 className="font-semibold">Kept</h3>
        {templates.isError ? (
          <p className="text-red-400">Could not load the templates.</p>
        ) : templates.data === undefined ? null : templates.data.length === 0 ? (
          <p className="text-sm text-slate-500">No template yet.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {templates.data.map((one) => (
              <li key={one.id}>
                <Link to={`/templates/${one.id}`} className="text-sky-400 hover:text-sky-300">
                  {one.name}
                </Link>{' '}
                <span className="text-slate-500">
                  · {one.timeframes.join(', ')} · {one.date_from.slice(0, 10)} →{' '}
                  {one.date_to.slice(0, 10)} · {String(one.launched)} run, {String(one.waiting)}{' '}
                  waiting
                  {one.failed > 0 && `, ${String(one.failed)} failed`}
                  {one.paused && ' · paused'}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
