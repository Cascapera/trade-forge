import { expect, test, type Route } from '@playwright/test'

// Not a CI test — a generator. `npm run screenshot` runs only this file (see package.json) and
// writes the results-screen image the README embeds. It drives the real UI in chromium with the
// API mocked, so the screenshot is the actual product, not a mockup.

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
    net_profit: '1240.50',
    gross_profit: '2100.00',
    gross_loss: '-859.50',
    total_trades: 18,
    long_trades: 11,
    short_trades: 7,
    win_rate: '0.5556',
    payoff: '1.42',
    profit_factor: '2.44',
    expectancy: '68.92',
    max_drawdown_abs: '410.00',
    max_drawdown_pct: '0.0381',
    max_dd_duration_days: 6,
    sharpe: '1.87',
    sortino: '2.61',
    cagr: null,
    avg_trade_duration: null,
  },
}
const trade = (id: number, net: string, r: string, reason: string, dir: 'long' | 'short') => ({
  id,
  direction: dir,
  entry_time: '2024-03-01T09:00:00Z',
  entry_price: '1.10250',
  exit_time: '2024-03-01T14:00:00Z',
  exit_price: '1.10620',
  exit_reason: reason,
  volume: '0.25',
  stop_loss: '1.10000',
  take_profit: '1.10750',
  gross_pnl: net,
  costs: '0',
  net_pnl: net,
  r_multiple: r,
  context: {},
})
const trades = {
  total: 4,
  limit: 100,
  offset: 0,
  items: [
    trade(1, '500.00', '2.00', 'tp', 'long'),
    trade(2, '-250.00', '-1.00', 'sl', 'short'),
    trade(3, '420.50', '1.68', 'tp', 'long'),
    trade(4, '180.00', '0.72', 'condition', 'long'),
  ],
}
const equity = Array.from({ length: 40 }, (_, index) => {
  const base = 10000 + index * 30
  const wobble = Math.round(Math.sin(index / 3) * 120)
  return {
    time: new Date(Date.UTC(2024, 0, 1 + index)).toISOString(),
    equity: String(base + wobble),
  }
})

const json = (route: Route, body: unknown, status = 200): Promise<void> =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

test('capture the results screen for the README', async ({ page }) => {
  await page.setViewportSize({ width: 1200, height: 1000 })
  await page.route('**/api/instruments', (route) => json(route, [instrument]))
  await page.route('**/api/strategies', (route) => json(route, strategy, 201))
  await page.route('**/api/backtests', (route) => json(route, { id: 'b1', status: 'queued' }, 202))
  await page.route('**/api/backtests/b1', (route) => json(route, doneRun))
  await page.route('**/api/backtests/b1/trades**', (route) => json(route, trades))
  await page.route('**/api/backtests/b1/equity', (route) => json(route, equity))

  await page.goto('/')
  await page.getByRole('button', { name: /save & configure/i }).click()
  await page.getByRole('button', { name: /run backtest/i }).click()

  await expect(page.getByRole('heading', { name: 'Backtest results' })).toBeVisible()
  await expect(page.getByText('2.00R')).toBeVisible()
  // Let the equity chart finish its first paint before capturing.
  await page.waitForTimeout(500)
  await page.screenshot({ path: '../../docs/assets/results.png', fullPage: true })
})
