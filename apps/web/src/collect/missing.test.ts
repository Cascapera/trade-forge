import type { PlannedCollection } from '../api/types'

import { catalogueClasses, collectionRequests, missingLine, queuedKeys } from './missing'

const Y2019 = { date_from: '2019-01-01T00:00:00Z', date_to: '2020-12-31T23:59:59.999999Z' }
const Y2026 = { date_from: '2026-01-01T00:00:00Z', date_to: '2026-09-17T12:00:00Z' }

function planned(
  symbol: string,
  windows = [Y2019],
  { timeframe = 'H1', covers = null as string | null } = {},
): PlannedCollection {
  return { symbol, timeframe, covers, windows }
}

describe('collectionRequests', () => {
  it('sends markets that need the same window at the same chart in one request', () => {
    expect(collectionRequests([planned('EURUSD'), planned('GBPUSD')])).toEqual([
      {
        items: [{ symbol: 'EURUSD' }, { symbol: 'GBPUSD' }],
        rows: [{ timeframe: 'H1', ...Y2019 }],
      },
    ])
  })

  it('keeps different windows apart', () => {
    expect(collectionRequests([planned('EURUSD'), planned('GBPUSD', [Y2026])])).toEqual([
      { items: [{ symbol: 'EURUSD' }], rows: [{ timeframe: 'H1', ...Y2019 }] },
      { items: [{ symbol: 'GBPUSD' }], rows: [{ timeframe: 'H1', ...Y2026 }] },
    ])
  })

  it('keeps different charts apart, even over the same window', () => {
    expect(
      collectionRequests([planned('EURUSD'), planned('EURUSD', [Y2019], { timeframe: 'H4' })]),
    ).toEqual([
      { items: [{ symbol: 'EURUSD' }], rows: [{ timeframe: 'H1', ...Y2019 }] },
      { items: [{ symbol: 'EURUSD' }], rows: [{ timeframe: 'H4', ...Y2019 }] },
    ])
  })

  it('sends a pair with two windows as two requests, which the server requires', () => {
    // ⚠️ `POST /collections` refuses one timeframe twice in a request, so the two gaps of one
    // pair can never share one — a row per request makes that impossible by construction.
    const requests = collectionRequests([planned('EURUSD', [Y2019, Y2026])])
    expect(requests).toHaveLength(2)
    for (const request of requests) expect(request.rows).toHaveLength(1)
  })

  it('splits a group past twenty symbols, the most one request takes', () => {
    const many = Array.from({ length: 21 }, (_, i) => planned(`SYM${String(i)}`))
    const sizes = collectionRequests(many).map((request) => request.items.length)
    expect(sizes).toEqual([20, 1])
  })

  it('has nothing to send when nothing is missing', () => {
    expect(collectionRequests([])).toEqual([])
  })
})

describe('collectionRequests, with the catalogue and what was already queued', () => {
  it('sends the class the catalogue holds, and nothing for a symbol it does not know', () => {
    const classes = catalogueClasses([
      { symbol: 'XAUUSD', asset_class: 'future' },
      { symbol: 'ODD', asset_class: 'not-a-class' },
    ] as never)
    expect(collectionRequests([planned('XAUUSD'), planned('ODD'), planned('NEW')], classes)).toEqual([
      {
        items: [{ symbol: 'XAUUSD', asset_class: 'future' }, { symbol: 'ODD' }, { symbol: 'NEW' }],
        rows: [{ timeframe: 'H1', ...Y2019 }],
      },
    ])
  })

  it('leaves out every symbol and window already queued, and only those', () => {
    const first = collectionRequests([planned('EURUSD', [Y2019, Y2026]), planned('GBPUSD')])
    const queued = new Set(queuedKeys(first[0]!))
    // The 2019 request carried both symbols; only EURUSD 2026 is left.
    expect(collectionRequests([planned('EURUSD', [Y2019, Y2026]), planned('GBPUSD')], new Map(), queued)).toEqual([
      { items: [{ symbol: 'EURUSD' }], rows: [{ timeframe: 'H1', ...Y2026 }] },
    ])
  })

  it('does not take a window queued at another chart for this one', () => {
    const queued = new Set(queuedKeys({ items: [{ symbol: 'EURUSD' }], rows: [{ timeframe: 'H4', ...Y2019 }] }))
    expect(collectionRequests([planned('EURUSD')], new Map(), queued)).toHaveLength(1)
  })
})

describe('missingLine', () => {
  it('says a market was never collected and which years would be fetched', () => {
    expect(missingLine(planned('GBPUSD'))).toBe('GBPUSD H1 — never collected; would fetch 2019–2020')
  })

  it('says what the disk holds when it holds something', () => {
    expect(
      missingLine(planned('EURUSD', [Y2019, Y2026], { covers: '2021-01-04 to 2025-12-31' })),
    ).toBe('EURUSD H1 — on disk 2021-01-04 to 2025-12-31; would fetch 2019–2020, 2026')
  })
})
