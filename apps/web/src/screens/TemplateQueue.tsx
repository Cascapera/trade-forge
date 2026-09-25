import { useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCombineSweeps, useInstruments, useSweepTemplate, useTemplateQueue } from '../api/hooks'
import type { QueueMarket, TemplateItem } from '../api/types'

/** A market being prepared for the queue: ticked, with its costs as typed so far. */
interface Draft {
  spread: string
  commission: string
  swapLong: string
  swapShort: string
}

const STATUS_LABEL: Record<TemplateItem['status'], string> = {
  waiting: 'waiting',
  launched: 'running',
  failed: 'not launched',
  removed: 'removed',
}

function stage(item: TemplateItem): string {
  if (item.status === 'launched' && item.finished) return 'finished'
  return STATUS_LABEL[item.status]
}

function measured(spread: string | null): string {
  return spread === null ? '' : String(Number(spread))
}

/**
 * One template's queue (26/09): markets added with their costs, run one after the other, and the
 * finished ones read together for the second phase.
 *
 * ⚠️ **The spread comes filled with the market's measured one**, his to correct; a blank is the
 * measured one too. Commission and swap start blank — nothing is charged that was not typed.
 */
export function TemplateQueue(): React.JSX.Element {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const template = useSweepTemplate(id)
  const instruments = useInstruments()
  const queue = useTemplateQueue(id)
  const combine = useCombineSweeps()
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [chosen, setChosen] = useState<string[]>([])

  if (template.isPending) return <p className="text-slate-400">Loading the template…</p>
  if (template.isError) return <p className="text-red-400">Could not load the template.</p>
  const data = template.data

  const tick = (symbol: string, spread: string | null): void => {
    setDrafts((current) => {
      if (symbol in current) {
        return Object.fromEntries(Object.entries(current).filter(([key]) => key !== symbol))
      }
      return {
        ...current,
        [symbol]: { spread: measured(spread), commission: '', swapLong: '', swapShort: '' },
      }
    })
  }
  const edit = (symbol: string, patch: Partial<Draft>): void => {
    setDrafts((current) => {
      const draft = current[symbol]
      return draft === undefined ? current : { ...current, [symbol]: { ...draft, ...patch } }
    })
  }
  const add = (): void => {
    const markets: QueueMarket[] = Object.entries(drafts).map(([symbol, draft]) => ({
      symbol,
      ...(draft.spread.trim() === '' ? {} : { spread_points: draft.spread.trim() }),
      ...(draft.commission.trim() === '' ? {} : { commission_per_unit: draft.commission.trim() }),
      ...(draft.swapLong.trim() === '' ? {} : { swap_long_per_lot: draft.swapLong.trim() }),
      ...(draft.swapShort.trim() === '' ? {} : { swap_short_per_lot: draft.swapShort.trim() }),
    }))
    queue.add.mutate(markets, {
      onSuccess: () => {
        setDrafts({})
      },
    })
  }
  const together = (): void => {
    combine.mutate(chosen, {
      onSuccess: (made) => {
        void navigate(`/sweeps/${made.id}`)
      },
    })
  }

  const finished = data.items.filter((one) => one.finished && one.sweep_id !== null)
  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm'
  const cell = (label: string, value: string, patch: (value: string) => Partial<Draft>, symbol: string) => (
    <input
      aria-label={`${label} of ${symbol}`}
      inputMode="decimal"
      value={value}
      placeholder={label === 'spread' ? 'measured' : '0'}
      onChange={(event) => {
        edit(symbol, patch(event.target.value))
      }}
      className={`${input} w-20`}
    />
  )

  return (
    <section className="space-y-6">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold text-slate-100">{data.name}</h2>
          <p className="mt-1 text-sm text-slate-400">
            {data.entry_names.map((one) => one ?? '(removed entry)').join(', ')} ·{' '}
            {data.timeframes.join(', ')} · {data.date_from.slice(0, 10)} →{' '}
            {data.date_to.slice(0, 10)}
          </p>
        </div>
        <button
          type="button"
          onClick={() => {
            if (data.paused) queue.resume.mutate(undefined)
            else queue.pause.mutate(undefined)
          }}
          className="rounded border border-slate-700 px-3 py-1.5 text-sm"
        >
          {data.paused ? 'Resume the queue' : 'Pause the queue'}
        </button>
      </header>
      {data.paused && (
        <p className="text-sm text-amber-300">
          Paused: the market running goes on to its end, and nothing new starts.
        </p>
      )}

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">Queue</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              <span className="sr-only">Read together</span>
            </th>
            <th scope="col" className="px-3 py-2">
              Market
            </th>
            <th scope="col" className="px-3 py-2">
              Costs
            </th>
            <th scope="col" className="px-3 py-2">
              State
            </th>
            <th scope="col" className="px-3 py-2">
              Runs
            </th>
            <th scope="col" className="px-3 py-2">
              <span className="sr-only">Remove</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {data.items.length === 0 ? (
            <tr>
              <td colSpan={6} className="px-3 py-2 text-slate-500">
                Nothing queued yet — choose markets below.
              </td>
            </tr>
          ) : (
            data.items.map((item) => (
              <tr key={item.id} className="border-b border-slate-900 align-top">
                <td className="px-3 py-2">
                  {item.finished && item.sweep_id !== null && (
                    <input
                      type="checkbox"
                      aria-label={`read ${item.symbol} together`}
                      checked={chosen.includes(item.sweep_id)}
                      onChange={() => {
                        const sweepId = item.sweep_id ?? ''
                        setChosen((current) =>
                          current.includes(sweepId)
                            ? current.filter((one) => one !== sweepId)
                            : [...current, sweepId],
                        )
                      }}
                    />
                  )}
                </td>
                <td className="px-3 py-2">
                  {item.sweep_id === null ? (
                    item.symbol
                  ) : (
                    <Link to={`/sweeps/${item.sweep_id}`} className="text-sky-400 hover:text-sky-300">
                      {item.symbol}
                    </Link>
                  )}
                </td>
                <td className="px-3 py-2 text-xs text-slate-400">
                  spread {item.cost_model.spread_points ?? '—'}
                  {item.cost_model.commission_per_unit !== undefined &&
                    item.cost_model.commission_per_unit !== '0' &&
                    ` · commission ${item.cost_model.commission_per_unit}`}
                  {item.cost_model.swap_long_per_lot !== undefined &&
                    ` · swap ${item.cost_model.swap_long_per_lot} / ${item.cost_model.swap_short_per_lot ?? '0'}`}
                </td>
                <td className={`px-3 py-2 ${item.status === 'failed' ? 'text-amber-300' : ''}`}>
                  {stage(item)}
                  {item.error !== null && (
                    <span className="block text-xs text-slate-500">{item.error}</span>
                  )}
                </td>
                <td className="px-3 py-2 text-slate-400">
                  {item.runs === 0
                    ? '—'
                    : `${String(item.done)} of ${String(item.runs)}${item.failed > 0 ? ` · ${String(item.failed)} failed` : ''}`}
                </td>
                <td className="px-3 py-2">
                  {item.status === 'waiting' && (
                    <button
                      type="button"
                      aria-label={`Remove ${item.symbol} from the queue`}
                      onClick={() => {
                        queue.remove.mutate(item.id)
                      }}
                      className="text-slate-500 hover:text-red-400"
                    >
                      ×
                    </button>
                  )}
                </td>
              </tr>
            ))
          )}
        </tbody>
      </table>

      {finished.length > 1 && (
        <div className="flex flex-wrap items-center gap-3 text-sm">
          <button
            type="button"
            disabled={chosen.length < 2 || combine.isPending}
            onClick={together}
            className="rounded bg-sky-600 px-4 py-2 font-medium disabled:opacity-40"
          >
            Read {String(chosen.length)} together
          </button>
          <button
            type="button"
            onClick={() => {
              setChosen(finished.map((one) => one.sweep_id ?? ''))
            }}
            className="text-sky-400 hover:text-sky-300"
          >
            choose every finished one ({String(finished.length)})
          </button>
          {combine.isError && (
            <span role="alert" className="text-red-400">
              {apiFailure(combine.error, 'Could not read them together.')}
            </span>
          )}
        </div>
      )}

      <div className="space-y-3 rounded border border-slate-800 p-4">
        <h3 className="font-semibold">Add markets to the queue</h3>
        <div className="grid gap-1 text-sm sm:grid-cols-3">
          {(instruments.data ?? []).map((instrument) => (
            <label key={instrument.id} className="flex items-center gap-2">
              <input
                type="checkbox"
                checked={instrument.symbol in drafts}
                onChange={() => {
                  tick(instrument.symbol, instrument.default_spread_points)
                }}
              />
              {instrument.symbol}
            </label>
          ))}
        </div>
        {Object.keys(drafts).length > 0 && (
          <table className="text-sm">
            <thead>
              <tr className="text-xs text-slate-500">
                <th className="px-2 py-1 text-left">Market</th>
                <th className="px-2 py-1 text-left">Spread</th>
                <th className="px-2 py-1 text-left">Commission / lot</th>
                <th className="px-2 py-1 text-left">Swap long / lot</th>
                <th className="px-2 py-1 text-left">Swap short / lot</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(drafts).map(([symbol, draft]) => (
                <tr key={symbol}>
                  <td className="px-2 py-1">{symbol}</td>
                  <td className="px-2 py-1">
                    {cell('spread', draft.spread, (value) => ({ spread: value }), symbol)}
                  </td>
                  <td className="px-2 py-1">
                    {cell('commission', draft.commission, (value) => ({ commission: value }), symbol)}
                  </td>
                  <td className="px-2 py-1">
                    {cell('swap long', draft.swapLong, (value) => ({ swapLong: value }), symbol)}
                  </td>
                  <td className="px-2 py-1">
                    {cell('swap short', draft.swapShort, (value) => ({ swapShort: value }), symbol)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {queue.add.isError && (
          <p role="alert" className="text-sm text-red-400">
            {apiFailure(queue.add.error, 'Could not queue them.')}
          </p>
        )}
        <button
          type="button"
          disabled={Object.keys(drafts).length === 0 || queue.add.isPending}
          onClick={add}
          className="rounded bg-sky-600 px-4 py-2 text-sm font-medium disabled:opacity-40"
        >
          Queue {String(Object.keys(drafts).length)} markets
        </button>
        <p className="text-xs text-slate-500">
          They run one after the other, in the order queued, each as a sweep of its own.
        </p>
      </div>
    </section>
  )
}
