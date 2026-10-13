import {
  type BoundFunctions,
  fireEvent,
  type queries,
  screen,
  within,
} from '@testing-library/react'

import { ApiError } from '../api/client'
import type { LiveMarketSetupOut, LiveMarketsOut, LiveSetup, SignalRow } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useLiveSetups: vi.fn(),
  useChangeLive: vi.fn(),
  useInstruments: vi.fn(),
  useLiveSignals: vi.fn(),
  useLiveMarkets: vi.fn(),
  useRestartMarket: vi.fn(),
  useRegisterLiveSetup: () => ({
    mutate: vi.fn(),
    isPending: false,
    error: null,
  }),
  useStrategy: () => ({ data: undefined }),
  useEditLiveSetup: () => ({ mutate: vi.fn(), isPending: false, error: null }),
}))

// The pickers have their own tests; here each is a button per market that toggles it.
vi.mock('../components/MarketBrowser', () => ({
  MarketBrowser: (props: {
    chosen: string[]
    onChange: (next: string[]) => void
    onClose: () => void
  }) => (
    <div>
      {['BIT', 'WIN', 'ZZZ'].map((symbol) => (
        <button
          key={symbol}
          type="button"
          onClick={() => {
            props.onChange(
              props.chosen.includes(symbol)
                ? props.chosen.filter((one) => one !== symbol)
                : [...props.chosen, symbol],
            )
          }}
        >
          tick {symbol}
        </button>
      ))}
      <button type="button" onClick={props.onClose}>
        done
      </button>
    </div>
  ),
}))
vi.mock('../components/SymbolPicker', () => ({ SymbolPicker: () => <div /> }))

import {
  useChangeLive,
  useInstruments,
  useLiveMarkets,
  useLiveSetups,
  useLiveSignals,
  useRestartMarket,
} from '../api/hooks'
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

