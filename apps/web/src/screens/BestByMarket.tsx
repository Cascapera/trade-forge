import { useMemo, useState } from 'react'

import { useBestMap } from '../api/hooks'
import type { BestMapCell, BestMetric } from '../api/types'
import { fillFor } from '../backtest/study'
import {
  type Column,
  entryNameOf,
  fillOf,
  formatValue,
  layoutOf,
  type MapFilters,
  METRICS,
  NO_FILTERS,
} from '../best/map'
import { BestCellPanel, type ChosenCell } from '../components/BestCellPanel'
import { Pager } from '../components/Pager'
import { usePaged } from '../components/paging'
import { count } from '../format'

/** Symbol rows of the map on one page; a folded market is one row. */
const MAP_ROWS_PER_PAGE = 30

/** A page of rows back into its markets, in order, each with the symbols it shows on this page. */
function groupByMarket(
  rows: readonly { market: string; symbol: string | null }[],
): { market: string; symbols: string[] }[] {
  const groups: { market: string; symbols: string[] }[] = []
  for (const row of rows) {
    const last = groups.at(-1)
    const group = last?.market === row.market ? last : { market: row.market, symbols: [] }
    if (group !== last) groups.push(group)
    if (row.symbol !== null) group.symbols.push(row.symbol)
  }
  return groups
}

/**
 * The best runs by market, setup and chart, across every sweep (his ask, 02/10).
 *
 * One cell per market × setup × chart, coloured by the chosen measure; a click opens that cell's
 * top ten, without clones or near-clones. A map to choose ideas to validate — never a validation:
 * every cell is the best of `ranked` draws over one window, and the page says so.
 *
 * Built as a table, like the study heatmap: a heatmap is a table of numbers with a colour on each,
 * so a screen reader gets rows, columns and headers, and every cell carries its number.
 */
