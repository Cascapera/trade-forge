import { render, screen } from '@testing-library/react'

import type { Metrics } from '../api/types'
import { MetricCards } from './MetricCards'

const metrics: Metrics = {
  net_profit: '100',
  gross_profit: '200',
  gross_loss: '-100',
  // ⚠️ Different on purpose. They were `1` and `1`, and a tile reading the wrong one of the two
  // passed every test in this file — the failure `campos que costumam coincidir` names.
  total_trades: 4,
  long_trades: 3,
  short_trades: 1,
  win_rate: '0.5',
  payoff: '2',
  profit_factor: '2',
  expectancy: '50',
  max_drawdown_abs: '100',
  max_drawdown_pct: '0.0098',
  max_dd_duration_days: 0,
  sharpe: '0.5',
  sortino: '0.7',
  cagr: null,
  avg_trade_duration: null,
}

/**
 * What one tile says, found by its own label.
 *
 * ⚠️ Written the day the second group landed, and the reason is the failure it replaces: the
 * expectancy test looked for `—` anywhere on the screen, and with nine more tiles — three of them
 * nullable — the page now holds several. A search that finds any dash proves a dash exists, not
 * that *this* number is undefined.
 */
function tile(label: string): string {
  const value = screen.getByText(label).nextElementSibling
  if (value === null) throw new Error(`the tile ${label} has no value beside its label`)
  return value.textContent
}

describe('MetricCards', () => {
  it('renders the headline numbers', () => {
    render(<MetricCards metrics={metrics} />)
    expect(screen.getByText('+100.00')).toBeInTheDocument()
    expect(screen.getByText('50.0%')).toBeInTheDocument()
    // Profit factor and payoff both read 2.00.
    expect(screen.getAllByText('2.00')).toHaveLength(2)
    expect(screen.getByText('1.0%')).toBeInTheDocument() // drawdown, one-decimal display
    expect(screen.getByText('0.50')).toBeInTheDocument() // sharpe
  })

  it('shows an em dash for an undefined expectancy, and only there', () => {
    render(<MetricCards metrics={{ ...metrics, expectancy: null }} />)

    expect(tile('Expectancy')).toBe('—')
    // The mirror: a tile that *has* a number must not read as undefined, or a component that
    // dashed everything would satisfy the line above.
    expect(tile('Net profit')).toBe('+100.00')
  })

  it('shows the two sides of the net, which the net alone cannot tell apart', () => {
    // ⚠️ +100 is a different result at 200 against −100 than at 10,100 against −10,000, and
    // nothing in the headline row separates those. Both were computed and thrown away.
    render(<MetricCards metrics={metrics} />)

    // Unsigned: each carries its sign structurally, and `signedMoney` printed `+0.00` on a run
    // with no losing trades — a plus on the one number the engine documents as `<= 0`.
    expect(tile('Gross profit')).toBe('200.00')
    expect(tile('Gross loss')).toBe('-100.00')
    expect(tile('Long trades')).toBe('3')
    expect(tile('Short trades')).toBe('1')
  })

  it('does not put a plus on a gross loss of zero', () => {
    // A run whose every trade won. `gross_loss` is the sum of an empty list, so it is exactly
    // zero — and a leading `+` there contradicts the one field the engine documents as `<= 0`.
    render(<MetricCards metrics={{ ...metrics, gross_loss: '0' }} />)

    expect(tile('Gross loss')).toBe('0.00')
  })

  it('tells the two risk ratios apart', () => {
    // ⚠️ Neighbouring tiles of the same shape, and the Sortino had no assertion at all: a tile
    // reading `sharpe` twice passed this file. They differ in the fixture so the swap shows.
    render(<MetricCards metrics={metrics} />)

    expect(tile('Sharpe')).toBe('0.50')
    expect(tile('Sortino')).toBe('0.70')
  })

  it('reads a CAGR as a percentage, not as the bare fraction', () => {
    // ⚠️ The only field on this screen where fraction against percentage is a real question, and
    // it was exercised only as `null` — where `percent` and `ratio` both answer `—` and separate
    // nothing.
    render(<MetricCards metrics={{ ...metrics, cagr: '0.123' }} />)

    expect(tile('CAGR')).toBe('12.3%')
  })

  it('shows the drawdown in cash beside the one in percent, and how long it lasted', () => {
    render(
      <MetricCards
        metrics={{ ...metrics, max_drawdown_abs: '250.5', max_dd_duration_days: 40 }}
      />,
    )

    // Unsigned: `max_drawdown_abs` is a fall, already a magnitude, so a minus would be the
    // second one on the same number.
    expect(tile('Drawdown in cash')).toBe('250.50')
    expect(tile('Longest underwater')).toBe('40 d')
  })

  it('says less than a day rather than none, when a drawdown was shorter than the unit', () => {
    // ⚠️ The API sends whole days, truncated, so every intraday run arrives as `0` — and a bare
    // `0 d` beside a real drawdown reads as "never underwater", which is a different claim.
    render(
      <MetricCards
        metrics={{ ...metrics, max_dd_duration_days: 0, max_drawdown_abs: '250.5' }}
      />,
    )

    expect(tile('Longest underwater')).toBe('< 1 d')
  })

  it('says none when there was no drawdown at all', () => {
    // The other side, and it is why the tile above cannot simply always say "less than a day":
    // a run that never fell has a zero that means zero.
    render(
      <MetricCards metrics={{ ...metrics, max_dd_duration_days: 0, max_drawdown_abs: '0' }} />,
    )

    expect(tile('Longest underwater')).toBe('0 d')
  })

  it('renders an average trade duration as time rather than as the wire format', () => {
    render(<MetricCards metrics={{ ...metrics, avg_trade_duration: 'P1Y35DT2H' }} />)

    expect(tile('Average trade')).toBe('400d 2h')
  })

  it('says why a CAGR can be absent, since a dash on its own reads as missing', () => {
    // A refusal, not a gap: annualising four months produces an enormous number that means
    // nothing. Left unexplained, the reader goes looking for a broken pipeline.
    render(<MetricCards metrics={metrics} />)

    expect(tile('CAGR')).toBe('—')
    // Both conditions: the engine also declines to annualise a run that ended at or below zero,
    // and a note naming only the span prints the wrong reason on exactly that result.
    expect(screen.getByText(/at least a year of history/)).toBeInTheDocument()
    expect(screen.getByText(/equity still above zero/)).toBeInTheDocument()
  })
})
