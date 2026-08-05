// The API's response and request shapes, mirrored from `tradeforge_api.schemas`.
//
// These are the HTTP DTOs, deliberately NOT the strategy DSL (that type comes from
// `@tradeforge/schema`, generated from the shared JSON Schema and never hand-written). Every
// monetary or ratio field is a `string`, because the backend serialises `Decimal` as a string
// to keep it exact — a JSON number would be a float, and the precision the engine and database
// preserved would be lost on the wire. The UI parses these only at the edge, to display.

export type BacktestStatus = 'queued' | 'running' | 'done' | 'failed'

export interface Instrument {
  id: string
  symbol: string
  name: string
  asset_class: string
  currency_quote: string
  currency_base: string | null
  tick_size: string
  tick_value: string
  contract_size: string
  digits: number
}

export interface StrategyOut {
  id: string
  name: string
  version: number
  schema_version: string
  definition: Record<string, unknown>
  created_at: string
}

export interface Metrics {
  net_profit: string
  gross_profit: string
  gross_loss: string
  total_trades: number
  long_trades: number
  short_trades: number
  win_rate: string
  payoff: string | null
  profit_factor: string | null
  expectancy: string | null
  max_drawdown_abs: string
  max_drawdown_pct: string
  max_dd_duration_days: number
  sharpe: string | null
  sortino: string | null
  cagr: string | null
  avg_trade_duration: string | null
}

export interface Backtest {
  id: string
  strategy_id: string
  instrument_id: string
  timeframe: string
  date_from: string
  date_to: string
  initial_capital: string
  status: BacktestStatus
  error: string | null
  engine_version: string
  created_at: string
  started_at: string | null
  finished_at: string | null
  // What the run actually read. `date_from`/`date_to` above are only the request: the dataset
  // underneath may start later or end earlier. Null on runs that failed, and on rows written
  // before the API recorded this — null means unknown, never zero.
  candles_seen: number | null
  first_candle: string | null
  last_candle: string | null
  metrics: Metrics | null
}

export interface Trade {
  id: number
  direction: 'long' | 'short'
  entry_time: string
  entry_price: string
  exit_time: string | null
  exit_price: string | null
  exit_reason: string | null
  volume: string
  stop_loss: string | null
  take_profit: string | null
  gross_pnl: string | null
  costs: string | null
  net_pnl: string | null
  r_multiple: string | null
  context: Record<string, string | null>
  /**
   * Whether this trade has an entry picture to fetch — not the picture itself.
   *
   * A snapshot is fifty-odd bars, and a reader opens the two or three entries that look wrong,
   * not all of them. So the list says only that one exists and `getTradeSnapshot` fetches it
   * when asked. `false` on a run older than the feature.
   */
  has_snapshot: boolean
}

/** One bar of an entry's picture. Prices are strings for the same reason every price here is. */
export interface SnapshotBar {
  time: string
  open: string
  high: string
  low: string
  close: string
}

/**
 * A rectangle to draw over the bars: a band of price with a left edge in time.
 *
 * `from_time` is the candle that *formed* the zone and is routinely older than the window's
 * first bar. Clamp the drawing at the chart's left edge; never move `from_time` to the first
 * visible bar, which would redraw the zone as younger than it is.
 */
export interface SnapshotRegion {
  label: string
  top: string
  bottom: string
  from_time: string
}

/**
 * A curve to draw across the bars — an indicator, as the strategy computed it.
 *
 * Each point is `[time, value]`. Join to the bars **on the timestamp**, never by position: the
 * curve legitimately ends before the last bar (it stops at the decision, while the bars run on
 * to the fill) and legitimately starts after the first (the indicator was warming up).
 */
export interface SnapshotSeries {
  label: string
  points: [string, string][]
}

/**
 * A horizontal segment: the structure a break of structure broke.
 *
 * Bounded at **both** ends, unlike a region. A zone stays in force after the entry and extends
 * rightward; a level is over the moment it is crossed. Its length is how long the structure
 * held — draw it between the two instants and do not extend it.
 *
 * `label` is `choch` or `bos`: one turns the trend, the other continues it.
 */
export interface SnapshotLevel {
  label: string
  price: string
  from_time: string
  to_time: string
}

export interface Snapshot {
  decided_at: string
  /** The bar the order filled on — the window's last. Equal to `decided_at` on a long rest. */
  filled_at: string
  bars: SnapshotBar[]
  regions: SnapshotRegion[]
  series: SnapshotSeries[]
  levels: SnapshotLevel[]
}

export interface TradesPage {
  total: number
  limit: number
  offset: number
  items: Trade[]
}

export interface EquityPoint {
  time: string
  equity: string
}

export interface CreatedBacktest {
  id: string
  status: BacktestStatus
}

export interface CreateBacktestRequest {
  strategy_id: string
  symbol: string
  timeframe: string
  date_from: string
  date_to: string
  initial_capital: string
  cost_model: Record<string, unknown>
}
