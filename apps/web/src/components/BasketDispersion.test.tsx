import { screen } from '@testing-library/react'

import type { BasketAggregate } from '../api/types'
import { renderWithProviders } from '../test-utils'
import { BasketDispersion } from './BasketDispersion'

function aggregate(over: Partial<BasketAggregate>): BasketAggregate {
  return {
    runs_total: 4,
    runs_finished: 4,
    runs_failed: 0,
    runs_profitable: 2,
    best_symbol: 'AAPL',
    best_return: '0.30',
    worst_symbol: 'EURUSD',
    worst_return: '-0.25',
    median_return: '-0.01',
    ...over,
  }
}

describe('BasketDispersion', () => {
  it('names the market at each extreme, not just the numbers', () => {
    // "worst: −25%" sends the reader looking; "worst: EURUSD −25%" tells them where to look. The
    // market that breaks a strategy is the thing a basket exists to surface.
    renderWithProviders(<BasketDispersion aggregate={aggregate({})} />)

    expect(screen.getByText('30.0%')).toBeInTheDocument()
    expect(screen.getByText('AAPL')).toBeInTheDocument()
    expect(screen.getByText('-25.0%')).toBeInTheDocument()
    expect(screen.getByText('EURUSD')).toBeInTheDocument()
  })

  it('reports the median, which is the number a mean would have hidden', () => {
    // These four are the case the choice was made for: three flat losses and one huge winner.
    // The mean is +18.75% and reads as a strong strategy; the median is -1% and is the truth
    // about the typical market.
    renderWithProviders(
      <BasketDispersion aggregate={aggregate({ median_return: '-0.01', best_return: '0.78' })} />,
    )

    expect(screen.getByText('-1.0%')).toBeInTheDocument()
    expect(screen.getByText(/typical market, not the average/i)).toBeInTheDocument()
  })

  it('shows a dash rather than 0% while nothing has finished', () => {
    // ⚠️ Undefined is not zero. Rendered as 0%, a basket whose runs are still queued would rank
    // alongside one that broke even in every market — and it would look like a finished result.
    renderWithProviders(
      <BasketDispersion
        aggregate={aggregate({
          runs_finished: 0,
          runs_profitable: 0,
          best_symbol: null,
          best_return: null,
          worst_symbol: null,
          worst_return: null,
          median_return: null,
        })}
      />,
    )

    expect(screen.getAllByText('—')).toHaveLength(3)
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
    expect(screen.getByText('4 still running')).toBeInTheDocument()
  })

  it('counts the failed runs apart from the finished ones', () => {
    // A run that crashed is not a market where the strategy lost money.
    renderWithProviders(
      <BasketDispersion
        aggregate={aggregate({ runs_total: 4, runs_finished: 3, runs_failed: 1, runs_profitable: 2 })}
      />,
    )

    expect(screen.getByText('2 / 3')).toBeInTheDocument()
    expect(screen.getByText('1 failed')).toBeInTheDocument()
  })

  it('says on the screen that the returns are not additive', () => {
    // A reader looking at four curves on one chart will reach for the sum unless told why there
    // is not one. Four runs of $10 000 are neither a $10 000 account nor a $40 000 one.
    renderWithProviders(<BasketDispersion aggregate={aggregate({})} />)

    expect(screen.getByText(/never additive/i)).toBeInTheDocument()
    expect(screen.getByText(/spread of outcomes, not a portfolio/i)).toBeInTheDocument()
  })
})
