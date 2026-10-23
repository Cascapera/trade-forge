import { renderHook } from '@testing-library/react'
import { vi } from 'vitest'

import type { SymbolHistory } from './types'

interface FakeResult {
  isPending: boolean
  data: SymbolHistory | undefined
}

let results: FakeResult[] = []

// `useQueries` is mocked down to the one thing this test is about: the `combine` function, which
// is where the hook decides what "measured" and "missing" mean. Driving it with hand-made
// results is the only way to hold a query *pending*, and pending is the state the whole
// auto-probe mitigation turns on.
vi.mock('@tanstack/react-query', () => ({
  useQueries: (options: { combine: (r: FakeResult[]) => unknown }) => options.combine(results),
  useMutation: () => ({ mutate: vi.fn() }),
  useQuery: () => ({}),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  skipToken: Symbol('skipToken'),
}))

import { useSymbolHistories } from './hooks'

function measured(symbol: string): SymbolHistory {
  return {
    symbol,
    timeframe: 'H1',
    oldest: '2009-01-01T00:00:00Z',
    bar_count: 1000,
    terminal_maxbars: 100000,
    bar_count_is_a_ceiling: false,
    last_fabricated: null,
    first_measured_cost: null,
    probed_at: '2026-08-21T00:00:00Z',
    capped_by_terminal: false,
    usable_from: '2009-01-01T00:00:00Z',
  }
}

function ask(symbols: string[]): { known: Map<string, SymbolHistory>; missing: string[] } {
  const { result } = renderHook(() => useSymbolHistories(symbols, 'H1'))
  return result.current
}

beforeEach(() => {
  results = []
})

it('an answered query lands in known, keyed by its symbol', () => {
  results = [{ isPending: false, data: measured('EURUSD') }]

  const { known, missing } = ask(['EURUSD'])

  expect(known.get('EURUSD')?.symbol).toBe('EURUSD')
  expect(missing).toEqual([])
})

it('a query that came back empty is missing', () => {
  // A 404 — nobody has probed this pair. This is the list the auto-probe acts on.
  results = [{ isPending: false, data: undefined }]

  expect(ask(['EURUSD']).missing).toEqual(['EURUSD'])
})

it('a query still in flight is NOT missing', () => {
  /**
   * ⚠️ **The distinction the whole mitigation rests on.** A pending query has no data either,
   * so reading "missing" as "has no data" would name every symbol the instant it was picked —
   * and the auto-probe would queue a 207-second measurement for a pair that was already being
   * measured, on the same single-job queue the collection needs.
   */
  results = [{ isPending: true, data: undefined }]

  expect(ask(['EURUSD']).missing).toEqual([])
})

it('separates the answered from the unanswered in one batch', () => {
  results = [
    { isPending: false, data: measured('EURUSD') },
    { isPending: true, data: undefined },
    { isPending: false, data: undefined },
  ]

  const { known, missing } = ask(['EURUSD', 'GBPUSD', 'USDJPY'])

  expect([...known.keys()]).toEqual(['EURUSD'])
  expect(missing).toEqual(['USDJPY'])
})

it('keys the answers by symbol rather than by position', () => {
  /**
   * ⚠️ A Map, not an array. Removing a symbol from the middle of the chosen list shifts every
   * index after it — an index-keyed answer would silently attach BTCUSD's floor to USDJPY, and
   * the suggested window would move for no visible reason.
   */
  results = [
    { isPending: false, data: undefined },
    { isPending: false, data: measured('BTCUSD') },
  ]

  const { known } = ask(['EURUSD', 'BTCUSD'])

  expect([...known.keys()]).toEqual(['BTCUSD'])
})

it('nothing chosen is nothing known and nothing missing', () => {
  const { known, missing } = ask([])

  expect(known.size).toBe(0)
  expect(missing).toEqual([])
})
