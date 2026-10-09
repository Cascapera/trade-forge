import { fireEvent, screen } from '@testing-library/react'

import type { LiveSetup, SignalRow } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useLiveSetups: vi.fn(),
  useChangeLive: vi.fn(),
  useInstruments: vi.fn(),
  useLiveSignals: vi.fn(),
}))

import { useChangeLive, useInstruments, useLiveSetups, useLiveSignals } from '../api/hooks'
import { LiveSignals } from './LiveSignals'

const setup: LiveSetup = {
  id: 's1',
  name: 'CHOCH BASE',
  strategy_id: 'st1',
  strategy_name: 'CHOCH BASE',
  timeframe: 'H1',
  no_target_r: '5.00000000',
  active: true,
  note: null,
  source_backtest_id: 'b1',
  created_at: '2026-10-09T12:00:00Z',
  markets: [
    { instrument_id: 'win', symbol: 'WIN', broker: 'xp', active: true },
    { instrument_id: 'wdo', symbol: 'WDO', broker: 'xp', active: false },
  ],
  metrics: {
    signals: 4,
    closed: 2,
    open: 1,
    cancelled: 1,
    wins: 1,
    win_rate: '0.5',
    net_r: '4',
    average_r: '2',
    profit_factor: '5',
    max_drawdown_r: '1',
    r_by_month: {},
  },
}

const signal: SignalRow = {
  number: 41,
  symbol: 'WIN',
  timeframe: 'H1',
  side: 'long',
  status: 'closed',
  order_type: 'stop',
  entry: '207055',
  stop: '206500',
  target: null,
  exit_price: '209830',
  result_r: '5',
  reason: null,
  armed_at: '2026-10-09T13:00:00Z',
  triggered_at: '2026-10-09T14:00:00Z',
  ended_at: '2026-10-09T16:00:00Z',
  strategy_id: 'st1',
}

function mount(setups: LiveSetup[]): ReturnType<typeof vi.fn> {
  const mutate = vi.fn()
  vi.mocked(useLiveSetups).mockReturnValue({ data: setups, isPending: false } as never)
  vi.mocked(useChangeLive).mockReturnValue({ mutate, isPending: false, error: null } as never)
  vi.mocked(useInstruments).mockReturnValue({
    data: [{ id: 'bit', symbol: 'BIT', broker: 'xp' }],
  } as never)
  vi.mocked(useLiveSignals).mockReturnValue({ data: [signal], isPending: false } as never)
  renderWithProviders(<LiveSignals />)
  return mutate
}

describe('LiveSignals', () => {
  it('shows each setup with its chart, markets and metrics in R', () => {
    mount([setup])

    expect(screen.getByRole('heading', { name: 'CHOCH BASE' })).toBeInTheDocument()
    expect(screen.getByText('H1')).toBeInTheDocument()
    expect(screen.getByText('+4.00R')).toBeInTheDocument()
    expect(screen.getByText('50%')).toBeInTheDocument()
    expect(screen.getByText('2 / 1 / 1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'WIN' })).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'run it came from' })).toHaveAttribute(
      'href',
      '/results/b1',
    )
  })

  it('adds, switches and drops markets', () => {
    const mutate = mount([setup])

    fireEvent.change(screen.getByRole('combobox', { name: 'Market to add' }), {
      target: { value: 'bit' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add' }))
    fireEvent.click(screen.getByRole('button', { name: 'WDO' }))
    fireEvent.click(screen.getByRole('button', { name: 'Drop WIN' }))

    expect(mutate.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      { kind: 'add-market', id: 's1', instrumentId: 'bit' },
      { kind: 'market', id: 's1', instrumentId: 'wdo', active: true },
      { kind: 'remove-market', id: 's1', instrumentId: 'win' },
    ])
  })

  it('opens the history of signals', () => {
    mount([setup])

    fireEvent.click(screen.getByRole('button', { name: /Show the signals \(4\)/ }))

    expect(screen.getByText('41')).toBeInTheDocument()
    expect(screen.getByText('+5.00R')).toBeInTheDocument()
  })

  it('switches a setup on and off, and removes it', () => {
    const mutate = mount([{ ...setup, active: false, source_backtest_id: null }])

    expect(screen.queryByRole('link', { name: 'run it came from' })).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Turn on' }))
    fireEvent.click(screen.getByRole('button', { name: 'Remove CHOCH BASE' }))

    expect(mutate.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      { kind: 'setup', id: 's1', patch: { active: true } },
      { kind: 'remove-setup', id: 's1' },
    ])
  })

  it('shows a setup with no result yet as dashes, and a sell still in trade', () => {
    mount([
      {
        ...setup,
        metrics: {
          ...setup.metrics,
          win_rate: null,
          profit_factor: null,
          average_r: null,
          net_r: '-1',
        },
      },
    ])
    vi.mocked(useLiveSignals).mockReturnValue({
      data: [{ ...signal, side: 'short', status: 'triggered', result_r: null, ended_at: null }],
      isPending: false,
    } as never)

    fireEvent.click(screen.getByRole('button', { name: /Show the signals/ }))

    expect(screen.getByText('-1.00R')).toBeInTheDocument()
    expect(screen.getByText('sell')).toBeInTheDocument()
    expect(screen.getByText('in trade')).toBeInTheDocument()
  })

  it('says when a setup has posted nothing yet, and while it loads', () => {
    mount([setup])
    vi.mocked(useLiveSignals).mockReturnValue({ data: [], isPending: false } as never)
    fireEvent.click(screen.getByRole('button', { name: /Show the signals/ }))
    expect(screen.getByText('No signal posted yet.')).toBeInTheDocument()

    vi.mocked(useLiveSetups).mockReturnValue({ data: undefined, isPending: true } as never)
    renderWithProviders(<LiveSignals />)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('says when nothing is followed', () => {
    mount([])
    expect(screen.getByText('No setup is followed yet.')).toBeInTheDocument()
  })
})
