import { useState } from 'react'

import { ApiError } from '../api/client'
import { useCollections, useCreateCollection, useSymbolHistory } from '../api/hooks'
import type { AssetClass, Collection } from '../api/types'
import { SymbolCombobox } from '../components/SymbolCombobox'
import { SymbolHistoryNote } from '../components/SymbolHistoryNote'
import { asDateInput, asInstant, suggestedWindow } from '../collect/window'

const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'] as const

const ASSET_CLASSES: readonly AssetClass[] = ['forex', 'stock', 'index', 'future', 'crypto']

const fieldClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Fetch a symbol's history without touching the CLI.
 *
 * ## The three things this screen has to say and a plain form would not
 *
 * * **What window to open on.** Not "the last five years" — a *budget of bars*, floored by what
 *   the probe found (`collect/window.ts`). More history is not more validation, and this is the
 *   only place the two get reconciled before somebody presses a button.
 * * **Who decides the asset class.** For 24 of this broker's 84 symbols the tree path names no
 *   class the system has, and `instruments.asset_class` cannot hold "unknown". The API answers
 *   409 and this screen turns that into a field, which is the difference between a question and
 *   a wall.
 * * **Where the work is.** The row advances a year at a time on a machine this browser cannot
 *   see, and a cold year takes minutes. "3 of 5 years" is what makes the wait legible.
 */
