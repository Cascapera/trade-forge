import { useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useClusters, useCreateCluster } from '../api/hooks'
import { percent } from '../format'

/** A member as the form holds it: the run, what to call it, and a risk typed by hand or blank. */
export interface DraftMember {
  backtest_id: string
  label: string
  risk: string
}

/**
 * The members this page was opened with — from a reserved-window test's "all points" or "all
 * passed" — or none. Read from the navigation, like a launch's skipped markets: a reload starts an
 * empty form, which says nothing false.
 */
function membersFrom(state: unknown): { name: string; members: DraftMember[] } {
  if (state === null || typeof state !== 'object' || !('members' in state)) {
    return { name: '', members: [] }
  }
  const raw = (state as { members: unknown; name?: unknown }).members
  const name = (state as { name?: unknown }).name
  const members = Array.isArray(raw)
    ? raw
        .filter(
          (one): one is { backtest_id: string; label?: unknown } =>
            typeof one === 'object' &&
            one !== null &&
            typeof (one as { backtest_id?: unknown }).backtest_id === 'string',
        )
        .map((one) => ({
          backtest_id: one.backtest_id,
          label: typeof one.label === 'string' ? one.label : one.backtest_id,
          risk: '',
        }))
    : []
  return { name: typeof name === 'string' ? name : '', members }
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** A number typed by hand: blank is allowed when `blank` is, anything else must be > 0. */
function positive(value: string, blank: boolean): boolean {
  if (value.trim() === '') return blank
  const number = Number(value)
  return Number.isFinite(number) && number > 0
}

/**
 * Clusters: several finished runs replayed on ONE shared account (25/09).
 *
 * The members usually arrive from a reserved-window test ("all points", or the points a slicing
 * passed); one can also be added by its run id. Each trades at its own document's risk unless a
 * risk is typed, and the portfolio's limits — open positions, open risk — decide what is taken.
 */
export function Clusters(): React.JSX.Element {
  const location = useLocation()
  const navigate = useNavigate()
  const clusters = useClusters()
  const create = useCreateCluster()
  const [draft] = useState(() => membersFrom(location.state))
  const [name, setName] = useState(draft.name)
  const [members, setMembers] = useState<DraftMember[]>(draft.members)
  const [capital, setCapital] = useState('10000')
  const [maxOpen, setMaxOpen] = useState('5')
  const [maxRisk, setMaxRisk] = useState('5')
  const [adding, setAdding] = useState('')

  const addingOk = UUID.test(adding.trim())
  const already = members.some((one) => one.backtest_id === adding.trim())
  const risksOk = members.every((one) => positive(one.risk, true) && Number(one.risk || 0) <= 100)
  const why =
    name.trim() === ''
      ? 'Give the cluster a name.'
      : members.length === 0
        ? 'Add at least one member.'
        : !risksOk
          ? 'A member risk is a percent above 0 and at most 100, or blank for its own.'
          : !positive(capital, false)
            ? 'The capital must be positive.'
            : !(Number.isInteger(Number(maxOpen)) && Number(maxOpen) >= 1)
              ? 'Open positions is a whole number of at least 1.'
              : !(positive(maxRisk, false) && Number(maxRisk) <= 100)
                ? 'Open risk is a percent above 0 and at most 100.'
                : null

  const launch = (): void => {
    create.mutate(
      {
        name: name.trim(),
        members: members.map((one) => ({
          backtest_id: one.backtest_id,
          ...(one.risk.trim() === '' ? {} : { risk_percent: one.risk.trim() }),
        })),
        initial_capital: capital,
        max_open_positions: Number(maxOpen),
        max_open_risk_percent: maxRisk,
      },
      {
        onSuccess: (made) => {
          void navigate(`/clusters/${made.id}`)
        },
      },
    )
  }

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm'
  return (
    <section className="space-y-6">
      <header>
        <h2 className="text-lg font-semibold text-slate-100">Clusters</h2>
        <p className="mt-1 max-w-3xl text-sm text-slate-400">
          Several finished runs on one shared account: each trade sized on the shared balance at
          its member&apos;s risk, the portfolio&apos;s limits deciding what is taken, and open
          positions marked on their own bars — so the combined drawdown is the one the account
          would have lived.
        </p>
      </header>

      <div className="space-y-4 rounded border border-slate-800 p-4">
        <h3 className="font-semibold">New cluster</h3>
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
          <label className="flex flex-col gap-1">
            Open positions, at most
            <input
              inputMode="numeric"
              value={maxOpen}
              onChange={(event) => {
                setMaxOpen(event.target.value)
              }}
              className={`${input} w-24`}
            />
          </label>
          <label className="flex flex-col gap-1">
            Open risk, at most (%)
            <input
              inputMode="decimal"
              value={maxRisk}
              onChange={(event) => {
                setMaxRisk(event.target.value)
              }}
              className={`${input} w-24`}
            />
          </label>
        </div>

        <table className="w-full border-collapse text-left text-sm">
          <caption className="mb-2 text-left text-slate-300">
            Members — {String(members.length)}
          </caption>
          <thead>
            <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
              <th scope="col" className="px-3 py-2">
                Run
              </th>
              <th scope="col" className="px-3 py-2">
                Risk per trade (%)
              </th>
              <th scope="col" className="px-3 py-2">
                <span className="sr-only">Remove</span>
              </th>
            </tr>
          </thead>
          <tbody>
            {members.map((member) => (
              <tr key={member.backtest_id} className="border-b border-slate-900">
                <td className="px-3 py-2">
                  <Link to={`/results/${member.backtest_id}`} className="hover:underline">
                    {member.label}
                  </Link>
                </td>
                <td className="px-3 py-2">
                  <input
                    aria-label={`risk of ${member.label}`}
                    inputMode="decimal"
                    placeholder="its own"
                    value={member.risk}
                    onChange={(event) => {
                      const risk = event.target.value
                      setMembers((current) =>
                        current.map((one) =>
                          one.backtest_id === member.backtest_id ? { ...one, risk } : one,
                        ),
                      )
                    }}
                    className={`${input} w-24`}
                  />
                </td>
                <td className="px-3 py-2">
                  <button
                    type="button"
                    aria-label={`Remove ${member.label}`}
                    onClick={() => {
                      setMembers((current) =>
                        current.filter((one) => one.backtest_id !== member.backtest_id),
                      )
                    }}
                    className="text-slate-500 hover:text-red-400"
                  >
                    ×
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <div className="flex flex-wrap items-end gap-2 text-sm">
          <label className="flex flex-col gap-1">
            Add a run by its id
            <input
              value={adding}
              placeholder="the id on its result page"
              onChange={(event) => {
                setAdding(event.target.value)
              }}
              className={`${input} w-96 font-mono`}
            />
          </label>
          <button
            type="button"
            disabled={!addingOk || already}
            onClick={() => {
              const id = adding.trim()
              setMembers((current) => [...current, { backtest_id: id, label: id, risk: '' }])
              setAdding('')
            }}
            className="rounded border border-slate-700 px-3 py-1.5 disabled:opacity-40"
          >
            Add
          </button>
          <span className="text-xs text-slate-500">
            Or open a reserved-window test and build one from its points. A run that kept no trades
            is run again first, and the cluster waits for it.
          </span>
        </div>

        {why !== null && <p className="text-xs text-amber-300">{why}</p>}
        {create.isError && (
          <p role="alert" className="text-sm text-red-400">
            {apiFailure(create.error, 'Could not build the cluster.')}
          </p>
        )}
        <button
          type="button"
          disabled={why !== null || create.isPending}
          onClick={launch}
          className="rounded bg-sky-600 px-4 py-2 text-sm font-medium disabled:opacity-40"
        >
          {create.isPending ? 'Building…' : 'Replay on one account'}
        </button>
      </div>

      <div className="space-y-2">
        <h3 className="font-semibold">Built so far</h3>
        {clusters.isError ? (
          <p className="text-red-400">Could not load the clusters.</p>
        ) : clusters.data === undefined ? null : clusters.data.length === 0 ? (
          <p className="text-sm text-slate-500">No cluster yet.</p>
        ) : (
          <ul className="space-y-1 text-sm">
            {clusters.data.map((one) => (
              <li key={one.id}>
                <Link to={`/clusters/${one.id}`} className="text-sky-400 hover:text-sky-300">
                  {one.name}
                </Link>{' '}
                <span className="text-slate-500">
                  · {String(one.members)} members · {one.status}
                  {one.net_return !== null && ` · ${percent(one.net_return)}`}
                  {one.max_drawdown_pct !== null &&
                    ` · deepest fall ${percent(one.max_drawdown_pct)}`}
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
    </section>
  )
}