export function BestByMarket(): React.JSX.Element {
  const [metric, setMetric] = useState<BestMetric>('recovery_r')
  const [everyRun, setEveryRun] = useState(false)
  const [filters, setFilters] = useState<MapFilters>(NO_FILTERS)
  const [folded, setFolded] = useState<string[]>([])
  const [chosen, setChosen] = useState<ChosenCell | undefined>(undefined)
  const map = useBestMap(metric, everyRun)

  const cells = useMemo(() => map.data?.cells ?? [], [map.data])
  const layout = useMemo(() => layoutOf(cells, metric, filters), [cells, metric, filters])
  const options = useMemo(() => optionsOf(cells), [cells])
  // ⚠️ Paged by row, a market folded counting as one (04/10): with hundreds of symbols the map was
  // one table as tall as the catalogue. A page that starts inside a market repeats its heading.
  const rows = useMemo(
    () =>
      layout.markets.flatMap(({ market, symbols }) =>
        folded.includes(market)
          ? [{ market, symbol: null }]
          : symbols.map((symbol): { market: string; symbol: string | null } => ({ market, symbol })),
      ),
    [layout, folded],
  )
  const { page, pager } = usePaged(rows, MAP_ROWS_PER_PAGE)
  const groups = groupByMarket(page)

  return (
    <div className="space-y-6">
      <header className="space-y-1">
        <h1 className="text-2xl font-semibold text-slate-100">Best by market</h1>
        <p className="text-sm text-slate-400">
          Each setup&apos;s best run on each market and chart, across every sweep on engine{' '}
          {map.data?.engine_version ?? '…'}. In sample and not validated: a cell is the best of the
          runs it ranked, and part of being best is the luck of the search.
        </p>
      </header>

      <div className="flex flex-wrap items-end gap-4 text-sm">
        <label className="flex flex-col gap-1 text-slate-300">
          Rank by
          <select
            value={metric}
            onChange={(event) => {
              setMetric(event.target.value as BestMetric)
            }}
            className="rounded border border-slate-700 bg-slate-900 px-2 py-1 text-slate-100"
          >
            {METRICS.map((one) => (
              <option key={one.key} value={one.key}>
                {one.label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 text-slate-300">
          <input
            type="checkbox"
            checked={everyRun}
            onChange={(event) => {
              setEveryRun(event.target.checked)
            }}
          />
          Include runs under their chart&apos;s trade floor
        </label>
      </div>

      <Chips
        title="Markets"
        options={options.markets.map((market) => ({
          key: market,
          label: market,
        }))}
        chosen={filters.markets}
        onChange={(markets) => {
          setFilters({ ...filters, markets })
        }}
      />
      <Chips
        title="Setups"
        options={options.entries}
        chosen={filters.entries}
        onChange={(entries) => {
          setFilters({ ...filters, entries })
        }}
      />
      <Chips
        title="Charts"
        options={options.timeframes.map((timeframe) => ({
          key: timeframe,
          label: timeframe,
        }))}
        chosen={filters.timeframes}
        onChange={(timeframes) => {
          setFilters({ ...filters, timeframes })
        }}
      />

      {map.isPending && <p className="text-sm text-slate-400">Loading…</p>}
      {map.isError && (
        <p role="alert" className="text-sm text-amber-300">
          Could not read the map: {map.error.message}
        </p>
      )}
      {map.data !== undefined && layout.columns.length === 0 && (
        <p className="text-sm text-slate-400">
          No run can be ranked yet
          {everyRun ? '' : ' over its chart’s trade floor'}: finished runs of ordinary sweeps on
          this engine appear here as they land.
        </p>
      )}

      {layout.columns.length > 0 && (
        <div className="space-y-3">
          <div className="overflow-x-auto">
            <table className="border-separate border-spacing-0.5 text-xs">
              <caption className="caption-top pb-2 text-left text-xs text-slate-400">
                {METRICS.find((one) => one.key === metric)?.label} of each cell&apos;s best run.
                Click a cell for its top ten.
              </caption>
              <thead>
                <tr>
                  <th
                    scope="col"
                    className="sticky left-0 bg-slate-950 px-2 text-left font-normal text-slate-400"
                  >
                    Market
                  </th>
                  {layout.columns.map((column) => (
                    <th
                      key={column.key}
                      scope="col"
                      className="max-w-28 px-1 text-center align-bottom font-normal text-slate-400"
                    >
                      <span className="block truncate" title={column.entryName}>
                        {column.entryName}
                      </span>
                      <span className="text-slate-500">{column.timeframe}</span>
                    </th>
                  ))}
                </tr>
              </thead>
              {groups.map(({ market, symbols }) => {
                const isFolded = folded.includes(market)
                const all = layout.markets.find((one) => one.market === market)?.symbols ?? []
                return (
                  <tbody key={market}>
                    <tr>
                      <th
                        scope="rowgroup"
                        colSpan={layout.columns.length + 1}
                        className="sticky left-0 bg-slate-950 pt-3 text-left"
                      >
                        <button
                          type="button"
                          aria-expanded={!isFolded}
                          onClick={() => {
                            setFolded(
                              isFolded
                                ? folded.filter((one) => one !== market)
                                : [...folded, market],
                            )
                          }}
                          className="text-sm font-medium text-slate-200 hover:text-slate-50"
                        >
                          {isFolded ? '▸' : '▾'} {market}{' '}
                          <span className="font-normal text-slate-500">
                            ({all.length} market
                            {all.length === 1 ? '' : 's'})
                          </span>
                        </button>
                      </th>
                    </tr>
                    {!isFolded &&
                      symbols.map((symbol) => (
                        <tr key={symbol}>
                          <th
                            scope="row"
                            className="sticky left-0 bg-slate-950 px-2 text-left font-normal text-slate-300"
                          >
                            {symbol}
                          </th>
                          {layout.columns.map((column) => (
                            <Cell
                              key={column.key}
                              cell={layout.at(symbol, column)}
                              column={column}
                              metric={metric}
                              extent={layout.extent}
                              selected={
                                chosen?.symbol === symbol &&
                                chosen.entryId === column.entryId &&
                                chosen.timeframe === column.timeframe
                              }
                              onChoose={() => {
                                setChosen({
                                  symbol,
                                  entryId: column.entryId,
                                  entryName: column.entryName,
                                  timeframe: column.timeframe,
                                })
                              }}
                            />
                          ))}
                        </tr>
                      ))}
                  </tbody>
                )
              })}
            </table>
          </div>
          <Pager label="Map pages" {...pager} />
          <Legend metric={metric} extent={layout.extent} />
        </div>
      )}

      {chosen !== undefined && (
        <BestCellPanel
          cell={chosen}
          metric={metric}
          everyRun={everyRun}
          onClose={() => {
            setChosen(undefined)
          }}
        />
      )}
    </div>
  )
}

function Cell({
  cell,
  column,
  metric,
  extent,
  selected,
  onChoose,
}: {
  cell: BestMapCell | undefined
  column: Column
  metric: BestMetric
  extent: number
  selected: boolean
  onChoose: () => void
}): React.JSX.Element {
  if (cell === undefined) {
    return <td className="min-w-16 rounded px-1 py-1.5 text-center text-slate-600">·</td>
  }
  const value = formatValue(cell.value, metric, cell.unbounded)
  return (
    <td className="p-0">
      <button
        type="button"
        onClick={onChoose}
        aria-pressed={selected}
        aria-label={`${cell.symbol}, ${column.entryName} ${column.timeframe}: ${value}, best of ${String(cell.ranked)} runs`}
        title={`${column.entryName} ${column.timeframe} on ${cell.symbol}: ${value} — best of ${count(cell.ranked)} runs`}
        className={`block w-full min-w-16 rounded px-1 py-1.5 text-center tabular-nums text-slate-100 hover:ring-2 hover:ring-slate-300 ${selected ? 'ring-2 ring-sky-400' : ''}`}
        style={{ backgroundColor: fillOf(cell, metric, extent) }}
      >
        {value}
      </button>
    </td>
  )
}

/** The colours in the map's own numbers: one scale, both arms against the same extent. */
function Legend({ metric, extent }: { metric: BestMetric; extent: number }): React.JSX.Element {
  const neutral = metric === 'positive_years' ? 0.5 : 0
  const low = String(neutral - extent)
  const high = String(neutral + extent)
  return (
    <div className="flex items-center gap-2 text-xs text-slate-400">
      <span>{extent === 0 ? '—' : formatValue(low, metric)}</span>
      <span className="flex h-3 w-40 overflow-hidden rounded" aria-hidden>
        {[-1, -0.6, -0.3, 0, 0.3, 0.6, 1].map((stop) => (
          <span
            key={stop}
            className="flex-1"
            style={{
              backgroundColor: fillFor(stop * (extent || 1), extent || 1),
            }}
          />
        ))}
      </span>
      <span>{extent === 0 ? '—' : formatValue(high, metric)}</span>
      <span className="sr-only">
        Blue is above the neutral point and red below it; every cell also carries its number.
      </span>
    </div>
  )
}

/** A row of toggles; none chosen means every one is shown. */
function Chips({
  title,
  options,
  chosen,
  onChange,
}: {
  title: string
  options: { key: string; label: string }[]
  chosen: string[]
  onChange: (chosen: string[]) => void
}): React.JSX.Element | null {
  if (options.length === 0) return null
  return (
    <fieldset className="flex flex-wrap items-center gap-2 text-xs">
      <legend className="sr-only">{title}</legend>
      <span className="w-16 text-slate-400">{title}</span>
      {options.map((option) => {
        const on = chosen.includes(option.key)
        return (
          <button
            key={option.key}
            type="button"
            aria-pressed={on}
            onClick={() => {
              onChange(on ? chosen.filter((one) => one !== option.key) : [...chosen, option.key])
            }}
            className={`rounded-full border px-2 py-0.5 ${on ? 'border-sky-400 bg-sky-950 text-sky-200' : 'border-slate-700 text-slate-300 hover:border-slate-500'}`}
          >
            {option.label}
          </button>
        )
      })}
      {chosen.length > 0 && (
        <button
          type="button"
          onClick={() => {
            onChange([])
          }}
          className="text-slate-500 hover:text-slate-300"
        >
          all
        </button>
      )}
    </fieldset>
  )
}

/** The markets, setups and charts the map holds — what the filters offer. */
function optionsOf(cells: readonly BestMapCell[]): {
  markets: string[]
  entries: { key: string; label: string }[]
  timeframes: string[]
} {
  const charts = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']
  const entries = new Map<string, string>()
  for (const cell of cells) entries.set(cell.entry_id, entryNameOf(cell))
  return {
    markets: [...new Set(cells.map((cell) => cell.market))].sort((a, b) => a.localeCompare(b)),
    entries: [...entries.entries()]
      .map(([key, label]) => ({ key, label }))
      .sort((a, b) => a.label.localeCompare(b.label)),
    timeframes: [...new Set(cells.map((cell) => cell.timeframe))].sort(
      (a, b) => charts.indexOf(a) - charts.indexOf(b),
    ),
  }
}
