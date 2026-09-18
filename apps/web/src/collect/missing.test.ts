import type { PlannedCollection } from '../api/types'

import { anythingToRun, missingLine } from './missing'

const Y2019 = { date_from: '2019-01-01T00:00:00Z', date_to: '2020-12-31T23:59:59.999999Z' }
const Y2026 = { date_from: '2026-01-01T00:00:00Z', date_to: '2026-09-17T12:00:00Z' }

function planned(
  symbol: string,
  windows = [Y2019],
  { timeframe = 'H1', covers = null as string | null, inWindow = false } = {},
): PlannedCollection {
  return { symbol, timeframe, covers, windows, in_window: inWindow }
}

describe('missingLine', () => {
  it('says a market was never collected and which years would be fetched', () => {
    expect(missingLine(planned('GBPUSD'))).toBe('GBPUSD H1 — never collected; would fetch 2019–2020')
  })

  it('says what the disk holds when it holds something', () => {
    expect(
      missingLine(planned('EURUSD', [Y2019, Y2026], { covers: '2021-01-04 to 2025-12-31' })),
    ).toBe('EURUSD H1 — on disk 2021-01-04 to 2025-12-31; would fetch 2019–2020, 2026')
  })

  it('names a single year once rather than as a range', () => {
    const one = { date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }
    expect(missingLine(planned('EURUSD', [one]))).toContain('would fetch 2024')
  })
})

describe('anythingToRun', () => {
  it('is true for a market with candles inside the window', () => {
    expect(anythingToRun(['EURUSD'], [planned('EURUSD', [Y2019], { inWindow: true })])).toBe(true)
  })

  it('is false when the only market has none', () => {
    // ⚠️ Even with a `covers` string: collected for other years is nothing to run over.
    expect(
      anythingToRun(
        ['EURUSD'],
        [planned('EURUSD', [Y2019], { covers: '2015-01-05 to 2016-12-30' })],
      ),
    ).toBe(false)
  })

  it('is true when one market of a basket has none and another does', () => {
    expect(anythingToRun(['EURUSD', 'GBPUSD'], [planned('GBPUSD')])).toBe(true)
  })

  it('is false when every market of a basket has none', () => {
    expect(anythingToRun(['EURUSD', 'GBPUSD'], [planned('EURUSD'), planned('GBPUSD')])).toBe(false)
  })
})
