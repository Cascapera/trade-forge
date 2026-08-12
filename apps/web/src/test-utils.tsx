import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, type RenderResult } from '@testing-library/react'
import type { ReactElement, ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

// Render a component inside the providers it expects — a fresh React Query client (retries off,
// so a mocked rejection surfaces at once) and a memory router at the given path.
export function renderWithProviders(ui: ReactElement, route = '/'): RenderResult {
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
    rerender: (next: ReactNode) => {
      result.rerender(wrap(next))
    },
  }
}
