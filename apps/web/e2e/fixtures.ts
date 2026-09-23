// The fixtures and the walk to the results screen, shared by both specs.
//
// ⚠️ **Shared because two copies were how these files rotted apart.** Each spec carried its own
// ninety lines of the same fixtures, so a field the API grew reached neither, and a screen that
// was renamed had to be fixed twice — which meant it was fixed nowhere, because nothing ran them.
// One copy is one place to be wrong.

import { expect, type Page, type Route } from '@playwright/test'

// ⚠️ **Run these against the dev server, which is what CI runs them against.** The config's
// `reuseExistingServer` means a container already listening on 5173 gets used instead — a
// *built* app, where `/src/**` does not exist and the module graph is bundled. Two failures in
// this file came from passing locally against that and failing on the runner. `docker compose
// stop web` before running by hand, or trust CI to be the one that knows.

export const strategy = {
  id: 's1',
  name: 'MA cross',
  version: 1,
  schema_version: '1.0',
  // ⚠️ The chart is the one field New backtest reads off the document, and it refuses to run a
  // strategy whose document it cannot read one from.
  definition: { timeframe: 'H1' },
  created_at: '2024-01-01T00:00:00Z',
}

/** The same strategy as one row of `GET /strategies`, which is what New backtest's picker lists. */
export const strategyRow = {
  id: 's1',
  name: 'MA cross',
  version: 1,
  schema_version: '1.0',
  setup: null,
  runs: 0,
  created_at: '2024-01-01T00:00:00Z',
}

// The market picker is a combobox over `/api/symbols/search`, not a select over the catalogue:
// it searches the broker's terminal by prefix. This is what one row of that answer looks like.
// ⚠️ Still needed, and the net is what proved it. The market is *picked* through the broker
// search, but the catalogue is what the run settings read to pre-fill a cost model — so removing
// this route on the theory that the combobox replaced it left a call that only succeeded on a
// machine with the backend running.
export const instruments = [
  {
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
    // `null`, not zero: nobody measured this one. Zero would claim it is free to trade.
    default_spread_points: null,
  },
]

export const symbolSearch = {
  symbols: [
    {
      symbol: 'EURUSD',
      description: 'Euro vs US Dollar',
      path: 'Forex\EURUSD',
      digits: 5,
      visible: true,
      asset_class: 'forex',
      currency_base: 'EUR',
      currency_quote: 'USD',
      tick_size: '0.00001',
      tick_value: '1',
      contract_size: '100000',
      spread: 1,
    },
  ],
  snapshot: null,
}

export const doneRun = {
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
  // Both present for the reason every key below is: the screen reads them, and a missing one is
  // `undefined`, which walks past a `!== null` guard. Without `targets` the run page threw while
  // drawing the ladder; without `recorded` it said this single run had kept less than it did.
  recorded: 'full',
  targets: null,
  created_at: '2024-01-01T00:00:00Z',
  started_at: '2024-01-01T00:00:01Z',
  finished_at: '2024-01-01T00:00:02Z',
  // Nothing was downloaded for this run — the journey is not about waiting for a collection.
  // Present for the reason the keys below are: the API always sends it, and the screen reads it.
  waiting_for: [],
  // ⚠️ Present, and that is not padding. `coverageNotice` guards these with `=== null`, so a
  // fixture that simply omits them makes them `undefined`, walks straight past the guard and
  // throws inside the date formatter — a blank screen where the results should be. The API
  // always sends the keys; a fixture that does not is a fake that disagrees with the real thing.
  candles_seen: 3,
  first_candle: '2024-01-01T00:00:00Z',
  last_candle: '2024-12-31T00:00:00Z',
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

export const trades = {
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

export const candles = {
  timeframe: 'H1',
  symbol: 'EURUSD',
  candles_seen: 3,
  first_candle: '2024-01-01T00:00:00Z',
  last_candle: '2024-01-01T02:00:00Z',
  count: 3,
  candles: [
    { time: '2024-01-01T00:00:00Z', open: '1.1', high: '1.11', low: '1.09', close: '1.1', volume: 10 },
    { time: '2024-01-01T01:00:00Z', open: '1.1', high: '1.12', low: '1.1', close: '1.102', volume: 12 },
    { time: '2024-01-01T02:00:00Z', open: '1.102', high: '1.103', low: '1.1', close: '1.101', volume: 8 },
  ],
}

export const overlays = {
  symbol: 'EURUSD',
  timeframe: 'H1',
  candles_seen: 3,
  count: 0,
  series: [],
}

export const equity = [
  { time: '2024-01-01T00:00:00Z', equity: '10000' },
  { time: '2024-01-01T01:00:00Z', equity: '10200' },
  { time: '2024-01-01T02:00:00Z', equity: '10100' },
]

export const json = (route: Route, body: unknown, status = 200): Promise<void> =>
  route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })

