import { screen } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { StudyAggregate } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { StudyDispersion } from './StudyDispersion'

function aggregate(over: Partial<StudyAggregate>): StudyAggregate {
  return {
    points_total: 6,
    points_finished: 6,
    points_failed: 0,
    points_profitable: 4,
    best_label: 'period=20',
    best_return: '0.40',
    worst_label: 'period=5',
    worst_return: '-0.10',
    median_return: '0.02',
    ...over,
  }
}

describe('StudyDispersion', () => {
  it('leads with the median, which is the number a study is for', () => {
    renderWithProviders(<StudyDispersion aggregate={aggregate({})} />)

    expect(screen.getByText('Median point')).toBeInTheDocument()
    expect(screen.getByText('2.0%')).toBeInTheDocument()
    expect(
      screen.getByText(/What a parameter set picked without hindsight returned/),
    ).toBeInTheDocument()
  })

  it('says how much of the searched space works, beside the best', () => {
    // ⚠️ The pair is the point. 40% at the best point means one thing when 4 of 6 combinations
    // were profitable and something else entirely when 1 of 60 was — and the second is what an
    // over-searched grid looks like.
    renderWithProviders(
      <StudyDispersion aggregate={aggregate({ points_profitable: 1, points_finished: 60 })} />,
    )

    expect(screen.getByText('1 / 60')).toBeInTheDocument()
    expect(screen.getByText('40.0%')).toBeInTheDocument()
  })

  it('says every figure is in-sample, including the best one', () => {
    // The sentence is load-bearing, not decoration: without it the screen presents a search of
    // the past as a claim about the future, which is the mistake this whole feature makes easy.
    renderWithProviders(<StudyDispersion aggregate={aggregate({})} />)

    expect(screen.getByText(/in-sample/)).toBeInTheDocument()
    expect(screen.getByText(/walk-forward/)).toBeInTheDocument()
  })

  it('shows a dash rather than zero while nothing has finished', () => {
    // Zero is a measured result of no profit. A study nobody has run yet has no result at all,
    // and collapsing the two would rank an unstarted study alongside one that broke even.
    renderWithProviders(
      <StudyDispersion
        aggregate={aggregate({
          points_finished: 0,
          points_profitable: 0,
          median_return: null,
          best_return: null,
          worst_return: null,
          best_label: null,
          worst_label: null,
        })}
      />,
    )

    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(4)
    expect(screen.queryByText('0.0%')).not.toBeInTheDocument()
  })

  it('reports points that failed to run without scoring them as losses', () => {
    renderWithProviders(<StudyDispersion aggregate={aggregate({ points_failed: 2 })} />)

    expect(screen.getByText(/2 point\(s\) failed to run/)).toBeInTheDocument()
  })
})
