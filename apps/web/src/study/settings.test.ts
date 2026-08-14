import { describe, expect, it } from 'vitest'

import { ApiError } from '../api/client'

import {
  axesOf,
  combinationCount,
  emptyStudyForm,
  parseValues,
  launchFailure,
  studyLabel,
  toStudyRequest,
  whyNotLaunchable,
  type StudyForm,
} from './settings'

function form(over: Partial<StudyForm> = {}): StudyForm {
  return {
    ...emptyStudyForm,
    symbol: 'AAPL',
    dateFrom: '2024-01-01',
    dateTo: '2025-01-01',
    axes: [{ path: 'setup.params.period', raw: '5, 9, 20' }],
    ...over,
  }
}

describe('parseValues', () => {
  it('reads numbers as numbers, because "5" is not a period the DSL accepts', () => {
    expect(parseValues('5, 9, 20')).toEqual([5, 9, 20])
    expect(parseValues('1.5, 2.0')).toEqual([1.5, 2])
  })

  it('reads names as names, because a grid over side is long and short', () => {
    expect(parseValues('long, short')).toEqual(['long', 'short'])
  })

  it('reads flags as flags', () => {
    expect(parseValues('true, false')).toEqual([true, false])
  })

  it('drops blanks instead of turning a trailing comma into a value', () => {
    // ⚠️ `Number('')` is 0. Without the filter, a half-typed line would send the server a
    // period of zero — refused there, but refused with a message about a strategy rather than
    // about a comma, which is the one thing that would have helped.
    expect(parseValues('5, 9,')).toEqual([5, 9])
    expect(parseValues('  ')).toEqual([])
  })
})

describe('combinationCount', () => {
  it('multiplies the axes rather than adding them', () => {
    // ⚠️ Three by four, never two by two: on a square grid the product and the sum agree, and a
    // test written there would be the only test of this arithmetic while proving nothing.
    const three = form({
      axes: [
        { path: 'a', raw: '1, 2, 3' },
        { path: 'b', raw: '1, 2, 3, 4' },
      ],
    })

    expect(combinationCount(three)).toBe(12)
  })

  it('is zero, not one, when nothing has been filled in', () => {
    // An empty product is 1, and "1 combination" on a blank form reads as a study ready to
    // launch. Zero is what "nothing to launch" looks like.
    expect(combinationCount(form({ axes: [{ path: '', raw: '' }] }))).toBe(0)
  })

  it('ignores an axis with a path but no values, and one with values but no path', () => {
    const half = form({
      axes: [
        { path: 'setup.params.period', raw: '5, 9' },
        { path: 'setup.params.rr', raw: '' },
        { path: '', raw: '1, 2, 3' },
      ],
    })

    expect(combinationCount(half)).toBe(2)
    expect(Object.keys(axesOf(half))).toEqual(['setup.params.period'])
  })
})

describe('whyNotLaunchable', () => {
  it('approves a complete form', () => {
    expect(whyNotLaunchable(form())).toBeNull()
  })

  it('refuses a form with nothing to vary, because that is a backtest', () => {
    expect(whyNotLaunchable(form({ axes: [{ path: '', raw: '' }] }))).toMatch(/at least one/)
  })

  it('refuses an axis that repeats a value', () => {
    const repeated = form({
      axes: [{ path: 'setup.params.period', raw: '5, 9, 5' }],
    })

    expect(whyNotLaunchable(repeated)).toMatch(/repeats a value/)
  })

  it('refuses a grid over the cap, and says how big it is', () => {
    // The number is the message: someone who typed a fourth axis did not add five runs, they
    // multiplied by five, and nothing else on the form says so.
    const huge = form({
      axes: [
        { path: 'a', raw: '1,2,3,4,5,6,7,8,9,10' },
        { path: 'b', raw: '1,2,3,4,5,6,7,8,9,10' },
        { path: 'c', raw: '1,2,3,4,5,6' },
      ],
    })

    expect(whyNotLaunchable(huge)).toBe('That is 600 combinations, over the 500 a study will run.')
  })

  it('accepts a grid exactly at the cap', () => {
    // Its own test because `>` and `>=` are indistinguishable everywhere else on this form.
    const exact = form({
      axes: [
        {
          path: 'a',
          raw: Array.from({ length: 25 }, (_, at) => at + 1).join(','),
        },
        {
          path: 'b',
          raw: Array.from({ length: 20 }, (_, at) => at + 1).join(','),
        },
      ],
    })

    expect(combinationCount(exact)).toBe(500)
    expect(whyNotLaunchable(exact)).toBeNull()
  })

  it('refuses a period that ends before it starts', () => {
    expect(whyNotLaunchable(form({ dateFrom: '2025-01-01', dateTo: '2024-01-01' }))).toMatch(
      /precedes/,
    )
  })
})

