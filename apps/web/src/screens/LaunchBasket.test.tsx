import { fireEvent, screen } from '@testing-library/react'

import { ApiError } from '../api/client'
import type { CreateBasketRequest } from '../api/types'
import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

const { mutate, state } = vi.hoisted(() => {
  // Annotated rather than asserted: the mutation's error is whatever the client threw, and the
  // test needs to put an `ApiError` in here later.
  const state: { isError: boolean; error: unknown } = { isError: false, error: null }
  return { mutate: vi.fn(), state }
})

vi.mock('../api/hooks', () => ({
  useInstruments: () => ({
    data: [
      { id: 'i1', symbol: 'EURUSD', default_spread_points: '8.0000000000' },
      { id: 'i2', symbol: 'GBPUSD', default_spread_points: '9.0000000000' },
      { id: 'i3', symbol: 'US500', default_spread_points: null },
      { id: 'i4', symbol: 'XAUUSD', default_spread_points: null },
    ],
  }),
  useCreateBasket: () => ({ mutate, isPending: false, ...state }),
}))

import { LaunchBasket } from './LaunchBasket'

function pick(name: string): void {
  fireEvent.click(screen.getByRole('checkbox', { name }))
}

beforeEach(() => {
  state.isError = false
  state.error = null
})

afterEach(() => {
  vi.clearAllMocks()
  useSession.getState().clear()
})

describe('LaunchBasket', () => {
  it('asks the user to build a strategy first when none is selected', () => {
    renderWithProviders(<LaunchBasket />)
    expect(screen.getByText(/build and run a strategy first/i)).toBeInTheDocument()
  })

  it('refuses a basket of one market, saying why rather than disabling in silence', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')

    expect(screen.getByText(/one market is a backtest, not a basket/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run 1 market$/i })).toBeDisabled()
  })

  it('enqueues every chosen market in one call, with no cost model of its own', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    mutate.mockImplementation(
      (_payload: unknown, options: { onSuccess: (b: { id: string; runs: unknown[] }) => void }) => {
        options.onSuccess({ id: 'k1', runs: [{}, {}, {}] })
      },
    )
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')
    pick('GBPUSD, 9 ticks')
    pick('US500, no spread measured')
    fireEvent.change(screen.getByLabelText('timeframe'), { target: { value: 'H4' } })
    fireEvent.change(screen.getByLabelText('capital'), { target: { value: '5000' } })
    fireEvent.change(screen.getByLabelText('from'), { target: { value: '2023-01-01' } })
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2023-06-01' } })
    fireEvent.click(screen.getByRole('button', { name: /run 3 markets/i }))

    expect(mutate).toHaveBeenCalledTimes(1)
    const payload = mutate.mock.calls[0]?.[0] as CreateBasketRequest
    expect(payload.symbols).toEqual(['EURUSD', 'GBPUSD', 'US500'])
    expect(payload.timeframe).toBe('H4')
    expect(payload.initial_capital).toBe('5000')
    // ⚠️ One call for three markets, and no cost model in it: the server charges each instrument
    // its own measured spread. A figure sent from here would be applied across instruments whose
    // tick sizes differ by three orders of magnitude.
    expect(payload).not.toHaveProperty('cost_model')
  })

  it('remembers the basket it launched, so the nav can lead back to it', () => {
    useSession.getState().setStrategy('s1', 'Ponto Contínuo')
    mutate.mockImplementation(
      (_payload: unknown, options: { onSuccess: (b: { id: string; runs: unknown[] }) => void }) => {
        options.onSuccess({ id: 'k1', runs: [{}, {}] })
      },
    )
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')
    pick('GBPUSD, 9 ticks')
    fireEvent.click(screen.getByRole('button', { name: /run 2 markets/i }))

    // There is no `GET /baskets`, so this thread is the only way back that is not a pasted URL.
    expect(useSession.getState().basketId).toBe('k1')
    expect(useSession.getState().basketLabel).toBe('Ponto Contínuo · 2 markets')
  })

  it('warns which chosen markets will run uncosted, by name', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)

    pick('EURUSD, 8 ticks')
    // Nothing to warn about yet: every chosen market has a measured spread.
    expect(screen.queryByText(/no measured spread/i)).not.toBeInTheDocument()

    pick('US500, no spread measured')

    // Named, not counted: the reader's next move is to go and catalogue that symbol, and "one of
    // them" does not say which.
    const warning = screen.getByRole('status')
    expect(warning).toHaveTextContent('US500')
    expect(warning).toHaveTextContent(/upper bound/i)
    expect(warning).not.toHaveTextContent('EURUSD')
  })

  it('reads as English when more than one market is uncosted', () => {
    // The singular and plural wordings are different sentences, not a suffix — "has"/"have",
    // "that run"/"those runs", "its result is an upper bound"/"their results are upper bounds".
    // A branch nobody renders is a branch nobody has read.
    useSession.getState().setStrategy('s1', 'MA cross')
    renderWithProviders(<LaunchBasket />)

    pick('US500, no spread measured')
    expect(screen.getByRole('status')).toHaveTextContent(
      /US500 has no measured spread, so that run will charge nothing/,
    )

    pick('XAUUSD, no spread measured')
    expect(screen.getByRole('status')).toHaveTextContent(
      /US500, XAUUSD have no measured spread, so those runs will charge nothing/,
    )
    expect(screen.getByRole('status')).toHaveTextContent(/their results are upper bounds/)
  })

  it('shows every unknown symbol the API named, not a house message', () => {
    useSession.getState().setStrategy('s1', 'MA cross')
    state.isError = true
    state.error = new ApiError(422, 'unknown symbols: NOPE, ALSONOPE')
    renderWithProviders(<LaunchBasket />)

    expect(screen.getByText('unknown symbols: NOPE, ALSONOPE')).toBeInTheDocument()
  })
})
