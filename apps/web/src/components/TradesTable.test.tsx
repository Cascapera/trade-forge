import { fireEvent, screen, waitFor } from '@testing-library/react'

import { api } from '../api/client'
import type { Snapshot, Trade } from '../api/types'
import { renderWithProviders } from '../test-utils'
import { TradesTable } from './TradesTable'

const BACKTEST = '11111111-1111-1111-1111-111111111111'

const win: Trade = {
  id: 1,
  direction: 'long',
  entry_time: '2024-01-01T00:00:00Z',
  entry_price: '1.10000',
  exit_time: '2024-01-01T01:00:00Z',
  exit_price: '1.10200',
  exit_reason: 'tp',
  volume: '1',
  stop_loss: '1.09900',
  take_profit: '1.10200',
  gross_pnl: '200',
  costs: '0',
  net_pnl: '200',
  r_multiple: '2',
  context: {},
  has_snapshot: true,
}

const open: Trade = {
  ...win,
  id: 2,
  exit_time: null,
  exit_price: null,
  exit_reason: null,
  net_pnl: null,
  r_multiple: null,
}

const snapshot: Snapshot = {
  decided_at: '2023-12-31T23:00:00Z',
  filled_at: '2024-01-01T00:00:00Z',
  bars: [
    {
      time: '2023-12-31T23:00:00Z',
      open: '1.09900',
      high: '1.10050',
      low: '1.09850',
      close: '1.10000',
    },
    {
      time: '2024-01-01T00:00:00Z',
      open: '1.10000',
      high: '1.10150',
      low: '1.09950',
      close: '1.10100',
    },
  ],
  regions: [],
  series: [],
}

describe('TradesTable', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('renders a row per trade with net and R', () => {
    renderWithProviders(<TradesTable trades={[win]} backtestId={BACKTEST} />)
    expect(screen.getByText('+200.00')).toBeInTheDocument()
    expect(screen.getByText('2.00R')).toBeInTheDocument()
    expect(screen.getByText('tp')).toBeInTheDocument()
  })

  it('shows em dashes for the columns an open trade has not filled', () => {
    renderWithProviders(<TradesTable trades={[open]} backtestId={BACKTEST} />)
    expect(screen.getAllByText('—').length).toBeGreaterThanOrEqual(3)
  })

  it('shows an empty-state message for no trades', () => {
    renderWithProviders(<TradesTable trades={[]} backtestId={BACKTEST} />)
    expect(screen.getByText('This run produced no trades.')).toBeInTheDocument()
  })

  it('fetches nothing until a chart is asked for', () => {
    const spy = vi.spyOn(api, 'getTradeSnapshot')
    renderWithProviders(<TradesTable trades={[win, open]} backtestId={BACKTEST} />)
    // The whole point of the on-demand endpoint. Rendering the table must not pull a window
    // for every row — that is megabytes a reader never looks at.
    expect(spy).not.toHaveBeenCalled()
  })

  it('loads one snapshot when its button is pressed, and only that one', async () => {
    const spy = vi.spyOn(api, 'getTradeSnapshot').mockResolvedValue(snapshot)
    renderWithProviders(<TradesTable trades={[win, open]} backtestId={BACKTEST} />)

    fireEvent.click(screen.getAllByRole('button', { name: /show the entry chart/i })[0]!)

    await waitFor(() => {
      expect(screen.getByRole('img', { name: /barras em volta da entrada/i })).toBeInTheDocument()
    })
    expect(spy).toHaveBeenCalledTimes(1)
    expect(spy).toHaveBeenCalledWith(BACKTEST, win.id)
  })

  it('closes the chart it opened, and opens one at a time', async () => {
    vi.spyOn(api, 'getTradeSnapshot').mockResolvedValue(snapshot)
    renderWithProviders(<TradesTable trades={[win, open]} backtestId={BACKTEST} />)
    const buttons = screen.getAllByRole('button', { name: /entry chart/i })

    fireEvent.click(buttons[0]!)
    await waitFor(() => {
      expect(screen.getByRole('img', { name: /barras/i })).toBeInTheDocument()
    })

    // Opening the second closes the first: several charts at once turn a table you scan into a
    // page you scroll, and the question being asked is about one entry.
    fireEvent.click(screen.getAllByRole('button', { name: /entry chart/i })[1]!)
    await waitFor(() => {
      expect(screen.getAllByRole('img', { name: /barras/i })).toHaveLength(1)
    })

    fireEvent.click(screen.getAllByRole('button', { name: /hide the entry chart/i })[0]!)
    await waitFor(() => {
      expect(screen.queryByRole('img', { name: /barras/i })).not.toBeInTheDocument()
    })
  })

  it('offers no button for a trade that recorded no window', () => {
    renderWithProviders(
      <TradesTable trades={[{ ...win, has_snapshot: false }]} backtestId={BACKTEST} />,
    )
    // Absent rather than disabled: a control that can never do anything is noise, and a run
    // older than the feature has none of these.
    expect(screen.queryByRole('button', { name: /entry chart/i })).not.toBeInTheDocument()
  })

  it('says so when the snapshot cannot be loaded, instead of showing an empty chart', async () => {
    vi.spyOn(api, 'getTradeSnapshot').mockRejectedValue(new Error('nope'))
    renderWithProviders(<TradesTable trades={[win]} backtestId={BACKTEST} />)

    fireEvent.click(screen.getByRole('button', { name: /show the entry chart/i }))

    await waitFor(() => {
      expect(screen.getByText(/could not load this entry/i)).toBeInTheDocument()
    })
    expect(screen.queryByRole('img', { name: /barras/i })).not.toBeInTheDocument()
  })
})
