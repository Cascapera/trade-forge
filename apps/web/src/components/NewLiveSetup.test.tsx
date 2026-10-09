import { SETUP_TYPES } from '@tradeforge/schema'
import { fireEvent, screen } from '@testing-library/react'

import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useInstruments: vi.fn(),
  useRegisterLiveSetup: vi.fn(),
}))

// The picker has its own tests; here it is a button per market that toggles it.
vi.mock('./SymbolPicker', () => ({
  SymbolPicker: (props: { chosen: string[]; onChange: (next: string[]) => void }) => (
    <div>
      {['WIN', 'EURUSD', 'NEVER'].map((symbol) => (
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
          pick {symbol}
        </button>
      ))}
    </div>
  ),
}))

import { useInstruments, useRegisterLiveSetup } from '../api/hooks'
import { NewLiveSetup } from './NewLiveSetup'

interface Sent {
  definition: {
    name: string
    timeframe: string
    setup: { type: string; params: Record<string, unknown> }
    exit: { take_profit: unknown }
    risk: unknown
  }
  timeframe: string
  instrument_ids: string[]
  no_target_r: string
}

function mount(): ReturnType<typeof vi.fn> {
  const mutate = vi.fn()
  vi.mocked(useInstruments).mockReturnValue({
    data: [
      { id: 'win', symbol: 'WIN', broker: 'xp' },
      { id: 'eur', symbol: 'EURUSD', broker: 'activtrades' },
    ],
  } as never)
  vi.mocked(useRegisterLiveSetup).mockReturnValue({
    mutate,
    isPending: false,
    error: null,
  } as never)
  renderWithProviders(<NewLiveSetup onClose={vi.fn()} />)
  return mutate
}

function sent(mutate: ReturnType<typeof vi.fn>): Sent {
  const [call] = mutate.mock.calls as [[Sent]]
  return call[0]
}

describe('NewLiveSetup', () => {
  it('waits for a name and a market before it can start', () => {
    mount()
    const start = screen.getByRole('button', { name: 'Start live' })
    expect(start).toBeDisabled()

    fireEvent.change(screen.getByLabelText('Name'), {
      target: { value: 'WIN H1 test' },
    })
    expect(start).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: 'pick WIN' }))
    expect(start).toBeEnabled()
  })

  it('starts a setup with its chart, its parameters and its markets', () => {
    const mutate = mount()

    fireEvent.change(screen.getByLabelText('Name'), {
      target: { value: 'WIN H1 test' },
    })
    fireEvent.change(screen.getByLabelText('Chart'), {
      target: { value: 'M15' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'pick WIN' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start live' }))

    const body = sent(mutate)
    expect(body.timeframe).toBe('M15')
    expect(body.definition.timeframe).toBe('M15')
    expect(body.definition.name).toBe('WIN H1 test')
    expect(body.definition.setup.type).toBe(SETUP_TYPES[0])
    expect(body.instrument_ids).toEqual(['win'])
    expect(body.no_target_r).toBe('5')
    expect(body.definition.risk).toEqual({
      sizing: { type: 'percent_risk', params: { percent: 1 } },
    })
  })

  it('refuses a market never collected', () => {
    mount()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'pick NEVER' }))

    expect(screen.getByText(/NEVER/, { selector: 'p' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Start live' })).toBeDisabled()
  })

  it('resets the parameters when the setup changes', () => {
    const mutate = mount()
    const other = SETUP_TYPES.find((one) => one !== SETUP_TYPES[0])
    if (other !== undefined) {
      fireEvent.change(screen.getByLabelText('Setup'), {
        target: { value: other },
      })
    }
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
    fireEvent.click(screen.getByRole('button', { name: 'pick EURUSD' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start live' }))

    expect(sent(mutate).definition.setup.type).toBe(other ?? SETUP_TYPES[0])
  })

  it('sends the risk and the no-target R typed, and unticks a market', () => {
    const mutate = mount()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
    fireEvent.change(screen.getByLabelText('risk per trade (%)'), {
      target: { value: '0.5' },
    })
    fireEvent.change(screen.getByLabelText('no target: close at (R)'), {
      target: { value: '3' },
    })
    fireEvent.click(screen.getByLabelText('take profit (R) off'))
    fireEvent.click(screen.getByRole('button', { name: 'pick WIN' }))
    fireEvent.click(screen.getByRole('button', { name: 'pick EURUSD' }))
    fireEvent.click(screen.getByRole('button', { name: 'pick WIN' }))
    fireEvent.click(screen.getByRole('button', { name: 'Start live' }))

    const body = sent(mutate)
    expect(body.instrument_ids).toEqual(['eur'])
    expect(body.no_target_r).toBe('3')
    expect(body.definition.exit.take_profit).toBeNull()
    expect(body.definition.risk).toEqual({
      sizing: { type: 'percent_risk', params: { percent: 0.5 } },
    })
  })

  it('cancels, and shows why a start was refused', () => {
    const close = vi.fn()
    vi.mocked(useInstruments).mockReturnValue({ data: [] } as never)
    vi.mocked(useRegisterLiveSetup).mockReturnValue({
      mutate: vi.fn(),
      isPending: false,
      error: new Error('already followed on this market'),
    } as never)
    renderWithProviders(<NewLiveSetup onClose={close} />)

    expect(screen.getByText('already followed on this market')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(close).toHaveBeenCalled()
  })
})
