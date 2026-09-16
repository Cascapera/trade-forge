import { describe, expect, it } from 'vitest'

import type { BacktestStatus, SweepRunOut } from '../api/types'

import { settled, summarise, tally } from './progress'

function rows(...statuses: BacktestStatus[]): SweepRunOut[] {
  return statuses.map(
    (status, index) =>
      ({
        entry_id: 'e1',
        entry_name: 'zeta',
        label: `M15 · period=${String(index)}`,
        values: {},
        run: { id: `r${String(index)}`, status },
      }) as SweepRunOut,
  )
}

describe('tally', () => {
  it('puts every run in exactly one column', () => {
    // ⚠️ The property an X of Y counter lives on: the columns add up to the total. A tally read
    // off the entries' aggregates does not — those count a point finished once it has metrics.
    const counts = tally(rows('done', 'done', 'running', 'queued', 'failed'))

    expect(counts).toEqual({ total: 5, done: 2, running: 1, queued: 1, failed: 1 })
    expect(counts.done + counts.running + counts.queued + counts.failed).toBe(counts.total)
  })

  it('counts an empty sweep as nothing rather than as finished', () => {
    expect(tally([])).toEqual({ total: 0, done: 0, running: 0, queued: 0, failed: 0 })
  })
})

describe('summarise', () => {
  it('says how many are done even when every one of them is', () => {
    // ⚠️ The reason this exists. The old line showed only while runs were outstanding, so a
    // finished sweep and one that never started both rendered as no line at all.
    expect(summarise(tally(rows('done', 'done', 'done')))).toBe('3 of 3 done')
  })

  it('names what is still in flight', () => {
    expect(summarise(tally(rows('done', 'running', 'queued')))).toBe(
      '1 of 3 done · 1 running · 1 queued',
    )
  })

  it('keeps failures visible beside the count', () => {
    expect(summarise(tally(rows('done', 'done', 'failed')))).toBe('2 of 3 done · 1 failed')
  })

  it('leaves out the columns that are empty', () => {
    const line = summarise(tally(rows('done', 'queued')))

    expect(line).toBe('1 of 2 done · 1 queued')
    expect(line).not.toContain('running')
    expect(line).not.toContain('failed')
  })

  it('shows a run that will never execute rather than hiding it', () => {
    // A worker that lost its database connection leaves a run `queued` with no job behind it.
    // The counter is where that becomes visible: 9 of 10, one queued, for ever.
    expect(summarise(tally(rows(...Array<BacktestStatus>(9).fill('done'), 'queued')))).toBe(
      '9 of 10 done · 1 queued',
    )
  })
})

describe('settled', () => {
  it('is false while anything is queued or running', () => {
    expect(settled(tally(rows('done', 'queued')))).toBe(false)
    expect(settled(tally(rows('done', 'running')))).toBe(false)
  })

  it('counts a failed run as settled, because it is not coming back', () => {
    expect(settled(tally(rows('done', 'failed')))).toBe(true)
  })
})
