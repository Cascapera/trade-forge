import { count, duration, money, percent, ratio, sign, signedMoney } from './format'

describe('format', () => {
  it('renders money with two decimals and thousands separators', () => {
    expect(money('12345.6')).toBe('12,345.60')
    expect(money('0')).toBe('0.00')
  })

  it('groups a count with a pinned locale', () => {
    // Pinned, not the machine's: an unpinned locale renders 3.480 or 3480 depending on the
    // runner, which is a test that passes here and fails in CI.
    expect(count(3480)).toBe('3,480')
    expect(count(0)).toBe('0')
    expect(count(7)).toBe('7')
  })

  it('signs money with an explicit + or minus', () => {
    expect(signedMoney('200')).toBe('+200.00')
    expect(signedMoney('-100')).toBe('−100.00')
    expect(signedMoney('0')).toBe('+0.00')
  })

  it('renders a fraction as a percent, or an em dash when undefined', () => {
    expect(percent('0.5')).toBe('50.0%')
    expect(percent('0.0381', 2)).toBe('3.81%')
    expect(percent(null)).toBe('—')
  })

  it('renders a ratio to two decimals, or an em dash when undefined', () => {
    expect(ratio('2')).toBe('2.00')
    expect(ratio(null)).toBe('—')
  })

  it('reads the sign for the status colour', () => {
    expect(sign('10')).toBe('up')
    expect(sign('-10')).toBe('down')
    expect(sign('0')).toBe('flat')
  })
  it('reads a year out of an ISO duration instead of losing it', () => {
    // ⚠️ **The trap, and it is silent.** Pydantic serialises a `timedelta` into the largest
    // units it can, so 400 days arrives as `P1Y35DT2H`. A parser reading only `D` renders that
    // as `35d 2h` — wrong by a factor of eleven, and nothing on screen looks odd. Measured
    // against the serialiser: a year there is exactly 365 days.
    expect(duration('P1Y35DT2H')).toBe('400d 2h')
    expect(duration('P2Y270DT2H')).toBe('1000d 2h')
    expect(duration('P1YT2H')).toBe('365d 2h')
  })

  it('shows two units and stops, largest first', () => {
    // A third unit is precision nobody acts on, and it pushes the number people came for off
    // the end of a tile.
    expect(duration('P1DT2H30M')).toBe('1d 2h')
    expect(duration('PT3H20M')).toBe('3h 20m')
    expect(duration('PT1H30M')).toBe('1h 30m')
    expect(duration('PT45S')).toBe('45s')
  })

  it('drops a trailing unit that is zero rather than writing it', () => {
    expect(duration('P7D')).toBe('7d')
    expect(duration('PT2H')).toBe('2h')
    // And it does not skip a zero in the *middle*: two days and ten minutes is two days, and
    // saying `2d 10m` would put a minute count next to a day count as if they were adjacent.
    expect(duration('P2DT10M')).toBe('2d')
  })

  it('says zero for a duration that is really zero', () => {
    // A trade opened and closed on one bar. A measurement, not an absence — and the difference
    // is why this is not the same answer as the em dash below.
    expect(duration('PT0S')).toBe('0s')
    // ⚠️ Both sides of the halfway mark. `PT0.4S` alone cannot tell flooring from rounding —
    // they agree there — and a rounded `PT0.6S` would report a second that did not elapse.
    expect(duration('PT0.4S')).toBe('0s')
    expect(duration('PT0.6S')).toBe('0s')
  })

  it('answers an em dash for nothing and for anything it cannot read', () => {
    // ⚠️ Never `0s`. A string nobody could parse is not a duration of zero, and rendering it as
    // one would put a confident number where there is no measurement at all.
    expect(duration(null)).toBe('—')
    expect(duration('')).toBe('—')
    expect(duration('P')).toBe('—')
    expect(duration('2 days')).toBe('—')
    expect(duration('PT1H30')).toBe('—')
  })
})
