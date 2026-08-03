import { count, money, percent, ratio, sign, signedMoney } from './format'

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
})
