import { screen, within } from '@testing-library/react'

import type { TargetRung } from '../api/types'
import { renderWithProviders } from '../test-utils'
import { RunTargets, SweepTargets } from './TargetLadder'

describe('RunTargets', () => {
  it('shows each rung in R, net, with its hits', () => {
    renderWithProviders(
      <RunTargets
        targets={{
          '2': {
            trades: 40,
            hits: 17,
            net_r: '3.4',
            expectancy_r: '0.085',
            max_drawdown_r: '4.5',
          },
          '3': null,
        }}
      />,
    )
    const table = screen.getByRole('table', { name: 'Result by target' })
    const two = within(table).getByRole('row', { name: /2 R/ })
    expect(two).toHaveTextContent('17 of 40')
    expect(two).toHaveTextContent('+3.40 R')
    expect(two).toHaveTextContent('+0.09 R')
    expect(two).toHaveTextContent('4.50 R')
  })

  it('says why a rung was not scored instead of printing zeros', () => {
    renderWithProviders(<RunTargets targets={{ '3': null }} />)
    expect(screen.getByRole('row', { name: /3 R/ })).toHaveTextContent(
      'Not scored — a trade here had a target of its own below this one',
    )
  })
})

function rung(over: Partial<TargetRung>): TargetRung {
  return {
    rung: '2',
    runs_scored: 0,
    runs_positive: 0,
    median_expectancy_r: null,
    best_label: null,
    best_net_r: null,
    ...over,
  }
}

describe('SweepTargets', () => {
  it('leads with the median and names the best beside it', () => {
    renderWithProviders(
      <SweepTargets
        rungs={[
          rung({
            runs_scored: 3,
            runs_positive: 2,
            median_expectancy_r: '0.1',
            best_label: 'EURUSD · M15 · period=21',
            best_net_r: '3',
          }),
        ]}
      />,
    )
    const row = screen.getByRole('row', { name: /2 R/ })
    expect(row).toHaveTextContent('2 of 3')
    expect(row).toHaveTextContent('+0.10 R')
    expect(row).toHaveTextContent('EURUSD · M15 · period=21 · +3.00 R')
  })

  it('shows nothing at all for a sweep recorded before the ladder existed', () => {
    const { container } = renderWithProviders(<SweepTargets rungs={[rung({}), rung({ rung: '3' })]} />)
    expect(container).toBeEmptyDOMElement()
  })
})
