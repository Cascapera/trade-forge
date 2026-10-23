import { filterCatalogue, gridSummary, type Searchable } from './catalogue'

function row(name: string, setup: string | null = null): Searchable {
  return { id: name, name, setup }
}

// ⚠️ The names here deliberately do **not** contain their own setup. A fixture called `MME9 turn`
// running `mme9_turn` agrees with every wrong implementation: matching the name only, matching
// the setup only, and matching either would all pass it.
const SHELF: Searchable[] = [
  row('Structure — CHoCH 56454', 'mme9_breakout'),
  row('9.1 sem filtro', 'mme9_turn'),
  row('9.1 com filtro', 'mme9_turn'),
  row('Ponto Contínuo', 'continuous_point'),
  row('Hand-built document'),
]

describe('filterCatalogue', () => {
  it('returns everything when nothing is asked', () => {
    expect(filterCatalogue(SHELF, '')).toHaveLength(5)
    expect(filterCatalogue(SHELF, '   ')).toHaveLength(5)
  })

  it('matches a name no setup contains', () => {
    expect(filterCatalogue(SHELF, 'sem filtro').map((item) => item.name)).toEqual([
      '9.1 sem filtro',
    ])
  })

  it('matches a setup no name contains', () => {
    // `mme9_breakout` appears in no name on this shelf, so an implementation that only ever
    // read the name returns nothing here. That is the mutant this case exists to kill.
    expect(filterCatalogue(SHELF, 'mme9_breakout').map((item) => item.name)).toEqual([
      'Structure — CHoCH 56454',
    ])
  })

  it('ignores case and surrounding space', () => {
    expect(filterCatalogue(SHELF, '  MME9_TURN ')).toHaveLength(2)
  })

  it('leaves out a row with no setup without throwing on it', () => {
    // The row whose setup is `null` has to survive being *asked about* — an optional chain that
    // was a bare property read would throw here rather than simply not matching.
    expect(filterCatalogue(SHELF, 'mme9')).toHaveLength(3)
    expect(filterCatalogue(SHELF, 'Hand-built').map((item) => item.name)).toEqual([
      'Hand-built document',
    ])
  })
})

describe('gridSummary', () => {
  it('says a sweep in one line, by the last segment of each path', () => {
    // ⚠️ `setup.params.` is on every path, so a caption repeating it is a column of identical
    // prefixes. The full path is still what travels to the server.
    const summary = gridSummary({
      'setup.params.period': [5, 9, 21],
      'setup.params.stop_buffer': [0.1, 0.2],
    })

    expect(summary).toBe('period=5, 9, 21 · stop_buffer=0.1, 0.2')
  })

  it('keeps a path that has no dot in it', () => {
    // `timeframe` is a top-level field of the document, and it is the axis a sweep across
    // several charts will need. Dropping the segment logic on it must not produce an empty name.
    expect(gridSummary({ timeframe: ['M15', 'H1'] })).toBe('timeframe=M15, H1')
  })

  it('is empty for a grid with no axes', () => {
    // The screen reads this as "there is nothing to say here" rather than printing a bare
    // separator, so an entry with no sweep shows no caption at all.
    expect(gridSummary({})).toBe('')
  })
})
