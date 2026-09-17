import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderResult } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

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
