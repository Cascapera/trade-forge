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
