import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderResult } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

import type { api } from './api/client'
import type { BrowsedSymbol, Markets, SymbolBrowse } from './api/types'

// Render a component inside the providers it expects — a fresh React Query client (retries off,
// so a mocked rejection surfaces at once) and a memory router at the given path. A route given as
// an object also carries navigation `state`, the way `navigate(to, { state })` would.
export function renderWithProviders(
  ui: ReactElement,
  route: string | { pathname: string; state: unknown } = '/',
): RenderResult & { client: QueryClient } {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  const wrap = (node: ReactNode): ReactElement => (
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[route]}>{node}</MemoryRouter>
    </QueryClientProvider>
  )
  const result = render(wrap(ui))
  // `rerender` has to go back through the providers. Testing Library replaces the whole tree, so
  // re-rendering the bare element drops the router and query contexts — which surfaces as
  // "Cannot destructure property 'basename' of null" from the first `Link` deep inside, an error
  // that says nothing about the test that caused it. The providers themselves keep their state:
  // same component in the same position, so `initialEntries` is not re-applied.
  return {
    ...result,
    // ⚠️ **Handed back so a test can make the server's answer change under a mounted screen.**
    // Re-pointing a mocked client method does nothing on its own — React Query will not ask
    // again just because the fixture moved — so a test written that way measures a machine that
    // was never given the chance to speak. Invalidating through this client is what actually
    // reproduces "somebody removed that row in another tab".
    client,
    rerender: (next: ReactNode) => {
      result.rerender(wrap(next))
    },
  }
}

/**
 * A broker's list as `/symbols/markets` and `/symbols/browse` serve it, for screens that open the
 * market browser: filtered and paged the way the API does, so a test can page and search.
 */
export function fakeBroker(rows: readonly BrowsedSymbol[]): {
  markets: () => Promise<Markets>
  browse: (params: Parameters<typeof api.browseSymbols>[0]) => Promise<SymbolBrowse>
} {
  const keys = ['forex', 'crypto', 'indices', 'stocks_us', 'stocks_br', 'other']
  return {
    markets: () =>
      Promise.resolve({
        markets: keys.map((key) => ({
          key,
          label: key,
          count: rows.filter((one) => one.market === key).length,
          collected: rows.filter((one) => one.market === key && one.catalogued).length,
        })),
        snapshot: { server: 'Broker-Server', synced_at: '2026-10-02T12:00:00Z' },
      }),
    browse: ({ market, q = '', collected = false, offset = 0, limit = 25 }) => {
      const needle = q.toLowerCase()
      const found = rows.filter(
        (one) =>
          (market === undefined || one.market === market) &&
          (!collected || one.catalogued) &&
          (one.symbol.toLowerCase().includes(needle) ||
            (one.description ?? '').toLowerCase().includes(needle)),
      )
      return Promise.resolve({
        total: found.length,
        offset,
        limit,
        items: found.slice(offset, offset + limit),
      })
    },
  }
}

/** One row of `fakeBroker`'s list. */
export function browsed(
  symbol: string,
  market: string,
  spread: string | null,
  catalogued = true,
): BrowsedSymbol {
  return {
    symbol,
    description: `${symbol} description`,
    path: null,
    market,
    catalogued,
    spread_points: spread,
  }
}
