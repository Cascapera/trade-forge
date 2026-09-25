import { useState } from 'react'
import { Link } from 'react-router-dom'

import { apiFailure } from '../api/failure'
import { useCreateSlicing, useSlicings } from '../api/hooks'
import type { SliceMode, SliceOut, SlicedPoint, SlicingOut } from '../api/types'
import { percent } from '../format'

const CHART_ORDER = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']

/** R with its sign, one decimal: `+3.2 R`. */
function inR(value: string): string {
  const number = Number(value)
  return `${number > 0 ? '+' : ''}${number.toFixed(1)} R`
}

function tone(value: string): string {
  const number = Number(value)
  if (number > 0) return 'text-emerald-400'
  if (number < 0) return 'text-red-400'
  return 'text-slate-300'
}

function Piece(props: { slice: SliceOut }): React.JSX.Element {
  const { slice } = props
  // ⚠️ An uncounted piece — a year with no trade, the short last block — is shown dimmed, never
  // hidden: the reader must see the gap, and must also see it did not enter the share.
  return (
    <span
      title={`${slice.date_from.slice(0, 10)} → ${slice.date_to.slice(0, 10)} · ${String(slice.trades)} trades${slice.counted ? '' : ' · not counted'}`}
      className={`rounded border px-1.5 py-0.5 font-mono text-[11px] ${
        slice.counted
          ? `border-slate-700 ${tone(slice.net_r)}`
          : 'border-dashed border-slate-800 text-slate-600'
      }`}
    >
      {slice.label} {inR(slice.net_r)}
    </span>
  )
}

function Verdict(props: { point: SlicedPoint }): React.JSX.Element {
  const { point } = props
  if (!point.trades_kept) {
    return (
      <span className="text-slate-500" title="tested before 25/09 and lost, so no trades were kept">
        no trades kept
      </span>
    )
  }
  return (
    <span className={point.passed ? 'text-emerald-400' : 'text-slate-400'}>
      {point.passed ? 'passed' : 'failed'} · {String(point.positive)}/{String(point.counted)}
    </span>
  )
}

function sortPoints(points: SlicedPoint[]): SlicedPoint[] {
  return [...points].sort(
    (left, right) =>
      (left.entry_name ?? '').localeCompare(right.entry_name ?? '') ||
      CHART_ORDER.indexOf(left.timeframe) - CHART_ORDER.indexOf(right.timeframe) ||
      left.symbol.localeCompare(right.symbol) ||
      left.label.localeCompare(right.label),
  )
}

function describe(slicing: SlicingOut): string {
  const cut =
    slicing.mode === 'calendar'
      ? 'by calendar year'
      : `in blocks of ${String(slicing.block_trades)} trades`
  return `Cut ${cut} · passes at ${percent(slicing.pass_share, 0)} of pieces positive · ${slicing.created_at.slice(0, 16).replace('T', ' ')}`
}

