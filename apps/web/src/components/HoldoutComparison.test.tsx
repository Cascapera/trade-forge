import { screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { HoldoutOut, HoldoutRow, HoldoutSide } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { HoldoutComparison } from './HoldoutComparison'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  // The judgements below the comparison have their own test (`HoldoutSlicings.test`).
  return {
    ...actual,
    api: {
      ...actual.api,
      getHoldout: vi.fn(),
      listSlicings: vi.fn().mockResolvedValue([]),
      listMonteCarlos: vi.fn().mockResolvedValue([]),
    },
  }
})

const getHoldout = vi.mocked(api.getHoldout)

function side(runId: string, netReturn: string | null, status = 'done'): HoldoutSide {
  return {
    run_id: runId,
    status: status as HoldoutSide['status'],
    net_return: netReturn,
    total_trades: netReturn === null ? null : 12,
    profit_factor: null,
    max_drawdown_pct: null,
  }
}

function row(over: Partial<HoldoutRow>): HoldoutRow {
  return {
    entry_id: 'e1',
    entry_name: 'CHOCH',
    symbol: 'EURUSD',
    timeframe: 'M15',
    label: 'M15 · edge',
    values: {},
    in_sample: side('in-1', '1.013'),
    out_of_sample: side('out-1', '-0.001'),
    ...over,
  }
}

const HOLDOUT: HoldoutOut = {
  id: 'test-1',
  holdout_of: 'sweep-1',
  rule: { metric: 'net_profit', top_n: 3, min_trades: { H1: 30, M15: 30 } },
  date_from: '2026-01-01T00:00:00Z',
  date_to: '2026-09-19T00:00:00Z',
  searched_from: '2020-01-01T00:00:00Z',
  searched_to: '2026-01-01T00:00:00Z',
  groups: [
    {
      entry_id: 'e1',
      entry_name: 'CHOCH',
      timeframe: 'H1',
      points: 3,
      done: 2,
      in_sample_median_return: '0.434',
      out_of_sample_median_return: '-0.058',
      out_of_sample_positive: '0.3333',
    },
    {
      entry_id: 'e1',
      entry_name: 'CHOCH',
      timeframe: 'M15',
      points: 3,
      done: 3,
      in_sample_median_return: '1.013',
      out_of_sample_median_return: '-0.001',
      out_of_sample_positive: '0.3333',
    },
  ],
  rows: [
    row({ timeframe: 'H1', label: 'H1 · midpoint', out_of_sample: side('out-2', null, 'queued') }),
    row({}),
    row({ label: 'M15 · gone', in_sample: null, out_of_sample: side('out-3', '0.049') }),
  ],
}

describe('HoldoutComparison', () => {
  beforeEach(() => {
    getHoldout.mockReset()
  })

  it('says what was chosen where, and links back to the sweep it came from', async () => {
    getHoldout.mockResolvedValue(HOLDOUT)
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    const link = await screen.findByRole('link', { name: 'the sweep it came from' })
    expect(link).toHaveAttribute('href', '/sweeps/sweep-1')
    expect(
      screen.getByText(/Chosen on 2020-01-01 → 2026-01-01 · tested on 2026-01-01 → 2026-09-19/),
    ).toBeInTheDocument()
    // The floors in chart order, whatever order the server sent them in.
    expect(screen.getByText(/by net profit · fewest trades: M15 30, H1 30/)).toBeInTheDocument()
    expect(getHoldout).toHaveBeenCalledWith('test-1')
  })

  it('leads with the medians both ways and the share still positive, charts in order', async () => {
    getHoldout.mockResolvedValue(HOLDOUT)
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    const table = await screen.findByRole('table', { name: 'By entry and chart' })
    const [, m15, h1] = within(table).getAllByRole('row')
    expect(m15).toHaveTextContent('M15101.3%-0.1%33%3 / 3')
    expect(h1).toHaveTextContent('H143.4%-5.8%33%2 / 3')
  })

  it('shows each point beside the run it was chosen by, and what is still running', async () => {
    getHoldout.mockResolvedValue(HOLDOUT)
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    const table = await screen.findByRole('table', { name: 'Point by point' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows.map((one) => one.textContent)).toEqual([
      'CHOCHEURUSDM15 · edge101.3% (12 tr)-0.1% (12 tr)',
      'CHOCHEURUSDM15 · gone—4.9% (12 tr)',
      'CHOCHEURUSDH1 · midpoint101.3% (12 tr)queued',
    ])
    const [first] = rows
    if (first === undefined) throw new Error('no rows')
    expect(within(first).getByRole('link')).toHaveAttribute('href', '/results/out-1')
  })

  it('names a sweep since deleted rather than linking to nothing', async () => {
    getHoldout.mockResolvedValue({
      ...HOLDOUT,
      holdout_of: null,
      searched_from: null,
      searched_to: null,
      groups: [],
      rows: [row({ entry_name: null })],
    })
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    expect(await screen.findByText('a sweep since deleted')).toBeInTheDocument()
    expect(screen.getByText(/Chosen on — → —/)).toBeInTheDocument()
    expect(screen.getByText('(removed entry)')).toBeInTheDocument()
  })

  it('says so when the comparison cannot be read', async () => {
    getHoldout.mockRejectedValue(new Error('down'))
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    expect(await screen.findByText('Could not load the comparison.')).toBeInTheDocument()
  })
})
