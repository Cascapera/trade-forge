import { SETUP_TYPES } from '@tradeforge/schema'

import { RUN_ABBREV, runName, stamp } from './naming'

// Instants are built from *local* components on purpose. `new Date(2026, 7, 7, 15, 12, 30)` is
// quarter past three in the afternoon wherever this test runs, so the expectations below hold on
// a machine in São Paulo and on a CI runner in UTC alike. An ISO string would have pinned the
// instant and let the rendered digits drift with the runner's zone.
const AFTERNOON = new Date(2026, 7, 7, 15, 12, 30)

describe('stamp', () => {
  it('renders the instant as sortable fixed-width fields', () => {
    expect(stamp(AFTERNOON)).toBe('20260807-151230')
  })

  it('pads every field that can be short', () => {
    // 1 February, four minutes and five seconds past nine in the morning. Unpadded this would
    // read `202621-945`, which is neither sortable nor parseable by eye.
    expect(stamp(new Date(2026, 1, 1, 9, 4, 5))).toBe('20260201-090405')
  })

  it('reads the local wall clock, not UTC', () => {
    // The label says the hour the author lived through. Building from local components and
    // reading local components has to round-trip, whatever the runner's offset.
    const at = new Date(2026, 11, 31, 23, 59, 59)
    expect(stamp(at)).toBe('20261231-235959')
  })

  it('sorts chronologically as plain text', () => {
    const instants = [
      new Date(2026, 7, 7, 15, 12, 30),
      new Date(2026, 7, 7, 9, 0, 0),
      new Date(2025, 11, 31, 23, 59, 59),
    ]
    const sorted = instants.map(stamp).sort()

    expect(sorted).toEqual(['20251231-235959', '20260807-090000', '20260807-151230'])
  })
})

describe('runName', () => {
  it('names a run by its abbreviation and the instant it was picked', () => {
    expect(runName('structure_choch', AFTERNOON)).toBe('SCHOCH-20260807-151230')
    expect(runName('ponto_continuo', AFTERNOON)).toBe('PCONT-20260807-151230')
    expect(runName('ma_cross', AFTERNOON)).toBe('MACROSS-20260807-151230')
  })

  it('gives two strategies picked at the same instant different names', () => {
    // The abbreviation is what separates them when the clock cannot.
    expect(runName('structure_choch', AFTERNOON)).not.toBe(
      runName('structure_continuation', AFTERNOON),
    )
  })

  it('gives the same strategy picked twice different names', () => {
    // And the clock is what separates them when the abbreviation cannot. This is the pair that
    // used to be a 409: the same obvious name, reached for again the next day.
    const tomorrow = new Date(2026, 7, 8, 15, 12, 30)
    expect(runName('structure_choch', AFTERNOON)).not.toBe(runName('structure_choch', tomorrow))
  })

  it('separates two picks one second apart', () => {
    const later = new Date(2026, 7, 7, 15, 12, 31)
    expect(runName('structure_choch', later)).toBe('SCHOCH-20260807-151231')
    expect(runName('structure_choch', later)).not.toBe(runName('structure_choch', AFTERNOON))
  })

  it('derives a name rather than crashing on a strategy it has not been told about', () => {
    // A setup added in Python before an abbreviation is chosen here still gets a usable name. The
    // test below is what stops that from being the normal case.
    expect(runName('order_block_sweep', AFTERNOON)).toBe('ORDERBLO-20260807-151230')
  })

  it('never produces a name that is only a timestamp', () => {
    // An id with nothing alphanumeric in it would otherwise yield `-20260807-151230`, which reads
    // as a missing field rather than a name.
    expect(runName('___', AFTERNOON)).toBe('RUN-20260807-151230')
  })
})

describe('the abbreviation map against the schema', () => {
  it('has an explicit abbreviation for every setup the schema declares', () => {
    // The loud half of the fallback above. Adding a setup in Python and forgetting to name it here
    // fails this test with the type spelled out, instead of quietly shipping a run called
    // STRUCTUR-… that nobody can tell from its sibling.
    const missing = SETUP_TYPES.filter((type) => RUN_ABBREV[type] === undefined)
    expect(missing).toEqual([])
  })

  it('gives every strategy its own abbreviation', () => {
    // Two strategies sharing one abbreviation would collide whenever they were picked in the same
    // second, and would read identically in the run log for ever after.
    const abbreviations = Object.values(RUN_ABBREV)
    expect(new Set(abbreviations).size).toBe(abbreviations.length)
  })
})