function Slicing(props: { slicing: SlicingOut }): React.JSX.Element {
  const { slicing } = props
  const passed = slicing.points.filter((point) => point.passed)
  const groups = [...slicing.groups].sort(
    (left, right) =>
      (left.entry_name ?? '').localeCompare(right.entry_name ?? '') ||
      CHART_ORDER.indexOf(left.timeframe) - CHART_ORDER.indexOf(right.timeframe),
  )
  return (
    <article aria-label={describe(slicing)} className="space-y-3 rounded border border-slate-800 p-4">
      <p className="text-sm text-slate-300">{describe(slicing)}</p>
      {passed.length > 0 && (
        <p className="text-sm">
          <Link
            to="/clusters"
            state={{
              name: `The ${String(passed.length)} that passed — ${describe(slicing)}`,
              members: passed.map((point) => ({
                backtest_id: point.run_id,
                label: `${point.symbol} ${point.timeframe} · ${point.label}`,
              })),
            }}
            className="text-sky-400 hover:text-sky-300"
          >
            Replay the {String(passed.length)} that passed on one account (cluster)
          </Link>
        </p>
      )}

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">Passed, by entry and chart</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Entry
            </th>
            <th scope="col" className="px-3 py-2">
              Chart
            </th>
            <th scope="col" className="px-3 py-2">
              Passed
            </th>
          </tr>
        </thead>
        <tbody>
          {groups.map((group) => (
            <tr key={`${group.entry_id}-${group.timeframe}`} className="border-b border-slate-900">
              <td className="px-3 py-2">{group.entry_name ?? '(removed entry)'}</td>
              <td className="px-3 py-2">{group.timeframe}</td>
              <td className={`px-3 py-2 ${group.passed > 0 ? 'text-emerald-400' : 'text-slate-400'}`}>
                {String(group.passed)} of {String(group.judged)} judged
                {group.judged < group.points && (
                  <span className="text-slate-500"> ({String(group.points)} tested)</span>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <table className="w-full border-collapse text-left text-sm">
        <caption className="mb-2 text-left font-semibold">Point by point</caption>
        <thead>
          <tr className="border-b border-slate-800 text-xs tracking-wide text-slate-500 uppercase">
            <th scope="col" className="px-3 py-2">
              Entry
            </th>
            <th scope="col" className="px-3 py-2">
              Market
            </th>
            <th scope="col" className="px-3 py-2">
              Point
            </th>
            <th scope="col" className="px-3 py-2">
              Total
            </th>
            <th scope="col" className="px-3 py-2">
              Verdict
            </th>
            <th scope="col" className="px-3 py-2">
              Pieces
            </th>
          </tr>
        </thead>
        <tbody>
          {sortPoints(slicing.points).map((point) => (
            <tr key={point.run_id} className="border-b border-slate-900 align-top">
              <td className="px-3 py-2">{point.entry_name ?? '(removed entry)'}</td>
              <td className="px-3 py-2">
                {point.symbol} {point.timeframe}
              </td>
              <td className="px-3 py-2 text-slate-300">{point.label}</td>
              <td className={`px-3 py-2 ${tone(point.net_r)}`}>{inR(point.net_r)}</td>
              <td className="px-3 py-2">
                <Verdict point={point} />
              </td>
              <td className="px-3 py-2">
                <div className="flex flex-wrap gap-1">
                  {point.slices.map((slice) => (
                    <Piece key={slice.label} slice={slice} />
                  ))}
                </div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </article>
  )
}

/**
 * Judge a finished reserved-window test in pieces — by calendar year or by blocks of trades —
 * and every judgement kept (25/09).
 *
 * ⚠️ **Started by hand, and only once the test has finished.** A bad sweep needs no second
 * analysis, and a test still running would be judged on the runs that happened to land first.
 *
 * ⚠️ **The bar is set before the result is seen, and stored with it.** Two ways to cut is two
 * chances to keep whichever looks better; the kept rule is the record of which was decided first.
 *
 * Summed in R, not money: the run compounds, and money would make late pieces look better for no
 * reason but their place in the run.
 */
export function HoldoutSlicings(props: { sweepId: string; settled: boolean }): React.JSX.Element {
  const { sweepId, settled } = props
  const slicings = useSlicings(sweepId)
  const create = useCreateSlicing(sweepId)
  const [mode, setMode] = useState<SliceMode>('calendar')
  const [blockTrades, setBlockTrades] = useState(30)
  const [passPercent, setPassPercent] = useState(70)

  const blockOk = Number.isInteger(blockTrades) && blockTrades >= 5
  const barOk = passPercent > 0 && passPercent <= 100
  const why = !settled
    ? 'The test is still running — judge it once every point has landed.'
    : mode === 'trades' && !blockOk
      ? 'A block holds at least 5 trades.'
      : !barOk
        ? 'The bar is a share between 1% and 100%.'
        : null

  const judge = (): void => {
    create.mutate({
      mode,
      ...(mode === 'trades' ? { block_trades: blockTrades } : {}),
      pass_share: String(passPercent / 100),
    })
  }

  const input = 'rounded border border-slate-700 bg-slate-900 px-2 py-1'
  return (
    <section aria-label="judged in pieces" className="space-y-4">
      <div className="space-y-3 rounded border border-slate-800 p-4">
        <div>
          <h3 className="font-semibold">Judge in pieces</h3>
          <p className="text-sm text-slate-400">
            Cut each point&apos;s run on the reserved window by calendar year or into blocks of the
            same number of trades, and count the pieces that made money, in R. Runs nothing — the
            trades are already kept — and every judgement stays below.
          </p>
        </div>
        <div className="flex flex-wrap items-end gap-4 text-sm">
          <fieldset className="flex gap-3">
            <legend className="sr-only">How to cut</legend>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name={`slice-mode-${sweepId}`}
                checked={mode === 'calendar'}
                onChange={() => {
                  setMode('calendar')
                }}
              />
              By calendar year
            </label>
            <label className="flex items-center gap-1">
              <input
                type="radio"
                name={`slice-mode-${sweepId}`}
                checked={mode === 'trades'}
                onChange={() => {
                  setMode('trades')
                }}
              />
              By blocks of trades
            </label>
          </fieldset>
          {mode === 'trades' && (
            <label className="flex flex-col gap-1">
              Trades per block
              <input
                type="number"
                min={5}
                value={blockTrades}
                onChange={(event) => {
                  setBlockTrades(Number(event.target.value))
                }}
                className={`${input} w-24`}
              />
            </label>
          )}
          <label className="flex flex-col gap-1">
            Passes at (% of pieces positive)
            <input
              type="number"
              min={1}
              max={100}
              value={passPercent}
              onChange={(event) => {
                setPassPercent(Number(event.target.value))
              }}
              className={`${input} w-24`}
            />
          </label>
          <button
            type="button"
            disabled={why !== null || create.isPending}
            onClick={judge}
            className="rounded bg-sky-700 px-3 py-1.5 text-sm font-medium text-white hover:bg-sky-600 disabled:opacity-40"
          >
            Judge
          </button>
        </div>
        {why !== null && <p className="text-xs text-amber-300">{why}</p>}
        {create.isError && <p className="text-sm text-red-400">{apiFailure(create.error, 'Could not judge the test.')}</p>}
      </div>

      {slicings.isError ? (
        <p className="text-red-400">Could not load the judgements.</p>
      ) : slicings.data === undefined ? null : slicings.data.length === 0 ? (
        <p className="text-sm text-slate-500">Not judged in pieces yet.</p>
      ) : (
        slicings.data.map((slicing) => <Slicing key={slicing.id} slicing={slicing} />)
      )}
    </section>
  )
}
