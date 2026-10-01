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
  retest_of: [],
  earlier_uses: 0,
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
    // A test launched before 28/09 kept its clones, and says nothing about them.
    expect(screen.queryByText(/clones skipped/)).not.toBeInTheDocument()
  })

  it('marks a retest and names the earlier tests of its window (01/10)', async () => {
    getHoldout.mockResolvedValue({
      ...HOLDOUT,
      rule: { ...HOLDOUT.rule, retest: true, retest_of: ['test-0', 'walk-1', 'gone-1'] },
      earlier_uses: 3,
      retest_of: [
        {
          kind: 'holdout',
          id: 'test-0',
          test_id: 'test-0',
          fold: null,
          date_from: '2026-01-01T00:00:00Z',
          date_to: '2026-06-01T00:00:00Z',
          created_at: '2026-09-20T10:00:00Z',
        },
        {
          kind: 'walk_forward',
          id: 'walk-1',
          test_id: null,
          fold: 2,
          date_from: '2026-01-01T00:00:00Z',
          date_to: '2027-01-01T00:00:00Z',
          created_at: '2026-09-21T10:00:00Z',
        },
      ],
    })
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    const mark = await screen.findByLabelText('retest')
    expect(within(mark).getByText('Retest')).toBeInTheDocument()
    expect(
      within(mark).getByText(/used by 3 earlier tests of the sweep .* \(1 since deleted\)/),
    ).toBeInTheDocument()
    expect(within(mark).getByRole('link', { name: 'Reserved-window test' })).toHaveAttribute(
      'href',
      '/sweeps/test-0',
    )
    expect(within(mark).getByRole('link', { name: 'Walk-forward, fold 3' })).toHaveAttribute(
      'href',
      '/sweep-walkforwards/walk-1',
    )
    expect(within(mark).getByText(/tested 2026-01-01 → 2026-06-01 · launched 2026-09-20/))
      .toBeInTheDocument()
  })

  it('says nothing of retests on a first look', async () => {
    getHoldout.mockResolvedValue(HOLDOUT)
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    await screen.findByRole('link', { name: 'the sweep it came from' })
    expect(screen.queryByText('Retest')).not.toBeInTheDocument()
  })

  it('says when the clones were skipped', async () => {
    getHoldout.mockResolvedValue({ ...HOLDOUT, rule: { ...HOLDOUT.rule, distinct: true } })
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    expect(await screen.findByText(/· clones skipped/)).toBeInTheDocument()
  })

  it('leads with the medians both ways and the share still positive, charts in order', async () => {
    getHoldout.mockResolvedValue(HOLDOUT)
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    const table = await screen.findByRole('table', { name: 'By entry and chart' })
    const [, m15, h1] = within(table).getAllByRole('row')
    expect(m15).toHaveTextContent('M15101.3%-0.1%33%3 / 3')
    expect(h1).toHaveTextContent('H143.4%-5.8%33%2 / 3')
  })

  it('says how many finished points never traded out, apart from the median (01/10)', async () => {
    const [h1, m15] = HOLDOUT.groups
    if (h1 === undefined || m15 === undefined) throw new Error('no groups')
    getHoldout.mockResolvedValue({
      ...HOLDOUT,
      groups: [h1, { ...m15, no_trades_out: 1 }],
    })
    renderWithProviders(<HoldoutComparison sweepId="test-1" />)

    const table = await screen.findByRole('table', {
      name: 'By entry and chart',
    })
    const [, withNone, withAll] = within(table).getAllByRole('row')
    expect(withNone).toHaveTextContent('3 / 31 with no trade out')
    expect(withAll).not.toHaveTextContent(/no trade out/)
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
