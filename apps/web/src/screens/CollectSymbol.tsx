import { useState } from 'react'

import { ApiError } from '../api/client'
import {
  useAutoProbe,
  useCollections,
  useCreateCollection,
  useSymbolHistories,
} from '../api/hooks'
import type { AssetClass, BrokerSymbol, Collection } from '../api/types'
import { SymbolMultiCombobox } from '../components/SymbolMultiCombobox'
import {
  asDateInput,
  asInstant,
  bindingFloor,
  estimateSlices,
  MAX_BATCH_SYMBOLS,
  shortWindows,
  suggestedWindow,
} from '../collect/window'

const TIMEFRAMES = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1'] as const

const ASSET_CLASSES: readonly AssetClass[] = ['forex', 'stock', 'index', 'future', 'crypto']

const fieldClass =
  'rounded border border-slate-700 bg-slate-900 px-2 py-1 text-sm text-slate-100 focus:border-sky-500 focus:outline-none'

/**
 * Fetch several symbols' history without touching the CLI.
 *
 * ## The four things this screen has to say and a plain form would not
 *
 * * **What window to open on.** Not "the last five years" — a *budget of bars*, floored by what
 *   the probe found (`collect/window.ts`). More history is not more validation, and this is the
 *   only place the two get reconciled before somebody presses a button.
 * * **Whose floor is binding.** One window covers the batch, so the **latest** floor among the
 *   chosen symbols decides it. Opening on the earliest would buy every other symbol a stretch of
 *   filler bars and typed spread — see `bindingFloor`.
 * * **Who decides the asset class, per symbol.** For 24 of this broker's 84 the tree path names
 *   no class the system has, and `instruments.asset_class` cannot hold "unknown". The API answers
 *   409; this screen asks *before* sending, and asks per symbol — XAUUSD is a future and BTCUSD
 *   is crypto, and one answer for the batch would have to be wrong about one of them.
 * * **How much work this is.** Each symbol walks its own calendar years, so a batch is symbols ×
 *   years of slices, each cold one minutes of a terminal downloading. That number belongs before
 *   the button, not after.
 */
