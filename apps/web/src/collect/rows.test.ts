import type { SymbolHistory } from '../api/types'

import {
  blockedReason,
  MAX_COLLECTIONS,
  newRow,
  nextFreeTimeframe,
  totalCollections,
  totalSlices,
  TIMEFRAMES,
  withSuggestedWindows,
} from './rows'
import type { DraftRow } from './rows'

const NOW = new Date('2026-08-20T00:00:00Z')

function measured(usable_from: string | null): SymbolHistory {
  return {
    symbol: 'EURUSD',
    timeframe: 'H1',
    oldest: '2000-01-01T00:00:00Z',
    bar_count: 1000,
    terminal_maxbars: 100000,
    bar_count_is_a_ceiling: false,
    last_fabricated: null,
    first_measured_cost: null,
    probed_at: '2026-08-21T00:00:00Z',
    capped_by_terminal: false,
    usable_from,
  }
}

function row(patch: Partial<DraftRow> = {}): DraftRow {
  // `touched: true` by default: these tests are about rows a person has already settled,
  // and an untouched one is a suggestion that `withSuggestedWindows` is free to move.
  return { id: 'r1', timeframe: 'H1', from: '2020-01-01', to: '2024-12-31', touched: true, ...patch }
}

describe('newRow', () => {
  it('opens each timeframe on the window its bar budget deserves', () => {
    /**
     * ⚠️ The whole reason rows exist. A year of M1 and seventeen of H1 are the same number of
     * bars; a row that inherited the previous row's dates would ask for seventeen years of M1,
     * which this broker's terminal will not serve.
     */
    const m1 = newRow('M1', undefined, NOW, 'a')
    const h1 = newRow('H1', undefined, NOW, 'b')

    expect(Number(h1.from.slice(0, 4))).toBeLessThan(Number(m1.from.slice(0, 4)))
  })

  it('never opens on years the measurement calls filler', () => {
    const withFloor = newRow('H1', measured('2009-01-01T00:00:00Z'), NOW, 'a')

    expect(withFloor.from).toBe('2009-01-01')
  })

  it('carries the id it was given, so a removal cannot collide keys', () => {
    expect(newRow('H1', undefined, NOW, 'row-7').id).toBe('row-7')
  })
})

describe('nextFreeTimeframe', () => {
  it('offers the first timeframe not already on a row', () => {
    expect(nextFreeTimeframe(['M1'])).toBe('M5')
  })

  it('skips over everything taken, in order', () => {
    expect(nextFreeTimeframe(['M1', 'M5', 'M15'])).toBe('M30')
  })

  it('offers nothing once every timeframe is taken', () => {
    /**
     * ⚠️ The API refuses a repeated timeframe, because two collections of the same series
     * overwrite each other's year partitions — and the symptom is a *missing* year, not a
     * duplicate. A screen that offered a duplicate and then relayed the refusal would be
     * setting a trap it already knows about.
     */
    expect(nextFreeTimeframe([...TIMEFRAMES])).toBeUndefined()
  })

  it('nothing taken means the first one', () => {
    expect(nextFreeTimeframe([])).toBe('M1')
  })
})

describe('how much work', () => {
  it('collections are symbols times rows', () => {
    expect(totalCollections(3, [row(), row({ id: 'r2', timeframe: 'M5' })])).toBe(6)
  })

  it('no rows is no work, however many symbols', () => {
    expect(totalCollections(20, [])).toBe(0)
  })

  it('slices sum across rows, because each row has its own window', () => {
    // Two symbols: 5 calendar years of H1 plus 1 of M1 = 2 × (5 + 1).
    const slices = totalSlices(2, [
      row({ from: '2020-01-01', to: '2024-12-31' }),
      row({ id: 'r2', timeframe: 'M1', from: '2026-01-01', to: '2026-08-20' }),
    ])

    expect(slices).toBe(12)
  })

  it('slices and collections are different numbers, and that is the point', () => {
    /**
     * ⚠️ Forty collections of H1 and forty of M1 are the same count and nothing like the same
     * wait. The count is the guardrail; the slices are the information.
     */
    const oneYear = [row({ from: '2026-01-01', to: '2026-08-20' })]
    const seventeen = [row({ from: '2009-01-01', to: '2026-08-20' })]

    expect(totalCollections(2, oneYear)).toBe(totalCollections(2, seventeen))
    expect(totalSlices(2, oneYear)).toBeLessThan(totalSlices(2, seventeen))
  })
})

