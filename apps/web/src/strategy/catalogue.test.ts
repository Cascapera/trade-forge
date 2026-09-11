import type { StrategyListItem } from '../api/types'
import { filterCatalogue, setupsIn } from './catalogue'

function row(partial: Partial<StrategyListItem> & { name: string }): StrategyListItem {
  return {
    id: partial.name,
    version: 1,
    schema_version: '1.0.0',
    setup: null,
    runs: 0,
    created_at: '2026-09-01T00:00:00Z',
    ...partial,
  }
}

// ⚠️ The names here deliberately do **not** contain their own setup. A fixture called
// `MME9 turn` running `mme9_turn` agrees with every wrong implementation in this file: matching
// the name only, matching the setup only, and matching either would all pass it.
const SHELF: StrategyListItem[] = [
  row({ name: 'Structure — CHoCH 56454', setup: 'mme9_breakout' }),
  row({ name: '9.1 sem filtro', setup: 'mme9_turn' }),
  row({ name: '9.1 com filtro', setup: 'mme9_turn' }),
  row({ name: 'Ponto Contínuo', setup: 'continuous_point' }),
  row({ name: 'Hand-built document', setup: null }),
]

describe('setupsIn', () => {
  it('lists each setup once, sorted', () => {
    expect(setupsIn(SHELF)).toEqual(['continuous_point', 'mme9_breakout', 'mme9_turn'])
  })

  it('leaves out the document with no named setup', () => {
    // Not the same assertion as the one above dressed differently: an implementation that
    // pushed `null` through would produce a list with a hole in it, and the length is what
    // catches that regardless of where sorting puts it.
    expect(setupsIn(SHELF)).toHaveLength(3)
    expect(setupsIn([row({ name: 'only a DSL doc' })])).toEqual([])
  })
})

describe('filterCatalogue', () => {
  it('returns everything when nothing is asked', () => {
    expect(filterCatalogue(SHELF, '', '')).toHaveLength(5)
  })

  it('matches a name no setup contains', () => {
    const found = filterCatalogue(SHELF, 'sem filtro', '')
    expect(found.map((item) => item.name)).toEqual(['9.1 sem filtro'])
  })

  it('matches a setup no name contains', () => {
    // `mme9_breakout` appears in no name on this shelf, so a filter that only ever read the name
    // returns nothing here. That is the mutant this case exists to kill.
    const found = filterCatalogue(SHELF, 'mme9_breakout', '')
    expect(found.map((item) => item.name)).toEqual(['Structure — CHoCH 56454'])
  })

  it('ignores case and surrounding space', () => {
    expect(filterCatalogue(SHELF, '  MME9_TURN ', '')).toHaveLength(2)
  })

  it('narrows by setup alone', () => {
    const found = filterCatalogue(SHELF, '', 'mme9_turn')
    expect(found.map((item) => item.name)).toEqual(['9.1 sem filtro', '9.1 com filtro'])
  })

  it('requires both when both are given', () => {
    // `9.1` matches two rows and `continuous_point` matches a third, and none of them is the
    // same row — so an implementation that ORed the two filters returns three here instead of
    // none. An AND that happened to be an OR is invisible on any fixture where the two agree.
    expect(filterCatalogue(SHELF, '9.1', 'continuous_point')).toEqual([])
    expect(filterCatalogue(SHELF, 'com filtro', 'mme9_turn')).toHaveLength(1)
  })

  it('leaves the document with no setup out of a setup filter without throwing', () => {
    const found = filterCatalogue(SHELF, 'document', 'mme9_turn')
    expect(found).toEqual([])
  })
})