/** The screen on its "By setup" tab, which reads each setup over every market. */
function mount(setups: LiveSetup[]): ReturnType<typeof vi.fn> {
  const mutate = vi.fn()
  vi.mocked(useLiveMarkets).mockReturnValue({
    data: { supervisor: { alive: true, at: null, reachable: true }, assets: [] },
    isPending: false,
    isError: false,
  } as never)
  vi.mocked(useRestartMarket).mockReturnValue({
    mutate: vi.fn(),
    isPending: false,
    isSuccess: false,
    isError: false,
    error: null,
  } as never)
  vi.mocked(useLiveSetups).mockReturnValue({
    data: setups,
    isPending: false,
  } as never)
  vi.mocked(useChangeLive).mockReturnValue({
    mutate,
    isPending: false,
    error: null,
  } as never)
  vi.mocked(useInstruments).mockReturnValue({
    data: [
      { id: 'bit', symbol: 'BIT', broker: 'xp' },
      { id: 'win', symbol: 'WIN', broker: 'xp' },
      { id: 'wdo', symbol: 'WDO', broker: 'xp' },
    ],
  } as never)
  vi.mocked(useLiveSignals).mockReturnValue({
    data: [signal],
    isPending: false,
  } as never)
  renderWithProviders(<LiveSignals />)
  fireEvent.click(screen.getByRole('tab', { name: 'By setup' }))
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

    fireEvent.click(screen.getByRole('button', { name: 'Add markets…' }))
    fireEvent.click(screen.getByRole('button', { name: 'tick BIT' }))
    fireEvent.click(screen.getByRole('button', { name: 'tick ZZZ' }))
    fireEvent.click(screen.getByRole('button', { name: 'tick WIN' }))
    fireEvent.click(screen.getByRole('button', { name: 'tick BIT' }))
    fireEvent.click(screen.getByRole('button', { name: 'done' }))
    fireEvent.click(screen.getByRole('button', { name: 'WDO' }))
    fireEvent.click(screen.getByRole('button', { name: 'Drop WIN' }))

    expect(mutate.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      { kind: 'add-market', id: 's1', instrumentId: 'bit' },
      { kind: 'remove-market', id: 's1', instrumentId: 'win' },
      { kind: 'remove-market', id: 's1', instrumentId: 'bit' },
      { kind: 'market', id: 's1', instrumentId: 'wdo', active: true },
      { kind: 'remove-market', id: 's1', instrumentId: 'win' },
    ])
    expect(screen.queryByRole('button', { name: 'done' })).not.toBeInTheDocument()
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
      data: [
        {
          ...signal,
          side: 'short',
          status: 'triggered',
          result_r: null,
          ended_at: null,
        },
      ],
      isPending: false,
    } as never)

    fireEvent.click(screen.getByRole('button', { name: /Show the signals/ }))

    expect(screen.getByText('-1.00R')).toBeInTheDocument()
    expect(screen.getByText('sell')).toBeInTheDocument()
    expect(screen.getByText('in trade')).toBeInTheDocument()
  })

  it('says when a setup has posted nothing yet, and while it loads', () => {
    mount([setup])
    vi.mocked(useLiveSignals).mockReturnValue({
      data: [],
      isPending: false,
    } as never)
    fireEvent.click(screen.getByRole('button', { name: /Show the signals/ }))
    expect(screen.getByText('No signal posted yet.')).toBeInTheDocument()

    vi.mocked(useLiveSetups).mockReturnValue({
      data: undefined,
      isPending: true,
    } as never)
    renderWithProviders(<LiveSignals />)
    fireEvent.click(screen.getAllByRole('tab', { name: 'By setup' })[1]!)
    expect(screen.getByText('Loading…')).toBeInTheDocument()
  })

  it('opens the form for a new setup, and the editor of one', () => {
    mount([setup])

    fireEvent.click(screen.getByRole('button', { name: 'New setup' }))
    expect(screen.getByRole('heading', { name: 'New setup' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(screen.getByRole('button', { name: 'New setup' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    expect(screen.getByText('Loading the setup…')).toBeInTheDocument()
  })

  it('says when nothing is followed', () => {
    mount([])
    expect(screen.getByText('No setup is followed yet.')).toBeInTheDocument()
  })
})

const armed: SignalRow = {
  ...signal,
  number: 42,
  timeframe: 'M5',
  side: 'short',
  status: 'armed',
  order_type: 'limit',
  entry: '128900',
  stop: '129100',
  target: '128300',
  exit_price: null,
  result_r: null,
  triggered_at: null,
  ended_at: null,
}

const inTrade: SignalRow = {
  ...signal,
  number: 43,
  timeframe: 'M5',
  status: 'triggered',
  entry: '128450',
  stop: '128300',
  exit_price: null,
  result_r: null,
  ended_at: null,
}

function onMarket(overrides: Partial<LiveMarketSetupOut>): LiveMarketSetupOut {
  return {
    setup_id: 's1',
    name: 'CHOCH BASE',
    strategy_name: 'CHOCH BASE',
    setup_active: true,
    active: true,
    no_target_r: '5.00000000',
    state: 'running',
    retry_in: null,
    under_way: [],
    metrics: setup.metrics,
    ...overrides,
  }
}

const live = { key: 'win:M5', phase: 'live' as const, heard_at: null, sessions: 2 }

const markets: LiveMarketsOut = {
  supervisor: { alive: true, at: '2026-10-13T15:00:00Z', reachable: true },
  assets: [
    {
      instrument_id: 'win',
      symbol: 'WIN',
      broker: 'xp',
      timeframes: [
        {
          timeframe: 'M5',
          processes: [live],
          restarting: false,
          setups: [
            onMarket({
              setup_id: 'm5a',
              name: 'CHOCH - 5 MIN',
              strategy_name: 'CHOCH',
              under_way: [armed, inTrade],
            }),
            onMarket({
              setup_id: 'm5b',
              name: 'CONTINUATION',
              strategy_name: 'CONTINUATION',
              state: 'waiting',
              retry_in: 42,
              metrics: { ...setup.metrics, signals: 1, net_r: '-1', win_rate: null },
            }),
          ],
        },
        {
          timeframe: 'H1',
          processes: [live, { ...live, key: 'win:H1', phase: 'warming' }],
          restarting: false,
          setups: [onMarket({ active: false, state: 'off' })],
        },
      ],
    },
  ],
}

/** The screen on its default tab, by asset; the setups are what "Add setup" offers. */
function mountByAsset(
  data: LiveMarketsOut | undefined,
  restartState: Record<string, unknown> = {},
): {
  mutate: ReturnType<typeof vi.fn>
  restart: ReturnType<typeof vi.fn>
  rerender: (next: React.ReactNode) => void
} {
  const mutate = vi.fn()
  // Taken at once, the way a 202 lands: the confirmation closes on its success.
  const restart = vi.fn((_body: unknown, options?: { onSuccess?: () => void }) => {
    options?.onSuccess?.()
  })
  vi.mocked(useLiveMarkets).mockReturnValue({
    data,
    isPending: data === undefined,
    isError: false,
  } as never)
  vi.mocked(useRestartMarket).mockReturnValue({
    mutate: restart,
    isPending: false,
    isSuccess: false,
    isError: false,
    error: null,
    ...restartState,
  } as never)
  vi.mocked(useLiveSetups).mockReturnValue({
    data: [
      setup,
      { ...setup, id: 's9', name: 'BOS H1', markets: [] },
      { ...setup, id: 's5', name: 'BOS M5', timeframe: 'M5', markets: [] },
    ],
    isPending: false,
  } as never)
  vi.mocked(useChangeLive).mockReturnValue({
    mutate,
    isPending: false,
    error: null,
  } as never)
  vi.mocked(useLiveSignals).mockReturnValue({
    data: [signal, { ...signal, number: 50, symbol: 'WDO' }],
    isPending: false,
  } as never)
  const { rerender } = renderWithProviders(<LiveSignals />)
  return { mutate, restart, rerender }
}

/** One market's block — an asset on a timeframe — to query inside. */
function market(name: string): BoundFunctions<typeof queries> {
  return within(screen.getByRole('region', { name }))
}

describe('LiveSignals by asset', () => {
  it('shows each asset by timeframe, with its processes, setups and signals under way', () => {
    mountByAsset(markets)

    expect(screen.getByRole('tab', { name: 'By asset' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('heading', { name: 'WIN' })).toBeInTheDocument()
    expect(screen.getByText('xp')).toBeInTheDocument()
    expect(screen.getByText('2 setups on · 1 armed · 1 in a trade')).toBeInTheDocument()
    expect(screen.getByText(/Signals supervisor running/)).toBeInTheDocument()

    const m5 = market('WIN M5')
    expect(m5.getByText('● live · 1 process')).toBeInTheDocument()
    expect(m5.getByText('CHOCH - 5 MIN')).toBeInTheDocument()
    expect(m5.getByText('CHOCH')).toBeInTheDocument()
    expect(m5.getByText('running')).toBeInTheDocument()
    expect(m5.getByText('failed, retrying in 42 s')).toBeInTheDocument()
    expect(m5.getByText('#42 armed: sell limit 128900 · stop 129100')).toBeInTheDocument()
    expect(
      m5.getByText('#43 in trade: buy 128450 · stop 128300 · no target (5R)'),
    ).toBeInTheDocument()
    expect(m5.getByText('4 signals · net +4.00R · win rate 50%')).toBeInTheDocument()
    expect(m5.getByText('1 signal · net -1.00R · win rate —')).toBeInTheDocument()

    const h1 = market('WIN H1')
    expect(
      h1.getByText('warming · 2 processes — a setup added since the last restart runs apart'),
    ).toBeInTheDocument()
    expect(h1.getByRole('switch', { name: 'CHOCH BASE on WIN' })).toHaveAttribute(
      'aria-checked',
      'false',
    )
    // A setup switched off here has no state pill: its switch already says so.
    expect(h1.getAllByText('off')).toHaveLength(1)
  })

  it('switches a setup on one market, and adds one of the same timeframe', () => {
    const { mutate } = mountByAsset(markets)

    fireEvent.click(screen.getByRole('switch', { name: 'CHOCH BASE on WIN' }))
    const add = market('WIN H1').getByRole('combobox', { name: 'Add a setup to WIN H1' })
    expect(within(add).getAllByRole('option').map((one) => one.textContent)).toEqual([
      'Add setup…',
      'BOS H1',
    ])
    fireEvent.change(add, { target: { value: 's9' } })

    expect(mutate.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      { kind: 'market', id: 's1', instrumentId: 'win', active: true },
      { kind: 'add-market', id: 's9', instrumentId: 'win' },
    ])
  })

  it('offers no setup to add when every one of the timeframe is already there', () => {
    mountByAsset(markets)
    vi.mocked(useLiveSetups).mockReturnValue({ data: [setup], isPending: false } as never)
    renderWithProviders(<LiveSignals />)

    const h1 = screen.getAllByRole('region', { name: 'WIN H1' })[1]
    expect(h1).not.toHaveTextContent('Add setup…')
    // Said rather than hidden: an empty space read as the control missing (13/10).
    expect(h1).toHaveTextContent('Every H1 setup is already on WIN')
  })

  it('removes a setup from this asset after saying what that does to its signals', () => {
    const { mutate } = mountByAsset(markets)
    const m5 = market('WIN M5')

    fireEvent.click(m5.getAllByRole('button', { name: 'Remove from WIN' })[0]!)

    expect(
      m5.getByText(/CHOCH - 5 MIN stops following WIN; it stays on its other assets\./),
    ).toBeInTheDocument()
    expect(
      m5.getByText(
        '1 armed signal will be cancelled on the channel; 1 in a trade will be followed by price.',
      ),
    ).toBeInTheDocument()
    fireEvent.click(m5.getByRole('button', { name: 'Yes, remove from WIN' }))
    expect(mutate.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      { kind: 'remove-market', id: 'm5a', instrumentId: 'win' },
    ])
  })

  it('deletes a setup from every asset only once confirmed, and can be kept', () => {
    const { mutate } = mountByAsset(markets)
    const m5 = market('WIN M5')

    fireEvent.click(m5.getAllByRole('button', { name: 'Delete setup' })[1]!)
    expect(m5.getByText(/CONTINUATION is deleted from every asset it follows/)).toBeInTheDocument()
    expect(m5.getByText('No signal is under way here, so none is touched.')).toBeInTheDocument()
    fireEvent.click(m5.getByRole('button', { name: 'Keep it' }))
    expect(m5.queryByRole('button', { name: 'Yes, delete CONTINUATION' })).not.toBeInTheDocument()
    expect(mutate).not.toHaveBeenCalled()

    fireEvent.click(m5.getAllByRole('button', { name: 'Delete setup' })[1]!)
    fireEvent.click(m5.getByRole('button', { name: 'Yes, delete CONTINUATION' }))
    expect(mutate.mock.calls.map((call: unknown[]) => call[0])).toEqual([
      { kind: 'remove-setup', id: 'm5b' },
    ])
  })

  it('says what a restart does to the signals under way, then asks for it', () => {
    const { restart } = mountByAsset(markets)
    const m5 = market('WIN M5')

    fireEvent.click(m5.getByRole('button', { name: 'Restart' }))
    expect(
      m5.getByText(
        '1 armed signal will be cancelled on the channel; 1 in a trade will be followed by price.',
      ),
    ).toBeInTheDocument()
    expect(m5.getByRole('button', { name: 'Restart' })).toBeDisabled()
    fireEvent.click(m5.getByRole('button', { name: 'Keep it running' }))
    expect(restart).not.toHaveBeenCalled()

    fireEvent.click(m5.getByRole('button', { name: 'Restart' }))
    fireEvent.click(m5.getByRole('button', { name: 'Restart WIN M5' }))
    expect(restart).toHaveBeenCalledWith(
      { instrument_id: 'win', timeframe: 'M5' },
      expect.anything(),
    )
    expect(m5.queryByRole('button', { name: 'Restart WIN M5' })).not.toBeInTheDocument()

    const h1 = market('WIN H1')
    fireEvent.click(h1.getByRole('button', { name: 'Restart' }))
    expect(h1.getByText('No signal is under way here, so none is touched.')).toBeInTheDocument()
  })

  it('counts every armed signal of the market across its setups', () => {
    const asset = markets.assets[0]!
    mountByAsset({
      ...markets,
      assets: [
        {
          ...asset,
          timeframes: [
            {
              ...asset.timeframes[0]!,
              setups: [
                onMarket({ setup_id: 'a', under_way: [armed] }),
                onMarket({ setup_id: 'b', name: 'Other', under_way: [{ ...armed, number: 44 }] }),
              ],
            },
          ],
        },
      ],
    })

    fireEvent.click(screen.getByRole('button', { name: 'Restart' }))
    expect(
      screen.getByText(
        '2 armed signals will be cancelled on the channel; 0 in a trade will be followed by price.',
      ),
    ).toBeInTheDocument()
  })

  it('shows a trade with its target, and an order whose levels are not known yet', () => {
    const asset = markets.assets[0]!
    mountByAsset({
      ...markets,
      assets: [
        {
          ...asset,
          timeframes: [
            {
              ...asset.timeframes[0]!,
              setups: [
                onMarket({
                  state: 'waiting',
                  under_way: [
                    { ...inTrade, target: '129200' },
                    { ...armed, order_type: null, entry: null, stop: null },
                  ],
                }),
              ],
            },
          ],
        },
      ],
    })

    expect(
      screen.getByText('#43 in trade: buy 128450 · stop 128300 · target 129200'),
    ).toBeInTheDocument()
    expect(screen.getByText('#42 armed: sell — · stop —')).toBeInTheDocument()
    expect(screen.getByText('failed, retrying')).toBeInTheDocument()
  })

  it('says a restart is being asked for while it is', () => {
    const { rerender } = mountByAsset(markets)
    const m5 = market('WIN M5')
    fireEvent.click(m5.getByRole('button', { name: 'Restart' }))

    vi.mocked(useRestartMarket).mockReturnValue({
      mutate: vi.fn(),
      isPending: true,
      isSuccess: false,
      isError: false,
      error: null,
    } as never)
    rerender(<LiveSignals />)

    expect(m5.getByRole('button', { name: 'Asking…' })).toBeDisabled()
  })

  it('holds the switches while a change is on its way, and says why one failed', () => {
    mountByAsset(markets)
    vi.mocked(useChangeLive).mockReturnValue({
      mutate: vi.fn(),
      isPending: true,
      error: new Error('that market is gone'),
    } as never)
    renderWithProviders(<LiveSignals />)

    const h1 = within(screen.getAllByRole('region', { name: 'WIN H1' })[1]!)
    expect(h1.getByRole('switch', { name: 'CHOCH BASE on WIN' })).toBeDisabled()
    expect(h1.getByRole('combobox', { name: 'Add a setup to WIN H1' })).toBeDisabled()
    expect(h1.getByText('that market is gone')).toBeInTheDocument()
  })

  it('tells when the restart was asked, and why it was not', () => {
    mountByAsset(markets, {
      isSuccess: true,
      submittedAt: Date.parse('2026-10-13T15:00:00Z'),
    })
    expect(screen.getAllByText(/^Asked at 13\/10.*12:00; the supervisor takes it/)).toHaveLength(2)

    mountByAsset(markets, {
      isError: true,
      error: new ApiError(503, 'could not reach Redis to ask for it'),
    })
    expect(screen.getAllByText('could not reach Redis to ask for it')).toHaveLength(2)
  })

  it('warns when the supervisor is stopped, and can restart nothing', () => {
    mountByAsset({ ...markets, supervisor: { alive: false, at: null, reachable: true } })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Signals supervisor stopped — run tradeforge-sinais.ps1',
    )
    for (const button of screen.getAllByRole('button', { name: 'Restart' })) {
      expect(button).toBeDisabled()
    }
    expect(market('WIN M5').getByText('unknown')).toBeInTheDocument()
  })

  it('warns when Redis cannot be read', () => {
    mountByAsset({ ...markets, supervisor: { alive: false, at: null, reachable: false } })

    expect(screen.getByRole('alert')).toHaveTextContent(
      'Cannot reach Redis — nothing is known about the sessions',
    )
  })

  it('says how each market’s processes stand', () => {
    const asset = markets.assets[0]!
    const on = [onMarket({ state: 'starting' })]
    mountByAsset({
      ...markets,
      assets: [
        {
          ...asset,
          timeframes: [
            { timeframe: 'M1', processes: [live], restarting: true, setups: on },
            { timeframe: 'M5', processes: [], restarting: false, setups: on },
            {
              timeframe: 'M15',
              processes: [],
              restarting: false,
              setups: [onMarket({ setup_active: false, state: 'off' })],
            },
            {
              timeframe: 'H1',
              processes: [{ ...live, phase: null }],
              restarting: false,
              setups: [onMarket({ state: 'stopping' })],
            },
          ],
        },
      ],
    })

    const m1 = market('WIN M1')
    expect(m1.getByText('restarting…')).toBeInTheDocument()
    expect(m1.getByRole('button', { name: 'Restarting…' })).toBeDisabled()
    expect(market('WIN M5').getByText('not running')).toBeInTheDocument()
    expect(market('WIN M15').getByText('nothing switched on')).toBeInTheDocument()
    expect(market('WIN M15').getByText('setup switched off')).toBeInTheDocument()
    expect(market('WIN H1').getByText('starting · 1 process')).toBeInTheDocument()
    expect(market('WIN H1').getByText('stopping')).toBeInTheDocument()
  })

  it('opens a setup’s history on that asset only', () => {
    mountByAsset(markets)
    const h1 = market('WIN H1')

    fireEvent.click(h1.getByRole('button', { name: 'History' }))
    expect(h1.getByText('41')).toBeInTheDocument()
    expect(h1.queryByText('50')).not.toBeInTheDocument()

    fireEvent.click(h1.getByRole('button', { name: 'Hide the history' }))
    expect(h1.queryByText('41')).not.toBeInTheDocument()
  })

  it('says while the markets load, when they cannot be read, and when none is followed', () => {
    mountByAsset(undefined)
    expect(screen.getByText('Loading…')).toBeInTheDocument()

    mountByAsset({ ...markets, assets: [] })
    expect(screen.getByText('No market is followed yet.')).toBeInTheDocument()

    vi.mocked(useLiveMarkets).mockReturnValue({
      data: undefined,
      isPending: false,
      isError: true,
      error: new ApiError(500, 'the database is down'),
    } as never)
    renderWithProviders(<LiveSignals />)
    expect(screen.getByText('the database is down')).toBeInTheDocument()
  })

  it('keeps the reading by setup in its own tab', () => {
    mountByAsset(markets)

    fireEvent.click(screen.getByRole('tab', { name: 'By setup' }))

    expect(screen.getByRole('tab', { name: 'By setup' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('heading', { name: 'CHOCH BASE' })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'BOS M5' })).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'WIN M5' })).not.toBeInTheDocument()
  })
})
