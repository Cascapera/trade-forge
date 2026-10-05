import type { BestMapCell, BestMetric } from '../api/types'
import { fillFor } from '../backtest/study'
import { inR, percent, ratio } from '../format'

/** The four measures he asked to choose between (02/10), in the order the selector lists them. */
export const METRICS: readonly { key: BestMetric; label: string }[] = [
  { key: 'recovery_r', label: 'R / drawdown' },
  { key: 'net_r', label: 'Net R (total)' },
  { key: 'net_r_per_year', label: 'Net R per year' },
  { key: 'positive_years', label: 'Positive years' },
  { key: 'return_pct', label: 'Return % (total)' },
  { key: 'cagr', label: 'Return % per year (CAGR)' },
  { key: 'profit_factor', label: 'Profit factor' },
  { key: 'win_rate', label: 'Win rate' },
  { key: 'sharpe', label: 'Sharpe' },
  { key: 'worst_year_r', label: 'Worst year (R)' },
]

/** Charts in the order a trader reads them, shortest first; one the list does not know goes last. */
const CHARTS = ['M1', 'M5', 'M15', 'M30', 'H1', 'H4', 'D1', 'W1']

/** One column of the map: a setup on a chart. */
export interface Column {
  key: string
  entryId: string
  entryName: string
  timeframe: string
}

/** One market's rows, in the order the map draws them. */
export interface MarketRows {
  market: string
  symbols: string[]
}

export interface MapLayout {
  markets: MarketRows[]
  columns: Column[]
  /** The cell of a symbol in a column, if it has one. */
  at: (symbol: string, column: Column) => BestMapCell | undefined
  /** The largest distance from the metric's neutral point among the cells drawn — both arms of
   *  the colour scale are measured against it, so one scale reads the whole map. */
  extent: number
}

/** The filters the page applies before drawing: empty means every one there is. */
export interface MapFilters {
  markets: string[]
  entries: string[]
  timeframes: string[]
}

export const NO_FILTERS: MapFilters = {
  markets: [],
  entries: [],
  timeframes: [],
}

/**
 * Where a metric's scale is neutral: zero for R, returns and Sharpe; half for the shares of
 * positive years and of winning trades; one for a profit factor, where the gains pay the losses.
 */
export function neutralOf(metric: BestMetric): number {
  if (metric === 'positive_years' || metric === 'win_rate') return 0.5
  if (metric === 'profit_factor') return 1
  return 0
}

/** A setup's name as a column heading — the catalogue's, or a short id for a removed entry. */
export function entryNameOf(cell: Pick<BestMapCell, 'entry_name' | 'entry_id'>): string {
  return cell.entry_name ?? `removed entry ${cell.entry_id.slice(0, 8)}`
}

/**
 * The map's rows and columns from the cells the API sent, after the page's filters.
 *
 * ⚠️ **Rows by market first, then by symbol.** He asked to see "by asset and by kind of market,
 * grouped so it does not pollute": the market is the group a reader scans down, the symbol the row.
 * Columns are a setup on a chart, setups by name, charts shortest first.
 */
export function layoutOf(
  cells: readonly BestMapCell[],
  metric: BestMetric,
  filters: MapFilters = NO_FILTERS,
): MapLayout {
  const kept = cells.filter(
    (cell) =>
      (filters.markets.length === 0 || filters.markets.includes(cell.market)) &&
      (filters.entries.length === 0 || filters.entries.includes(cell.entry_id)) &&
      (filters.timeframes.length === 0 || filters.timeframes.includes(cell.timeframe)),
  )

  const bySymbol = new Map<string, string>()
  for (const cell of kept) bySymbol.set(cell.symbol, cell.market)
  const markets = [...new Set(bySymbol.values())]
    .sort((a, b) => a.localeCompare(b))
    .map((market) => ({
      market,
      symbols: [...bySymbol.entries()]
        .filter(([, owner]) => owner === market)
        .map(([symbol]) => symbol)
        .sort((a, b) => a.localeCompare(b)),
    }))

  const columnsByKey = new Map<string, Column>()
  for (const cell of kept) {
    const key = `${cell.entry_id}|${cell.timeframe}`
    if (!columnsByKey.has(key)) {
      columnsByKey.set(key, {
        key,
        entryId: cell.entry_id,
        entryName: entryNameOf(cell),
        timeframe: cell.timeframe,
      })
    }
  }
  const columns = [...columnsByKey.values()].sort(
    (a, b) =>
      a.entryName.localeCompare(b.entryName) || chartOrder(a.timeframe) - chartOrder(b.timeframe),
  )

  const cellsByKey = new Map(
    kept.map((cell) => [`${cell.symbol}|${cell.entry_id}|${cell.timeframe}`, cell]),
  )
  const neutral = neutralOf(metric)
  const extent = kept.reduce(
    (largest, cell) =>
      cell.value === null ? largest : Math.max(largest, Math.abs(Number(cell.value) - neutral)),
    0,
  )

  return {
    markets,
    columns,
    at: (symbol, column) => cellsByKey.get(`${symbol}|${column.entryId}|${column.timeframe}`),
    extent,
  }
}

function chartOrder(timeframe: string): number {
  const found = CHARTS.indexOf(timeframe)
  return found === -1 ? CHARTS.length : found
}

/**
 * A cell's colour: blue above the metric's neutral point, red below, stronger the further it is,
 * against the map's own extent — the study heatmap's scale (`fillFor`), so the app has one.
 * An unbounded drawdown ratio is the strongest gain there is.
 */
export function fillOf(cell: BestMapCell, metric: BestMetric, extent: number): string {
  if (cell.unbounded) return fillFor(extent || 1, extent || 1)
  if (cell.value === null) return fillFor(null, extent)
  return fillFor(Number(cell.value) - neutralOf(metric), extent)
}

/** A value under `metric` as the map writes it in the cell. */
export function formatValue(value: string | null, metric: BestMetric, unbounded = false): string {
  if (unbounded) return '∞'
  if (value === null) return '—'
  if (metric === 'positive_years' || metric === 'win_rate') return percent(value, 0)
  if (metric === 'return_pct' || metric === 'cagr') return percent(value, 1)
  if (metric === 'recovery_r' || metric === 'sharpe') return ratio(value, 1)
  if (metric === 'profit_factor') return ratio(value, 2)
  return inR(value)
}