export function CollectSymbol(): React.JSX.Element {
  const [chosen, setChosen] = useState<BrokerSymbol[]>([])
  const [classes, setClasses] = useState<Record<string, AssetClass>>({})
  const [timeframe, setTimeframe] = useState<string>('H1')
  const [touched, setTouched] = useState(false)
  const [window, setWindow] = useState({ from: '', to: '' })

  const symbols = chosen.map((found) => found.symbol)
  const histories = useSymbolHistories(symbols, timeframe)
  // ⚠️ Only the pairs that came back with nothing are measured, and each one only once — see
  // `useAutoProbe`. A probe shares the collection's single-job queue, so re-measuring on every
  // render would put hours of work in front of the first candle.
  const probing = useAutoProbe({ missing: histories.missing, timeframe })
  const create = useCreateCollection()
  const collections = useCollections()

  // ⚠️ The suggestion is recomputed on every render and only *adopted* while the operator has
  // not touched the dates. Writing it into state on every change would fight a person mid-edit:
  // they widen the start, a measurement arrives, and the field snaps back under the cursor.
  const floor = bindingFloor(histories.known.values())
  const suggested = suggestedWindow(timeframe, floor, new Date())
  const from = touched ? window.from : asDateInput(suggested.from)
  const to = touched ? window.to : asDateInput(suggested.to)

  // Symbols the broker's filing cannot classify and nobody has answered for yet. Asked here
  // rather than discovered from a 409, because the person who can answer is the one holding the
  // form — and with twenty symbols the API's refusal would name a list, not a field.
  const unanswered = chosen.filter(
    (found) => found.asset_class_from_path === null && classes[found.symbol] === undefined,
  )

  const slices = estimateSlices(chosen.length, from, to)
  const short = shortWindows(symbols, histories.known, from)
  const blocked = blockedReason({
    chosen: chosen.length,
    unanswered: unanswered.map((found) => found.symbol),
    from,
    to,
  })

  const toggle = (found: BrokerSymbol): void => {
    create.reset()
    setChosen((current) =>
      current.some((each) => each.symbol === found.symbol)
        ? current.filter((each) => each.symbol !== found.symbol)
        : [...current, found],
    )
    // ⚠️ The answer goes with the symbol. Keeping it after a removal would re-apply a class
    // somebody chose for a metal to whatever symbol next occupied that slot.
    setClasses((current) =>
      Object.fromEntries(Object.entries(current).filter(([symbol]) => symbol !== found.symbol)),
    )
  }

  const submit = (): void => {
    create.mutate({
      items: chosen.map((found) => {
        const answered = classes[found.symbol]
        return { symbol: found.symbol, ...(answered === undefined ? {} : { asset_class: answered }) }
      }),
      timeframe,
      date_from: asInstant(from),
      // ⚠️ End of day, because the window is inclusive on both ends. Midnight would drop every
      // bar of the final day — silently, and only on the day somebody chose as the end.
      date_to: asInstant(to, true),
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
          Fetch symbols from the broker so they can be backtested. The work runs on the machine
          beside the terminal, one symbol and one calendar year at a time.
        </p>
      </div>

      <SymbolMultiCombobox chosen={chosen} onToggle={toggle} max={MAX_BATCH_SYMBOLS} />

      <div className="grid grid-cols-2 gap-4 md:grid-cols-3">
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

        {!touched && chosen.length > 0 && (
          <p className="col-span-full text-xs text-slate-400">
            Suggested: {suggested.bars.toLocaleString()} bars per symbol,{' '}
            {suggested.bound === 'probe' && floor !== undefined
              ? `starting where ${floor.symbol}'s measurement says the series stops being filler and typed costs — the latest floor among the chosen symbols, so it is the one that binds`
              : 'as much as one run should carry — widen it if you want more'}
          </p>
        )}

        {chosen.length > 0 && (
          <p className="col-span-full text-xs text-slate-400">
            {/* ⚠️ Slices, not a duration. The screen cannot know how long a cold year takes on
                this broker — it varies by symbol and by how much the terminal already holds —
                but it knows exactly how many of them are being asked for. */}
            That is <strong className="text-slate-200">{slices.toLocaleString()}</strong> year
            {slices === 1 ? '' : 's'} of history to fetch, one after another.
          </p>
        )}
      </div>

      {probing.queued > 0 && (
        <p className="text-xs text-slate-400">
          {/* ⚠️ The wait made legible. Measuring shares the collection's single-job queue and a
              cold H4 took 207 seconds on this broker, so these run *before* the first candle.
              Saying how many turns a silent minute into a queue somebody can reason about —
              the same argument as "3 of 5 years" on a running row. */}
          Measuring{' '}
          <strong className="text-slate-200">
            {probing.queued} symbol{probing.queued === 1 ? '' : 's'}
          </strong>{' '}
          nobody has measured yet. That runs on the same queue as the collection, so it goes
          first — each pair is measured once and never again.
        </p>
      )}

      {short.length > 0 && (
        <div className="rounded border border-slate-700 bg-slate-900/60 p-3 text-xs">
          {/* Not a warning box: coming back short is an ordinary answer, not a failure. An
              empty year does not fail a collection — but the row will report fewer candles than
              the dates imply, and without this the only explanation available is "a bug". */}
          <p className="text-slate-300">
            These start after the window does, so they will come back shorter:
          </p>
          <ul className="mt-1 space-y-0.5">
            {short.map((each) => (
              <li key={each.symbol} className="text-slate-400">
                <span className="font-mono text-slate-200">{each.symbol}</span> — usable from{' '}
                {each.usableFrom}
              </li>
            ))}
          </ul>
        </div>
      )}

      {unanswered.length > 0 && (
        <div className="space-y-2 rounded border border-amber-700 bg-amber-950/30 p-3 text-xs">
          <p className="text-amber-300">
            ⚠️ The broker files {unanswered.length === 1 ? 'this one' : 'these'} where nothing says
            what {unanswered.length === 1 ? 'it is' : 'they are'}.
          </p>
          <p className="text-slate-400">
            Pick the arithmetic each should be priced with — nothing in the engine branches on
            this, but the catalogue cannot hold &quot;unknown&quot;.
          </p>
          {unanswered.map((found) => (
            <label
              key={found.symbol}
              className="flex flex-wrap items-center gap-2 text-slate-400"
              htmlFor={`class-${found.symbol}`}
            >
              <span className="font-mono text-slate-200">{found.symbol}</span>
              <span className="text-slate-500">{found.path}</span>
              <select
                id={`class-${found.symbol}`}
                // ⚠️ Named explicitly rather than inheriting the label's text. The label reads
                // "XAUUSD CFDs\Metals\XAUUSD", which a screen reader would announce as the name
                // of the control — a path, where the question is what kind of thing it is. With
                // several of these on screen the names also have to differ from one another.
                aria-label={`Asset class for ${found.symbol}`}
                className={fieldClass}
                value={classes[found.symbol] ?? ''}
                onChange={(event) => {
                  setClasses((current) => ({
                    ...current,
                    [found.symbol]: event.target.value as AssetClass,
                  }))
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
          ))}
        </div>
      )}

      {create.error !== null && (
        <p className="text-xs text-amber-300">
          {/* `detail` when the API sent one, the message otherwise — a bare "API error 422"
              tells nobody which field. */}
          ⚠️ {reason(create.error)}
        </p>
      )}

      <div className="flex items-center gap-3">
        <button
          type="button"
          className="rounded bg-sky-600 px-3 py-1.5 text-sm font-semibold text-white disabled:bg-slate-700 disabled:text-slate-400"
          disabled={blocked !== null || create.isPending}
          onClick={submit}
        >
          {create.isPending
            ? 'Requesting…'
            : `Collect ${String(chosen.length)} symbol${chosen.length === 1 ? '' : 's'}`}
        </button>
        {/* ⚠️ The reason beside the button, not instead of it. A disabled control with no
            explanation is a dead end — the reader can see it is off and not why. */}
        {blocked !== null && <span className="text-xs text-slate-400">{blocked}</span>}
      </div>

      <CollectionList rows={collections.data ?? []} />
    </div>
  )
}

/** Why the button is off, or `null` when it is not. */
function blockedReason(args: {
  chosen: number
  unanswered: readonly string[]
  from: string
  to: string
}): string | null {
  if (args.chosen === 0) return 'Choose at least one symbol.'
  if (args.from === '' || args.to === '') return 'Set both ends of the window.'
  if (args.to < args.from) return 'The window runs backwards.'
  if (args.unanswered.length > 0) {
    return `Say what ${args.unanswered.join(', ')} ${args.unanswered.length === 1 ? 'is' : 'are'}.`
  }
  return null
}

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
