import { describe, expect, it } from 'vitest'

import { RANK_MIN_TRADES, floorPlaceholder } from './rankFloor'

describe('the ranking floor', () => {
  it('is the API’s, chart by chart (ranking_floor.RANK_MIN_TRADES)', () => {
    expect(RANK_MIN_TRADES).toEqual({
      M1: 60,
      M5: 60,
      M15: 30,
      M30: 30,
      H1: 30,
      H4: 20,
      D1: 10,
      W1: 5,
    })
  })

  it('fills a blank floor field with its chart’s number, and says "default" for a chart it lacks', () => {
    expect(floorPlaceholder('D1')).toBe('10')
    expect(floorPlaceholder('H7')).toBe('default')
  })
})
