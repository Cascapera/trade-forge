import { entryYears, sumR, windowYears } from './years'

describe('entryYears', () => {
  it('sums each year of entry over every year of exit, earliest first', () => {
    const rows = entryYears({
      '2022': { '2022': '1.5' },
      '2021': { '2022': '-1', '2021': '2.5' },
    })
    expect(rows).toEqual([
      {
        year: 2021,
        r: '1.5',
        exits: [
          { year: 2021, r: '2.5' },
          { year: 2022, r: '-1' },
        ],
        leftLater: true,
      },
      { year: 2022, r: '1.5', exits: [{ year: 2022, r: '1.5' }], leftLater: false },
    ])
  })
})

describe('sumR', () => {
  it('does not print float dust as a result', () => {
    // 0.1 + 0.2 - 0.3 is 5.55e-17 in floats: neither a gain nor a loss of a hundredth.
    expect(sumR(['0.1', '0.2', '-0.3'])).toBe('0')
    expect(sumR(['-0.1', '-0.2', '0.3'])).toBe('0')
    expect(sumR([])).toBe('0')
  })
})

describe('windowYears', () => {
  it('runs from the first year to the one the window ends in, the end exclusive', () => {
    expect(windowYears('2020-01-01T00:00:00Z', '2025-01-01T00:00:00Z')).toEqual([
      2020, 2021, 2022, 2023, 2024,
    ])
  })

  it('keeps a year the window only reaches into', () => {
    expect(windowYears('2020-06-15T00:00:00Z', '2022-03-01T00:00:00Z')).toEqual([2020, 2021, 2022])
  })
})