describe('blockedReason', () => {
  const ok = { symbols: 2, rows: [row()], unanswered: [] }

  it('a complete batch is not blocked', () => {
    expect(blockedReason(ok)).toBeNull()
  })

  it('no symbols is named first', () => {
    expect(blockedReason({ ...ok, symbols: 0 })).toMatch(/at least one symbol/i)
  })

  it('no rows is named', () => {
    expect(blockedReason({ ...ok, rows: [] })).toMatch(/at least one timeframe/i)
  })

  it('an unfinished window names its own timeframe', () => {
    // ⚠️ With several rows on screen, "set both ends" would leave the reader hunting. The
    // message says which row.
    const message = blockedReason({ ...ok, rows: [row({ timeframe: 'M15', from: '' })] })

    expect(message).toMatch(/M15/)
  })

  it('a backwards window names its own timeframe', () => {
    const message = blockedReason({
      ...ok,
      rows: [row({ timeframe: 'H4', from: '2024-01-01', to: '2020-01-01' })],
    })

    expect(message).toMatch(/H4.*backwards/i)
  })

  it('the collections ceiling is refused before the request is sent', () => {
    const rows = ['M1', 'M5', 'M15', 'H1'].map((timeframe, i) => row({ id: `r${String(i)}`, timeframe }))

    expect(blockedReason({ symbols: 11, rows, unanswered: [] })).toMatch(/44 collections/)
  })

  it('exactly at the ceiling is allowed', () => {
    /**
     * ⚠️ Tested on both sides. Forty-four refused above and forty allowed here — a single test
     * at the limit cannot tell `>` from `>=`, and this ceiling is one somebody will sit on.
     */
    const rows = ['M1', 'M5', 'M15', 'H1'].map((timeframe, i) => row({ id: `r${String(i)}`, timeframe }))

    expect(totalCollections(10, rows)).toBe(MAX_COLLECTIONS)
    expect(blockedReason({ symbols: 10, rows, unanswered: [] })).toBeNull()
  })

  it('an unanswered asset class blocks last, after the shape is right', () => {
    // Ordering matters: telling somebody to classify XAUUSD while the window is empty makes
    // them fix the wrong thing first.
    expect(blockedReason({ ...ok, unanswered: ['XAUUSD'] })).toMatch(/Say what XAUUSD is/)
  })

  it('the window complaint wins over the class one', () => {
    const message = blockedReason({
      ...ok,
      rows: [row({ timeframe: 'M5', from: '' })],
      unanswered: ['XAUUSD'],
    })

    expect(message).toMatch(/M5/)
  })
})

describe('withSuggestedWindows', () => {
  it('an untouched row follows the floor when a measurement arrives', () => {
    /**
     * ⚠️ The regression this exists for. The opening row is created before any symbol is
     * chosen, so its first window comes from the bar budget alone and reaches back to 1966. The
     * floor arrives seconds later, and the row has to follow it — otherwise the form offers
     * decades the probe has already called filler.
     */
    const opened = newRow('H1', undefined, NOW, 'r0')
    expect(opened.from).not.toBe('2009-01-01')

    const [followed] = withSuggestedWindows([opened], measured('2009-01-01T00:00:00Z'), NOW)

    expect(followed?.from).toBe('2009-01-01')
  })

  it('a touched row is left exactly as it was left', () => {
    // Re-deriving here would snap the field back under the cursor of the very person a
    // measurement is arriving for.
    const edited = row({ from: '1999-01-01', touched: true })

    const [kept] = withSuggestedWindows([edited], measured('2009-01-01T00:00:00Z'), NOW)

    expect(kept?.from).toBe('1999-01-01')
  })

  it('each row follows its own timeframe budget, not a shared one', () => {
    const rows = [newRow('M1', undefined, NOW, 'a'), newRow('H1', undefined, NOW, 'b')]

    const followed = withSuggestedWindows(rows, undefined, NOW)

    expect(followed[0]?.from).not.toBe(followed[1]?.from)
  })

  it('ids survive, so React keys do not shuffle under a re-render', () => {
    const followed = withSuggestedWindows([newRow('H1', undefined, NOW, 'keep-me')], undefined, NOW)

    expect(followed[0]?.id).toBe('keep-me')
  })
})