describe('toStudyRequest', () => {
  it('charges nothing as a cost model of none, never as a spread of zero', () => {
    // ⚠️ The same distinction the instrument catalogue makes: zero ticks is the claim that this
    // market is free to trade, which is a claim and the wrong one. `{"type": "none"}` says the
    // run is uncosted, which is the truth.
    const request = toStudyRequest(form({ spreadTicks: '' }), 'strategy')

    expect(request.cost_model).toEqual({ type: 'none' })
  })

  it('carries a spread through as a string, so the tick count stays exact', () => {
    const request = toStudyRequest(form({ spreadTicks: '8' }), 'strategy')

    expect(request.cost_model).toEqual({ type: 'spread', spread_points: '8' })
  })

  it('sends the grid the form described, with its values typed', () => {
    const request = toStudyRequest(
      form({
        axes: [
          { path: 'setup.params.period', raw: '5, 9' },
          { path: 'setup.params.side', raw: 'long, short' },
        ],
      }),
      'strategy',
    )

    expect(request.grid).toEqual({
      'setup.params.period': [5, 9],
      'setup.params.side': ['long', 'short'],
    })
  })
})

describe('studyLabel', () => {
  it('names the market and what is being varied, not the full paths', () => {
    const label = studyLabel(
      form({
        axes: [
          { path: 'setup.params.period', raw: '5, 9' },
          { path: 'setup.params.rr', raw: '2, 3' },
        ],
      }),
    )

    expect(label).toBe('AAPL H1 · period, rr')
  })
})

describe('launchFailure', () => {
  it("shows the server's sentence, not the status it arrived with", () => {
    // ⚠️ `ApiError.message` is built from the status alone — "API error 422" — which is the one
    // thing a reader cannot act on. The reasons the server sends are specific by design, and
    // each is the sentence that says what to change.
    const refused = new ApiError(422, "'setup.params.periodd': this strategy has nothing at it")

    expect(launchFailure(refused)).toBe(
      "'setup.params.periodd': this strategy has nothing at it",
    )
  })

  it('reads the DSL validator body, which is the other shape a refusal takes', () => {
    // A path that resolves but a *value* that does not is refused by a different validator, and
    // it answers with a structure rather than a sentence. This is the more confusing of the two
    // to meet blind, so it is the one that most needs unpacking.
    const refused = new ApiError(422, {
      message: 'strategy failed schema validation',
      errors: [
        {
          loc: ['setup', 'mme9_breakout', 'params', 'breakeven_at_r'],
          msg: 'Input should be greater than 0',
        },
      ],
    })

    expect(launchFailure(refused)).toBe(
      'strategy failed schema validation: breakeven_at_r input should be greater than 0',
    )
  })

  it('keeps the message of an error that is not the API refusing', () => {
    // The network dying is not a validation problem, and its own message is the only thing that
    // knows what happened. Swallowing it would send a reader to fix a form that was fine.
    expect(launchFailure(new Error('Failed to fetch'))).toBe('Failed to fetch')
  })

  it('says something usable even for a shape it does not recognise', () => {
    expect(launchFailure({ weird: true })).toMatch(/Check the parameters/)
  })
})
