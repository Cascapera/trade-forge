import type { BestMapCell } from '../api/types'
import { fillFor } from '../backtest/study'

import { fillOf, formatValue, layoutOf, neutralOf } from './map'

function cell(over: Partial<BestMapCell>): BestMapCell {
  return {
    market: 'Forex',
    symbol: 'EURUSD',
    entry_id: 'e1',
    entry_name: 'MM9',
    timeframe: 'H1',
    value: '2',
    unbounded: false,
    run_id: 'r1',
    sweep_id: 's1',
    ranked: 10,
    ...over,
  }
}

describe('layoutOf', () => {
  const cells = [
    cell({ symbol: 'GOLD', market: 'Metals', timeframe: 'H4', value: '-3' }),
    cell({ symbol: 'EURUSD', timeframe: 'H4' }),
    cell({ symbol: 'EURUSD', timeframe: 'M5', value: '1' }),
    cell({ symbol: 'AUDUSD', entry_id: 'e2', entry_name: 'CHOCH', value: '4' }),
  ]

  it('groups rows by market, then symbol, and orders columns by setup then chart', () => {
    const layout = layoutOf(cells, 'net_r')

    expect(layout.markets).toEqual([
      { market: 'Forex', symbols: ['AUDUSD', 'EURUSD'] },
      { market: 'Metals', symbols: ['GOLD'] },
    ])
    expect(layout.columns.map((one) => `${one.entryName} ${one.timeframe}`)).toEqual([
      'CHOCH H1',
      'MM9 M5',
      'MM9 H4',
    ])
    const [first, , last] = layout.columns
    expect(last && layout.at('GOLD', last)?.value).toBe('-3')
    expect(first && layout.at('GOLD', first)).toBeUndefined()
  })

  it('measures the colour scale from the drawn cells only', () => {
    expect(layoutOf(cells, 'net_r').extent).toBe(4)
    expect(
      layoutOf(cells, 'net_r', { markets: ['Metals'], entries: [], timeframes: [] }).extent,
    ).toBe(3)
  })

  it('filters by market, setup and chart, empty meaning every one', () => {
    const layout = layoutOf(cells, 'net_r', { markets: [], entries: ['e1'], timeframes: ['H4'] })

    expect(layout.markets.flatMap((one) => one.symbols)).toEqual(['EURUSD', 'GOLD'])
    expect(layout.columns).toHaveLength(1)
  })

  it('names a removed entry by its id rather than leaving the heading blank', () => {
    const layout = layoutOf([cell({ entry_name: null, entry_id: 'abcdef1234' })], 'net_r')

    expect(layout.columns[0]?.entryName).toBe('removed entry abcdef12')
  })
})

describe('the colour and the number', () => {
  it('is neutral at half the years for the share of positive years, at zero for R', () => {
    expect(neutralOf('positive_years')).toBe(0.5)
    expect(neutralOf('net_r')).toBe(0)
    // 05/10: a profit factor of one is break-even; half the trades won is the coin.
    expect(neutralOf('profit_factor')).toBe(1)
    expect(neutralOf('win_rate')).toBe(0.5)
    expect(neutralOf('return_pct')).toBe(0)
    expect(fillOf(cell({ value: '0.5' }), 'positive_years', 0.5)).toBe(fillFor(0, 0.5))
    expect(fillOf(cell({ value: '0' }), 'net_r', 3)).toBe(fillFor(0, 3))
  })

  it('paints an unbounded drawdown ratio as the strongest gain and writes it as infinity', () => {
    const unbounded = cell({ value: null, unbounded: true })

    expect(fillOf(unbounded, 'recovery_r', 5)).toBe(fillFor(5, 5))
    expect(formatValue(null, 'recovery_r', true)).toBe('∞')
  })

  it('writes each measure in its own unit', () => {
    expect(formatValue('2.5', 'net_r')).toBe('+2.50 R')
    expect(formatValue('-1.25', 'net_r_per_year')).toBe('-1.25 R')
    expect(formatValue('3.14', 'recovery_r')).toBe('3.1')
    expect(formatValue('0.75', 'positive_years')).toBe('75%')
    expect(formatValue(null, 'net_r')).toBe('—')
    expect(formatValue('0.253', 'return_pct')).toBe('25.3%')
    expect(formatValue('0.061', 'cagr')).toBe('6.1%')
    expect(formatValue('0.45', 'win_rate')).toBe('45%')
    expect(formatValue('1.567', 'profit_factor')).toBe('1.57')
    expect(formatValue('1.23', 'sharpe')).toBe('1.2')
    expect(formatValue('-3.5', 'worst_year_r')).toBe('-3.50 R')
  })
})
