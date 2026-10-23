import { renderHook } from '@testing-library/react'
import { vi } from 'vitest'

interface ProbeArgs {
  symbol: string
  timeframe: string
}

// Typed rather than a bare `vi.fn()`: `mock.calls` is `any[]` otherwise, and this project
// forbids reading `any` — which is exactly how a test can assert on a shape that never existed.
const probe = vi.fn<(args: ProbeArgs) => void>()

/** The arguments of every probe fired, in order. */
function fired(): ProbeArgs[] {
  return probe.mock.calls.map(([args]) => args)
}

vi.mock('@tanstack/react-query', () => ({
  useMutation: () => ({ mutate: probe }),
  useQueryClient: () => ({ invalidateQueries: vi.fn() }),
  useQueries: () => ({}),
  useQuery: () => ({}),
  skipToken: Symbol('skipToken'),
}))

import { useAutoProbe } from './hooks'

beforeEach(() => {
  probe.mockClear()
})

it('measures each unmeasured symbol exactly once', () => {
  renderHook(() => useAutoProbe({ missing: ['EURUSD', 'GBPUSD'], timeframe: 'H1' }))

  expect(fired()).toEqual([
    { symbol: 'EURUSD', timeframe: 'H1' },
    { symbol: 'GBPUSD', timeframe: 'H1' },
  ])
})

it('does not measure the same pair again on a re-render', () => {
  /**
   * ⚠️ **The mitigation the whole feature's DD-04 depends on, and the reason this file exists.**
   *
   * A probe is queued on the *same single-job queue the collection will use*, and a cold H4 took
   * 207 seconds on this broker. Without the "already asked" set, every invalidation, re-render
   * or re-pick would queue the same measurement again — and twenty symbols could put hours of
   * work in front of the first candle, silently.
   *
   * Re-rendering is the honest way to test it: React re-runs an effect whenever its dependencies
   * change identity, and an array rebuilt each render changes identity every time.
   */
  const { rerender } = renderHook((props: { missing: string[] }) =>
    useAutoProbe({ missing: props.missing, timeframe: 'H1' }),
  { initialProps: { missing: ['EURUSD'] } })

  rerender({ missing: ['EURUSD'] })
  rerender({ missing: ['EURUSD'] })

  expect(probe).toHaveBeenCalledTimes(1)
})

it('measures a symbol added later without re-measuring the ones already asked', () => {
  const { rerender } = renderHook((props: { missing: string[] }) =>
    useAutoProbe({ missing: props.missing, timeframe: 'H1' }),
  { initialProps: { missing: ['EURUSD'] } })

  rerender({ missing: ['EURUSD', 'GBPUSD'] })

  expect(fired().map((args) => args.symbol)).toEqual(['EURUSD', 'GBPUSD'])
})

it('a symbol removed and picked again is not measured a second time', () => {
  // The cache is per pair, not per selection. Un-picking and re-picking is the cheapest thing a
  // person does on this screen, and it must not cost 207 seconds of queue.
  const { rerender } = renderHook((props: { missing: string[] }) =>
    useAutoProbe({ missing: props.missing, timeframe: 'H1' }),
  { initialProps: { missing: ['EURUSD'] } })

  rerender({ missing: [] })
  rerender({ missing: ['EURUSD'] })

  expect(probe).toHaveBeenCalledTimes(1)
})

it('the same symbol on another timeframe is a different measurement', () => {
  /**
   * ⚠️ The key is the **pair**, not the symbol. EURUSD H1 and EURUSD M1 are separate series with
   * separate floors — this project measured them as 178,642 bars against a terminal-capped
   * 10,000,000 — so a cache keyed on the symbol alone would leave M1 permanently unmeasured
   * after anyone had looked at H1.
   */
  const { rerender } = renderHook((props: { timeframe: string }) =>
    useAutoProbe({ missing: ['EURUSD'], timeframe: props.timeframe }),
  { initialProps: { timeframe: 'H1' } })

  rerender({ timeframe: 'M1' })

  expect(fired().map((args) => args.timeframe)).toEqual(['H1', 'M1'])
})

it('measures nothing when nothing is missing', () => {
  renderHook(() => useAutoProbe({ missing: [], timeframe: 'H1' }))

  expect(probe).not.toHaveBeenCalled()
})

it('reports how many measurements are still ahead', () => {
  const { result } = renderHook(() =>
    useAutoProbe({ missing: ['EURUSD', 'GBPUSD', 'USDJPY'], timeframe: 'H1' }),
  )

  expect(result.current.queued).toBe(3)
})
