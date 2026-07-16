import { expect, test, type Route } from '@playwright/test'

// The whole user journey — build a strategy, run a backtest, read the results — in a real
// browser, with every API call fulfilled from a fixture. The point is the UI flow and its wiring
// (navigation, the poll settling on `done`, the results rendering), not the backend, which has
// its own end-to-end test in `apps/api`.

const strategy = {
  id: 's1',
  name: 'MA cross',
  version: 1,
  schema_version: '1.0',
  definition: {},
  created_at: '2024-01-01T00:00:00Z',
}

const instrument = {
  id: 'i1',
  symbol: 'EURUSD',
  name: 'Euro vs US Dollar',
  asset_class: 'forex',
  currency_quote: 'USD',
  currency_base: 'EUR',
  tick_size: '0.00001',
  tick_value: '1',
  contract_size: '100000',
  digits: 5,
}

const doneRun = {
  id: 'b1',
  strategy_id: 's1',
  instrument_id: 'i1',
  timeframe: 'H1',
  date_from: '2024-01-01T00:00:00Z',
  date_to: '2024-12-31T00:00:00Z',
  initial_capital: '10000',
  status: 'done',
  error: null,
  engine_version: '0.1.0',
  created_at: '2024-01-01T00:00:00Z',
  started_at: '2024-01-01T00:00:01Z',
  finished_at: '2024-01-01T00:00:02Z',
  metrics: {
    net_profit: '100',
    gross_profit: '200',
    gross_loss: '-100',
    total_trades: 1,
    long_trades: 1,
    short_trades: 0,
    win_rate: '1',
    payoff: null,
    profit_factor: null,
    expectancy: '100',
    max_drawdown_abs: '0',
    max_drawdown_pct: '0',
    max_dd_duration_days: 0,
    sharpe: null,
    sortino: null,
    cagr: null,
    avg_trade_duration: null,
  },
}

const trades = {
  total: 1,
  limit: 100,
  offset: 0,
  items: [
    {
      id: 1,
      direction: 'long',
      entry_time: '2024-01-01T00:00:00Z',
      entry_price: '1.10000',
      exit_time: '2024-01-01T01:00:00Z',
      exit_price: '1.10200',
      exit_reason: 'tp',
      volume: '1',
      stop_loss: '1.09900',
      take_profit: '1.10200',
      gross_pnl: '200',
      costs: '0',
      net_pnl: '200',
      r_multiple: '2',
      context: {},
    },
  ],
}

const equity = [
  { time: '2024-01-01T00:00:00Z', equity: '10000' },
  { time: '2024-01-01T01:00:00Z', equity: '10200' },
  { time: '2024-01-01T02:00:00Z', equity: '10100' },
]

const json = (route: Route, body: unknown, status = 200): Promise<void> =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

test('build a strategy, run a backtest, and read the results', async ({ page }) => {
  await page.route('**/api/instruments', (route) => json(route, [instrument]))
  await page.route('**/api/strategies', (route) => json(route, strategy, 201))
  await page.route('**/api/backtests', (route) => json(route, { id: 'b1', status: 'queued' }, 202))
  await page.route('**/api/backtests/b1', (route) => json(route, doneRun))
  await page.route('**/api/backtests/b1/trades**', (route) => json(route, trades))
  await page.route('**/api/backtests/b1/equity', (route) => json(route, equity))

  await page.goto('/')
  await expect(page.getByRole('heading', { name: 'Build a strategy' })).toBeVisible()
  await page.getByRole('button', { name: /save & configure/i }).click()

  await expect(page.getByRole('heading', { name: /Backtest/ })).toBeVisible()
  await page.getByRole('button', { name: /run backtest/i }).click()

  await expect(page.getByRole('heading', { name: 'Backtest results' })).toBeVisible()
  await expect(page.getByText('done')).toBeVisible()
  // The net-profit tile, addressed by its label so it is not confused with the equal expectancy.
  await expect(page.getByText('Net profit').locator('..')).toContainText('+100.00')
  await expect(page.getByText('2.00R')).toBeVisible() // the trade's R-multiple, in the table
})
