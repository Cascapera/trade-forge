import { describe, expect, it } from 'vitest'

import type { DashboardSweep } from '../api/types'

import { launchWindow, localDay, runsPerDay } from './dashboard'

/** The local calendar reading of an instant — asserted this way so the test holds in any zone. */
function local(iso: string | undefined): [number, number, number, number, number] {
  const moment = new Date(iso ?? '')
  return [
    moment.getFullYear(),
    moment.getMonth() + 1,
    moment.getDate(),
    moment.getHours(),
    moment.getMinutes(),
  ]
}

describe('launchWindow', () => {
  it('starts at local midnight of the first day and ends at the midnight after the last', () => {
    // The last day is included, and the server's end is exclusive: 16th picked → 17th 00:00.
    const window = launchWindow('2026-09-15', '2026-09-16')

    expect(local(window?.launched_from)).toEqual([2026, 9, 15, 0, 0])
    expect(local(window?.launched_to)).toEqual([2026, 9, 17, 0, 0])
  })

  it('sends instants with an offset, never bare days', () => {
    const window = launchWindow('2026-09-15', '')

    expect(window?.launched_from).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/)
  })

  it('leaves an empty side unbounded', () => {
    expect(launchWindow('', '')).toEqual({})
    expect(launchWindow('2026-09-15', '')).not.toHaveProperty('launched_to')
    expect(launchWindow('', '2026-09-15')).not.toHaveProperty('launched_from')
  })

  it('crosses a month end', () => {
    expect(local(launchWindow('', '2026-09-30')?.launched_to)).toEqual([2026, 10, 1, 0, 0])
  })

  it('accepts one day as a window of one day', () => {
    const window = launchWindow('2026-09-16', '2026-09-16')

    expect(local(window?.launched_from)).toEqual([2026, 9, 16, 0, 0])
    expect(local(window?.launched_to)).toEqual([2026, 9, 17, 0, 0])
  })

  it('refuses days that run backwards', () => {
    expect(launchWindow('2026-09-16', '2026-09-15')).toBeNull()
  })

  it('treats a garbled day as no limit rather than as a date', () => {
    expect(launchWindow('16/09/2026', '')).toEqual({})
  })
})

describe('runsPerDay', () => {
  function sweep(createdAt: string, runs: number): DashboardSweep {
    return {
      id: createdAt,
      created_at: createdAt,
      entry_names: [],
      runs,
      finished: runs,
      winners: 0,
      median_return: null,
    }
  }

  it('adds up the runs of each local day, oldest first', () => {
    // Noon UTC is the same calendar day in every zone from -11 to +11.
    const days = runsPerDay([
      sweep('2026-09-16T12:00:00Z', 250),
      sweep('2026-09-15T12:00:00Z', 10),
      sweep('2026-09-15T12:30:00Z', 500),
    ])

    expect(days).toEqual([
      { day: '2026-09-15', runs: 510 },
      { day: '2026-09-16', runs: 250 },
    ])
  })

  it("counts a late-evening launch on the reader's day, not on UTC's", () => {
    // ⚠️ Pinned to São Paulo, because CI runs in UTC, where the two calendars never disagree
    // and a count taken off the ISO text would pass. 22:14 on the 15th there is 01:14Z on the
    // 16th — the real sweep of 500 runs.
    const zone = process.env.TZ
    process.env.TZ = 'America/Sao_Paulo'
    try {
      expect(runsPerDay([sweep('2026-09-16T01:14:48Z', 500)])).toEqual([
        { day: '2026-09-15', runs: 500 },
      ])
    } finally {
      process.env.TZ = zone
    }
  })

  it('reads the day in the local calendar', () => {
    const iso = '2026-09-16T01:14:48Z'
    const [year, month, date] = local(iso)
    const expected = `${String(year)}-${String(month).padStart(2, '0')}-${String(date).padStart(2, '0')}`

    expect(localDay(iso)).toBe(expected)
  })
})
