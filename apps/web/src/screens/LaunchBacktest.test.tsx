import { fireEvent, screen } from '@testing-library/react'

import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

const { mutate } = vi.hoisted(() => ({ mutate: vi.fn() }))

vi.mock('../api/hooks', () => ({
  useInstruments: () => ({ data: [{ id: 'i1', symbol: 'EURUSD' }] }),
  // The symbol field is a combobox over the broker's catalogue now, so it fetches. Stubbed
  // with an empty snapshot: these tests type a ticker rather than picking from the list, which
  // is the path that matters to them, and a real query here would be a network call in jsdom.
  useSymbolSearch: () => ({ data: { symbols: [], snapshot: null } }),
  useSyncSymbols: () => ({ mutate: () => undefined, isPending: false }),
  // The symbol field now also asks how much history the pair has. Stubbed as "never probed",
  // which is the state these tests are in and the one that renders the least.
  useSymbolHistory: () => ({ data: undefined, error: null }),
  useProbeSymbol: () => ({ mutate: () => undefined, isPending: false, isSuccess: false }),
  useCreateBacktest: () => ({ mutate, isPending: false, isError: false }),
}))

import { LaunchBacktest } from './LaunchBacktest'

afterEach(() => {
  vi.clearAllMocks()
  useSession.getState().clear()
})

describe('LaunchBacktest', () => {
  it('asks the user to build a strategy first when none is selected', () => {
    renderWithProviders(<LaunchBacktest />)
    expect(screen.getByText(/build and run a strategy first/i)).toBeInTheDocument()
  })

  it('enqueues a backtest with a spread cost model', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    mutate.mockImplementation(
      (_payload: unknown, options: { onSuccess: (b: { id: string }) => void }) => {
        options.onSuccess({ id: 'b1' })
      },
    )
    renderWithProviders(<LaunchBacktest />)

    // Touch every field so their handlers run.
    fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: 'EURUSD' } })
    fireEvent.change(screen.getByLabelText('timeframe'), { target: { value: 'H4' } })
    fireEvent.change(screen.getByLabelText('capital'), { target: { value: '5000' } })
    fireEvent.change(screen.getByLabelText('from'), { target: { value: '2023-01-01' } })
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2023-06-01' } })
    fireEvent.change(screen.getByLabelText('cost model'), { target: { value: 'spread' } })
    fireEvent.change(screen.getByLabelText('spread points'), { target: { value: '15' } })
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    expect(mutate).toHaveBeenCalledTimes(1)
    const payload = mutate.mock.calls[0]?.[0] as { symbol: string; cost_model: unknown }
    expect(payload.symbol).toBe('EURUSD')
    expect(payload.cost_model).toEqual({ type: 'spread', spread_points: 15 })
  })
})
