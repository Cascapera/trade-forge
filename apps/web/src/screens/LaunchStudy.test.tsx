import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

import { LaunchStudy } from './LaunchStudy'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: {
      ...actual.api,
      listInstruments: vi.fn(),
      createStudy: vi.fn(),
    },
  }
})

const listInstruments = vi.mocked(api.listInstruments)
const createStudy = vi.mocked(api.createStudy)

function instrument(symbol: string) {
  return {
    id: symbol,
    symbol,
    name: symbol,
    asset_class: 'stock',
    currency_quote: 'USD',
    currency_base: null,
    tick_size: '0.01',
    tick_value: '0.01',
    contract_size: '1',
    digits: 2,
    default_spread_points: null,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  listInstruments.mockResolvedValue([instrument('AAPL')])
  useSession.setState({ strategyId: 'strategy-1', strategyName: 'MME9' })
})

describe('LaunchStudy', () => {
  it('asks for a strategy first, because a study varies one that already exists', () => {
    useSession.setState({ strategyId: null, strategyName: null })

    renderWithProviders(<LaunchStudy />)

    expect(screen.getByText(/Build and run a strategy first/)).toBeInTheDocument()
  })

  it('reports the size of the grid as it is typed, and multiplies rather than adds', () => {
    // ⚠️ The reason this number is on screen at all. Three by four is twelve backtests, not
    // seven, and the moment someone needs to know that is *while* adding the second axis — not
    // after launching a study four times the size they meant.
    renderWithProviders(<LaunchStudy />)

    fireEvent.change(screen.getByLabelText('Parameter 1 path'), {
      target: { value: 'setup.params.period' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 values'), {
      target: { value: '5, 9, 20' },
    })
    expect(screen.getByRole('status')).toHaveTextContent('3 combinations, so 3 backtests.')

    fireEvent.click(screen.getByRole('button', { name: 'Add a parameter' }))
    fireEvent.change(screen.getByLabelText('Parameter 2 path'), {
      target: { value: 'setup.params.rr' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 2 values'), {
      target: { value: '1, 2, 3, 4' },
    })

    expect(screen.getByRole('status')).toHaveTextContent('12 combinations, so 12 backtests.')
  })

  it('says nothing to run rather than one combination on an empty form', async () => {
    // An empty product is 1, and "1 combination" on a blank form reads as ready to launch.
    renderWithProviders(<LaunchStudy />)

    expect(await screen.findByRole('status')).toHaveTextContent('Nothing to run yet.')
  })

  it('refuses a grid over the cap and says how big it is', async () => {
    renderWithProviders(<LaunchStudy />)

    // ⚠️ The option, not the label. `findByLabelText('Market')` resolves the moment the select
    // exists — with only the placeholder in it — and firing a change for a value that has no
    // option is a no-op that leaves the field empty and says nothing. Waiting for the option is
    // waiting for the instruments query.
    await screen.findByRole('option', { name: 'AAPL' })
    fireEvent.change(screen.getByLabelText('Market'), {
      target: { value: 'AAPL' },
    })
    fireEvent.change(screen.getByLabelText('From'), {
      target: { value: '2024-01-01' },
    })
    fireEvent.change(screen.getByLabelText('To'), {
      target: { value: '2025-01-01' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 path'), {
      target: { value: 'a' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 values'), {
      target: {
        value: Array.from({ length: 26 }, (_, at) => at + 1).join(','),
      },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add a parameter' }))
    fireEvent.change(screen.getByLabelText('Parameter 2 path'), {
      target: { value: 'b' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 2 values'), {
      target: {
        value: Array.from({ length: 20 }, (_, at) => at + 1).join(','),
      },
    })

    expect(screen.getByText(/520 combinations, over the 500/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run the study' })).toBeDisabled()
  })

  it('sends the grid with its values typed, not as strings', async () => {
    // `"5"` is not a period the DSL accepts, and `long` is not a number. The form is the only
    // place that knows which is which, because it is the only place that saw the text.
    createStudy.mockResolvedValue({ id: 'study-1', points: [] })
    renderWithProviders(<LaunchStudy />)

    // ⚠️ The option, not the label. `findByLabelText('Market')` resolves the moment the select
    // exists — with only the placeholder in it — and firing a change for a value that has no
    // option is a no-op that leaves the field empty and says nothing. Waiting for the option is
    // waiting for the instruments query.
    await screen.findByRole('option', { name: 'AAPL' })
    fireEvent.change(screen.getByLabelText('Market'), {
      target: { value: 'AAPL' },
    })
    fireEvent.change(screen.getByLabelText('From'), {
      target: { value: '2024-01-01' },
    })
    fireEvent.change(screen.getByLabelText('To'), {
      target: { value: '2025-01-01' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 path'), {
      target: { value: 'setup.params.period' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 values'), {
      target: { value: '5, 9' },
    })
    fireEvent.click(screen.getByRole('button', { name: 'Add a parameter' }))
    fireEvent.change(screen.getByLabelText('Parameter 2 path'), {
      target: { value: 'setup.params.side' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 2 values'), {
      target: { value: 'long, short' },
    })

    fireEvent.click(screen.getByRole('button', { name: 'Run the study' }))

    await waitFor(() => {
      expect(createStudy).toHaveBeenCalledWith(
        expect.objectContaining({
          strategy_id: 'strategy-1',
          symbol: 'AAPL',
          grid: {
            'setup.params.period': [5, 9],
            'setup.params.side': ['long', 'short'],
          },
          cost_model: { type: 'none' },
        }),
      )
    })
  })

  it('warns that everything a study measures is in-sample', () => {
    // The one sentence on this screen that is about what a study cannot do. Without it the form
    // reads as a tool for finding the parameters to trade, which is exactly the wrong reading.
    renderWithProviders(<LaunchStudy />)

    expect(screen.getByText(/measured on the same data it searched/)).toBeInTheDocument()
  })

  it('carries a spread through as the cost every point pays', () => {
    // One market, so one cost — and it has to be one, or the points are not comparable to each
    // other, which is the only thing a study is for.
    createStudy.mockResolvedValue({ id: 'study-1', points: [] })
    renderWithProviders(<LaunchStudy />)

    return (async () => {
      await screen.findByRole('option', { name: 'AAPL' })
      fireEvent.change(screen.getByLabelText('Market'), { target: { value: 'AAPL' } })
      fireEvent.change(screen.getByLabelText('From'), { target: { value: '2024-01-01' } })
      fireEvent.change(screen.getByLabelText('To'), { target: { value: '2025-01-01' } })
      fireEvent.change(screen.getByLabelText(/Spread \(ticks\)/), { target: { value: '8' } })
      fireEvent.change(screen.getByLabelText('Parameter 1 path'), {
        target: { value: 'setup.params.period' },
      })
      fireEvent.change(screen.getByLabelText('Parameter 1 values'), { target: { value: '5, 9' } })
      fireEvent.click(screen.getByRole('button', { name: 'Run the study' }))

      await waitFor(() => {
        expect(createStudy).toHaveBeenCalledWith(
          expect.objectContaining({ cost_model: { type: 'spread', spread_points: '8' } }),
        )
      })
    })()
  })

  it('reports a refusal from the server instead of failing silently', async () => {
    createStudy.mockRejectedValue(new Error('this grid expands to 900 combinations'))
    renderWithProviders(<LaunchStudy />)

    await screen.findByRole('option', { name: 'AAPL' })
    fireEvent.change(screen.getByLabelText('Market'), { target: { value: 'AAPL' } })
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2024-01-01' } })
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2025-01-01' } })
    fireEvent.change(screen.getByLabelText('Parameter 1 path'), {
      target: { value: 'setup.params.period' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 values'), { target: { value: '5, 9' } })
    fireEvent.click(screen.getByRole('button', { name: 'Run the study' }))

    // ⚠️ The server's own words, not a generic apology: the refusals it sends are specific
    // ("nothing at 'setup.params.periodd'", "expands to 900 combinations"), and each of them is
    // the one sentence that tells the reader what to change.
    expect(await screen.findByText(/900 combinations/)).toBeInTheDocument()
  })
})