/** Every call the journey makes, answered from the fixtures above. */
export async function mockApi(page: Page): Promise<void> {
  // ⚠️ **Registered first, and that is not a style choice: playwright matches routes in the
  // reverse order they were added**, so the last handler registered wins. A net added at the
  // bottom swallows every call the specific routes below were written to answer — which is
  // exactly what it did on the first attempt, and the suite then failed naming the search
  // endpoint it does mock.
  //
  // The net exists because this suite passed here and failed on CI. The dev server proxies
  // `/api` to port 8000, where a developer usually has the real backend running, so an unmocked
  // call quietly succeeded against live data locally and met a closed port on the runner. The
  // journey then failed on a later assertion, naming a screen instead of the missing request.
  //
  // Refused loudly rather than answered: a fixture invented here would be a mock nobody chose.
  //
  // ⚠️ Anchored on the origin, not `**/api/**`. That glob also matches `/src/api/client.ts` —
  // a module the **dev server** serves, which only exists when vite is compiling on demand. The
  // production build has no such URL, so a net written as a glob passes against a built app and
  // refuses the app's own source on CI.
  await page.route(/^https?:\/\/[^/]+\/api\//, (route) => {
    const request = route.request()
    throw new Error(
      `the journey called ${request.method()} ${request.url()}, which no fixture answers — ` +
        'add it to mockApi rather than letting it reach a backend that only exists on your machine',
    )
  })

  await page.route('**/api/instruments', (route) => json(route, instruments))
  await page.route(/\/api\/symbols\/search/, (route) => json(route, symbolSearch))
  // A symbol nobody has probed answers 404, which the note beside the field is built to read.
  await page.route(/\/api\/symbols\/[^/]+\/history/, (route) => json(route, {}, 404))
  // ⚠️ One path, four questions, and a glob without the query string only ever answered one of
  // them. `POST` saves the strategy; the builder first `GET`s the list with a `name` filter to
  // find whether the name already has a lineage (it must not, or the save becomes a `PUT`); New
  // backtest's picker `GET`s the list without one and must find the strategy just saved; and it
  // then `GET`s the document itself for the chart. Matched by regex so the query comes along.
  await page.route(/\/api\/strategies/, (route) => {
    const request = route.request()
    if (request.method() === 'POST') return json(route, strategy, 201)
    const url = new URL(request.url())
    if (url.pathname.endsWith('/strategies/s1')) return json(route, strategy)
    return url.searchParams.has('name')
      ? json(route, { total: 0, limit: 20, offset: 0, items: [] })
      : json(route, { total: 1, limit: 200, offset: 0, items: [strategyRow] })
  })
  // The catalogue is where the journey starts now; an empty shelf is all it needs to read.
  await page.route('**/api/catalog', (route) => json(route, { total: 0, items: [] }))
  // Asked before the launch: nothing missing, so the journey runs straight through as it did.
  await page.route('**/api/collections/plan', (route) => json(route, []))
  await page.route('**/api/backtests', (route) => json(route, { id: 'b1', status: 'queued' }, 202))
  await page.route('**/api/backtests/b1', (route) => json(route, doneRun))
  await page.route(/\/api\/backtests\/b1\/trades/, (route) => json(route, trades))
  await page.route('**/api/backtests/b1/equity', (route) => json(route, equity))
  await page.route('**/api/backtests/b1/candles', (route) => json(route, candles))
  await page.route('**/api/backtests/b1/overlays', (route) => json(route, overlays))

}

/**
 * From an empty builder to the results screen, the way a person gets there.
 *
 * ⚠️ **Two screens again, for a different reason than before.** Building and running were once
 * separate pages joined by a `save & configure` button, then one form whose button saved and ran
 * together (PR-249 and before). Since PR-250 building lives in the catalogue and only saves, and
 * New backtest runs a strategy that is already saved — so the journey crosses between them.
 */
export async function runToResults(page: Page): Promise<void> {
  await page.goto('/catalog')
  await page.getByRole('button', { name: 'New strategy' }).click()
  await expect(page.getByRole('heading', { name: 'Build a strategy' })).toBeVisible()

  // ⚠️ `side` has no schema default on purpose — a pre-selected one turns a forgotten choice into
  // a long-only run read as the setup's result — so the button stays disabled until it is
  // answered. Asserting that first is what makes the click below mean anything.
  const save = page.getByRole('button', { name: /save strategy/i })
  await expect(save).toBeDisabled()
  await page.getByLabel('setup side').selectOption('long')
  await expect(save).toBeEnabled()
  await save.click()

  // Saved, not run and not shelved — and the way on is the link the builder offers.
  await page.getByRole('link', { name: /run it in new backtest/i }).click()
  await expect(page.getByRole('heading', { name: 'New backtest' })).toBeVisible()
  // Preselected from the save, and its chart read off the document.
  await expect(page.getByText('as the strategy was written')).toBeVisible()

  // The market comes from the broker search, typed and picked the way a person does it.
  await page.getByLabel('Symbol').fill('EUR')
  await page.getByRole('option', { name: /EURUSD/ }).click()

  const run = page.getByRole('button', { name: /run backtest/i })
  await expect(run).toBeEnabled()
  await run.click()
  await expect(page.getByRole('heading', { name: 'Backtest results' })).toBeVisible()
}
