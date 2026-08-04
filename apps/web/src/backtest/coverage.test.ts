import type { Backtest } from '../api/types'
import { coverageNotice } from './coverage'

function run(over: Partial<Backtest>): Backtest {
  return {
    id: 'b1',
    strategy_id: 's1',
    instrument_id: 'i1',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2026-08-03T00:00:00Z',
    initial_capital: '10000',
    status: 'done',
    error: null,
    engine_version: '0.1.0',
    created_at: '',
    started_at: null,
    finished_at: null,
    candles_seen: null,
    first_candle: null,
    last_candle: null,
    metrics: null,
    ...over,
  }
}

describe('coverageNotice', () => {
  it('reports the shortfall that actually happened', () => {
    // The real run: two years asked for, five months of data underneath.
    const notice = coverageNotice(
      run({
        candles_seen: 3480,
        first_candle: '2024-08-01T13:00:00Z',
        last_candle: '2026-07-31T19:00:00Z',
      }),
    )

    expect(notice).toEqual({
      requestedFrom: '2024-01-01',
      requestedTo: '2026-08-03',
      actualFrom: '2024-08-01',
      actualTo: '2026-07-31',
      candles: 3480,
    })
  })

  it('says nothing when the data covers the whole request', () => {
    expect(
      coverageNotice(
        run({
          date_from: '2024-09-01T00:00:00Z',
          date_to: '2024-09-30T00:00:00Z',
          candles_seen: 500,
          first_candle: '2024-08-15T00:00:00Z',
          last_candle: '2024-10-05T00:00:00Z',
        }),
      ),
    ).toBeNull()
  })

  it('does not nag when the first bar is later the same day', () => {
    // A session opening at 13:00 on the requested day is that day's data, not a gap. A notice
    // here would fire on every intraday run and train the reader to skip it.
    expect(
      coverageNotice(
        run({
          date_from: '2024-08-01T00:00:00Z',
          date_to: '2024-08-02T00:00:00Z',
          candles_seen: 7,
          first_candle: '2024-08-01T13:00:00Z',
          last_candle: '2024-08-02T19:00:00Z',
        }),
      ),
    ).toBeNull()
  })

  it('flags a run that stops short at the end only', () => {
    const notice = coverageNotice(
      run({
        date_from: '2024-01-01T00:00:00Z',
        date_to: '2026-08-03T00:00:00Z',
        candles_seen: 10,
        first_candle: '2023-12-01T00:00:00Z',
        last_candle: '2026-07-31T19:00:00Z',
      }),
    )

    expect(notice?.actualTo).toBe('2026-07-31')
  })

  it('stays silent when the run has no provenance at all', () => {
    // Null is unknown, not "covered everything". A failed run and an older row both land here,
    // and claiming full coverage for them would be the same lie this module exists to stop.
    expect(coverageNotice(run({ status: 'failed', error: 'no candles' }))).toBeNull()
  })

  it('needs all three fields before it will say anything', () => {
    expect(coverageNotice(run({ candles_seen: 100 }))).toBeNull()
    expect(coverageNotice(run({ candles_seen: 100, first_candle: '2024-08-01T00:00:00Z' }))).toBeNull()
  })
})
