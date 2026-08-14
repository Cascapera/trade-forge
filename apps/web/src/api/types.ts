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
  /**
   * The broker's quoted spread in ticks, for pre-filling a run's cost model.
   *
   * `null` means nobody has measured this symbol — a seeded row, or one catalogued before
   * the collector recorded it. Not zero, which would be the claim that this instrument is
   * free to trade, and the screen has to be able to tell the two apart: it charges nothing
   * for an unmeasured instrument, but says so rather than implying the number is a result.
   */
  default_spread_points: string | null
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

/**
 * One row of the run log. Deliberately not `Backtest`, and the differences are the point.
 *
 * It resolves what the detail shape leaves as foreign keys — `symbol`, `strategy_name`,
 * `strategy_version` — because a list is read by a human choosing where to look, and an
 * `instrument_id` answers nothing. And it carries `cost_model`, because under ADR-07 the same
 * strategy over the same window with a wider spread is a *different experiment*: a table that
 * hid that would invite reading two incomparable rows as like for like.
 *
 * What it does not carry is the equity curve. That is fetched per run, only for the runs
 * someone selected — see `useEquityCurves`.
 */
export interface BacktestListItem {
  id: string
  strategy_id: string
  strategy_name: string
  strategy_version: number
  symbol: string
  timeframe: string
  date_from: string
  date_to: string
  initial_capital: string
  cost_model: Record<string, unknown>
  status: BacktestStatus
  error: string | null
  created_at: string
  finished_at: string | null
  metrics: Metrics | null
}

export interface BacktestsPage {
  total: number
  limit: number
  offset: number
  items: BacktestListItem[]
}

/** The run log's filters. Every field absent means "no filter", which is the default view. */
export interface BacktestFilters {
  symbol?: string
  timeframe?: string
  status?: BacktestStatus
  limit?: number
  offset?: number
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

/** One bar of the price chart. Prices are strings for the same reason every price here is. */
export interface Candle {
  time: string
  open: string
  high: string
  low: string
  close: string
}

/**
 * The bars a run read, with the provenance to check them against.
 *
 * `candles_seen` is what the run *recorded* eating; `count` is what was found on disk just now.
 * They are two different questions — Parquet underneath a run can be re-collected or extended
 * afterwards — and they are both carried so a client can see them disagree instead of drawing a
 * chart that quietly covers a different period than the trades plotted on it.
 */
export interface CandlesResponse {
  timeframe: string
  symbol: string
  candles_seen: number
  first_candle: string
  last_candle: string
  count: number
  candles: Candle[]
}

/**
 * A curve to draw across the run's bars — an indicator, as the strategy computed it.
 *
 * Each point is `[time, value]`. Join to the candles **on the timestamp**, never by index: the
 * series is shorter than the bars whenever the indicator was warming up, so an index join draws
 * every point one period to the left of where it belongs — and the shape still looks right.
 */
export interface OverlaySeries {
  label: string
  points: [string, string][]
}

/**
 * One region over the whole run, with both ends of its life.
 *
 * Three instants, none interchangeable. `from_time` is where the rectangle begins — the candle
 * before the gap, routinely far older than the break that revealed it. `confirmed_at` is when a
 * strategy could first act on it; the two are a median of 8 bars apart on real data, so
 * collapsing them draws most regions much younger than they are. `mitigated_at` is the bar whose
 * wick took the region, and `null` means it was still standing when the run ended — extend that
 * one to the chart's right edge rather than closing it somewhere invented.
 */
export interface Zone {
  kind: 'demand' | 'supply'
  top: string
  bottom: string
  from_time: string
  confirmed_at: string
  mitigated_at: string | null
  primary: boolean
}

/** Every curve the run's strategy was reading. Empty for a setup whose overlay is zones. */
export interface OverlaysResponse {
  symbol: string
  timeframe: string
  /**
   * The same provenance pair the candles carry, and the curve needs it *more* than they do: the
   * bars are read back, while the curve is recomputed over them, so one extra bar inside the
   * window does not add a point at the end — it reseeds the average and moves the whole line.
   */
  candles_seen: number
  count: number
  series: OverlaySeries[]
  /** Empty for the swing setups, which read a curve and mark no zones. The two halves are
   * independent: having one says nothing about whether a strategy has the other. */
  zones: Zone[]
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

// --------------------------------------------------------------------------- //
// Baskets — one strategy across several markets                                 //
// --------------------------------------------------------------------------- //

/**
 * Launch one strategy over several symbols, one run each.
 *
 * ⚠️ **No `cost_model`, and that absence is the contract.** Each run is charged the spread
 * measured for *its own* instrument, resolved by the server. A single figure across a basket
 * would be meaningless: 8 ticks of EURUSD and 4 of AAPL are not only different numbers, they
 * are counted in tick sizes that differ by a factor of a thousand. The screen's job is to
 * *show* what each symbol will be charged before the launch, not to choose it.
 */
export interface CreateBasketRequest {
  strategy_id: string
  symbols: string[]
  timeframe: string
  date_from: string
  date_to: string
  initial_capital: string
}

/** One symbol's place in the basket: which run it became, and what it is being charged. */
export interface BasketRunOut {
  backtest_id: string
  symbol: string
  status: BacktestStatus
  cost_model: Record<string, unknown>
  /** `null` means nobody measured this symbol, so the run is uncosted. Never zero. */
  default_spread_points: string | null
}

export interface CreatedBasket {
  id: string
  runs: BasketRunOut[]
}

/**
 * What a basket says once its runs finish — **dispersion, never a combined account.**
 *
 * There is no summed equity curve in this shape, and its absence is deliberate. Every run in a
 * basket starts with the whole `initial_capital`, so four runs of $10 000 are neither a $10 000
 * account nor a $40 000 one. Adding the curves would draw a line that looks like a portfolio and
 * is not one — the same failure as the forward-fill the run comparator refuses.
 *
 * The median rather than the mean, and the extremes by name: a strategy returning 30% on one
 * symbol and −25% on another has an average near zero and a story the average destroys.
 *
 * `null` for every statistic until at least one run finishes. Undefined, not zero — a basket
 * whose runs are still queued has not returned 0%.
 */
export interface BasketAggregate {
  runs_total: number
  runs_finished: number
  runs_failed: number
  runs_profitable: number
  best_symbol: string | null
  best_return: string | null
  worst_symbol: string | null
  worst_return: string | null
  median_return: string | null
}

/**
 * A basket read back: how it was launched, every run in it, and how far apart they landed.
 *
 * `runs` is `BacktestListItem[]` — the same row the run log renders, from the same builder on the
 * server. That is why this screen reuses `RunTable` and `ComparisonChart` unchanged: a basket
 * assembling its own idea of a run would drift from the log's the first time a column is added,
 * and the two views would then disagree about what a run *is* while both looking right.
 */
export interface BasketOut {
  id: string
  strategy_id: string
  strategy_name: string
  strategy_version: number
  timeframe: string
  date_from: string
  date_to: string
  initial_capital: string
  created_at: string
  aggregate: BasketAggregate
  runs: BacktestListItem[]
}