export function CollectSymbol(): React.JSX.Element {
  const [symbol, setSymbol] = useState('')
  const [timeframe, setTimeframe] = useState<string>('H1')
  const [assetClass, setAssetClass] = useState<AssetClass | ''>('')
  const [touched, setTouched] = useState(false)
  const [window, setWindow] = useState({ from: '', to: '' })

  const history = useSymbolHistory(symbol, timeframe)
  const create = useCreateCollection()
  const collections = useCollections()

  // ⚠️ The suggestion is recomputed on every render and only *adopted* while the operator has
  // not touched the dates. Writing it into state on every change would fight a person mid-edit:
  // they widen the start, the probe result arrives, and the field snaps back under the cursor.
  const suggested = suggestedWindow(timeframe, history.data, new Date())
  const from = touched ? window.from : asDateInput(suggested.from)
  const to = touched ? window.to : asDateInput(suggested.to)

  // ⚠️ Kept as the error object rather than a boolean, so the message below can be narrowed to
  // it. And the message read is `detail`, not `message`: `ApiError.message` is only the status
  // code — the sentence explaining what went wrong lives in `detail`, which this project has
  // already shipped an empty warning box over once.
  const conflict =
    create.error instanceof ApiError && create.error.status === CONFLICT ? create.error : null
  const needsClass = conflict !== null && assetClass === ''

  const submit = (): void => {
    create.mutate({
      symbol,
      timeframe,
      date_from: asInstant(from),
      // ⚠️ End of day, because the window is inclusive on both ends. Midnight would drop every
      // bar of the final day — silently, and only on the day somebody chose as the end.
      date_to: asInstant(to, true),
      ...(assetClass === '' ? {} : { asset_class: assetClass }),
    })
  }

  const edit = (patch: { from?: string; to?: string }): void => {
    setTouched(true)
    setWindow({ from, to, ...patch })
  }

  return (
    <div className="space-y-6">
      <div>
        <h2 className="text-base font-semibold">Collect history</h2>
        <p className="mt-1 text-xs text-slate-400">
          Fetch a symbol from the broker so it can be backtested. The work runs on the machine
          beside the terminal, one calendar year at a time.
        </p>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-4">
        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Symbol
          <SymbolCombobox
            value={symbol}
            onChange={(next) => {
              setSymbol(next)
              // A different symbol is a different question: whatever class was answered for the
              // last one says nothing about this one, and carrying it over would file a metal
              // as whatever the currency pair before it was.
              setAssetClass('')
              create.reset()
            }}
          />
        </label>

        <label className="flex flex-col gap-1 text-xs text-slate-400">
          Timeframe
          <select
            className={fieldClass}
            value={timeframe}
            onChange={(event) => {
              setTimeframe(event.target.value)
              // ⚠️ The suggested window is per timeframe — a year on M1, seventeen on H1 — so a
              // change here has to be allowed to move the dates again.
              setTouched(false)
            }}
          >
            {TIMEFRAMES.map((option) => (
              <option key={option} value={option}>
                {option}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-xs text-slate-400">
          From
          <input
            type="date"
            className={fieldClass}
            value={from}
            onChange={(event) => {
              edit({ from: event.target.value })
            }}
          />
        </label>

        <label className="flex flex-col gap-1 text-xs text-slate-400">
          To
          <input
            type="date"
            className={fieldClass}
            value={to}
            onChange={(event) => {
              edit({ to: event.target.value })
            }}
          />
        </label>

        <SymbolHistoryNote symbol={symbol} timeframe={timeframe} />

        {!touched && symbol !== '' && (
          <p className="col-span-full text-xs text-slate-400">
            Suggested: {suggested.bars.toLocaleString()} bars,{' '}
            {suggested.bound === 'probe'
              ? 'starting where the measurement says the series stops being filler and typed costs'
              : 'as much as one run should carry — widen it if you want more'}
          </p>
        )}
      </div>

      {needsClass && (
        <div className="space-y-2 rounded border border-amber-700 bg-amber-950/30 p-3 text-xs">
          <p className="text-amber-300">⚠️ {reason(conflict)}</p>
          <p className="text-slate-400">
            The broker files this one where nothing says what it is. Pick the arithmetic it should
            be priced with — nothing in the engine branches on this, but the catalogue cannot hold
            &quot;unknown&quot;.
          </p>
          <label className="flex items-center gap-2 text-slate-400">
            Asset class
            <select
              className={fieldClass}
              value={assetClass}
              onChange={(event) => {
                setAssetClass(event.target.value as AssetClass)
              }}
            >
              <option value="">choose…</option>
              {ASSET_CLASSES.map((option) => (
                <option key={option} value={option}>
                  {option}
                </option>
              ))}
            </select>
          </label>
        </div>
      )}

      {create.error !== null && conflict === null && (
        <p className="text-xs text-amber-300">
          {/* Everything that is not the classification question: a 422 from a window the schema
              refused, a 500, a network failure. `detail` when the API sent one, the message
              otherwise — a bare "API error 422" tells nobody which field. */}
          ⚠️ {reason(create.error)}
        </p>
      )}

      <button
        type="button"
        className="rounded bg-sky-600 px-3 py-1.5 text-sm font-semibold text-white disabled:bg-slate-700 disabled:text-slate-400"
        disabled={symbol === '' || from === '' || to === '' || create.isPending}
        onClick={submit}
      >
        {create.isPending ? 'Requesting…' : 'Collect'}
      </button>

      <CollectionList rows={collections.data ?? []} />
    </div>
  )
}

const CONFLICT = 409

/**
 * What to put on screen when a request was refused.
 *
 * ⚠️ `ApiError.message` is only `API error 409` — the sentence a person can act on lives in
 * `detail`, and this project has already shipped a warning box that said nothing for exactly
 * that reason. Guarded on `typeof detail === 'string'` because a 422 sends a *list* of field
 * errors, and stringifying that yields `[object Object]`.
 */
function reason(error: unknown): string {
  if (error instanceof ApiError && typeof error.detail === 'string') return error.detail
  if (error instanceof ApiError) return `The request was refused (${String(error.status)}).`
  return error instanceof Error ? error.message : 'Something went wrong.'
}

function CollectionList(props: { rows: Collection[] }): React.JSX.Element | null {
  if (props.rows.length === 0) return null

  return (
    <table className="w-full text-left text-xs">
      <caption className="pb-2 text-left text-xs text-slate-400">Recent collections</caption>
      <thead className="text-slate-400">
        <tr>
          <th className="py-1 font-normal">Symbol</th>
          <th className="py-1 font-normal">Timeframe</th>
          <th className="py-1 font-normal">Requested</th>
          <th className="py-1 font-normal">State</th>
          <th className="py-1 font-normal">Result</th>
        </tr>
      </thead>
      <tbody>
        {props.rows.map((row) => (
          <tr key={row.id} className="border-t border-slate-800">
            <td className="py-1 text-slate-200">{row.symbol}</td>
            <td className="py-1 text-slate-300">{row.timeframe}</td>
            <td className="py-1 text-slate-400">
              {row.date_from.slice(0, 10)} → {row.date_to.slice(0, 10)}
            </td>
            <td className="py-1 text-slate-300">{state(row)}</td>
            <td className="py-1 text-slate-400">{result(row)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

function state(row: Collection): string {
  if (row.status === 'running') {
    // ⚠️ Years, not a percentage. The work advances one calendar year at a time and a cold year
    // can take minutes on this broker — "3 of 5 years" is a sentence somebody can act on, and
    // 60% is a number they can only watch.
    return `${String(row.years_done)} of ${String(row.years_total)} years`
  }
  return row.status
}

function result(row: Collection): string {
  // ⚠️ The error first: a failed row can carry counts from a prior attempt, and reading the
  // count of a failure as a result is how somebody concludes they have data they do not have.
  if (row.error !== null) return row.error
  // ⚠️ `null` is not `0`. Nothing collected *yet* and nothing there at all are different
  // sentences, and only one of them means the request is finished.
  if (row.candles === null) return '—'
  return `${row.candles.toLocaleString()} bars · ${String(row.gaps ?? 0)} gaps`
}
