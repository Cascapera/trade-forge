import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
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
      listStrategies: vi.fn(),
      createStudy: vi.fn(),
    },
  }
})

const listInstruments = vi.mocked(api.listInstruments)
const createStudy = vi.mocked(api.createStudy)
const listStrategies = vi.mocked(api.listStrategies)

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
  listStrategies.mockResolvedValue({
    total: 1,
    limit: 200,
    offset: 0,
    items: [
      {
        id: 'strategy-1',
        name: 'MME9',
        version: 1,
        schema_version: '1.0',
        setup: 'mme9_breakout',
        runs: 3,
        created_at: '2024-01-01T00:00:00Z',
      },
    ],
  })
  useSession.setState({ strategyId: 'strategy-1', strategyName: 'MME9' })
})

describe('LaunchStudy', () => {
  it('offers the strategies that exist rather than demanding one built just now', async () => {
    // ⚠️ This screen used to *refuse to open* unless a strategy had been created since the last
    // reload, because there was no way to ask the server what existed — for a database holding
    // forty-five of them. The picker is what `GET /strategies` was missing for.
    useSession.setState({ strategyId: null, strategyName: null })

    renderWithProviders(<LaunchStudy />)

    expect(await screen.findByRole('option', { name: /MME9 · mme9_breakout/ })).toBeInTheDocument()
    expect(screen.queryByText(/Build and run a strategy first/)).not.toBeInTheDocument()
  })

  it('will not launch until a strategy is chosen, and says so', async () => {
    useSession.setState({ strategyId: null, strategyName: null })

    renderWithProviders(<LaunchStudy />)
    await screen.findByRole('option', { name: /MME9/ })
    fireEvent.change(screen.getByLabelText('Parameter 1 path'), {
      target: { value: 'setup.params.period' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 values'), { target: { value: '5, 9' } })

    expect(screen.getByText('Choose a strategy.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Run the study' })).toBeDisabled()
  })

  it('shows what each strategy actually runs, because a name can lie', async () => {
    // Not hypothetical: this project's database holds `Structure — CHoCH 56454`, which runs
    // `mme9_breakout`. A name is typed by a person; a setup is executed by the engine.
    listStrategies.mockResolvedValue({
      total: 1,
      limit: 200,
      offset: 0,
      items: [
        {
          id: 'liar',
          name: 'Structure - CHoCH 56454',
          version: 1,
          schema_version: '1.0',
          setup: 'mme9_breakout',
          runs: 1,
          created_at: '2024-01-01T00:00:00Z',
        },
      ],
    })

    renderWithProviders(<LaunchStudy />)

    expect(
      await screen.findByRole('option', { name: /Structure - CHoCH 56454 · mme9_breakout/ }),
    ).toBeInTheDocument()
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

  it('reports the server own words, not the status code it came with', async () => {
    // ⚠️ `ApiError.message` is built from the status alone — "API error 422" — and that is what
    // the screen used to show. Reported from the screen exactly like that, with the reason the
    // server had actually sent sitting unread in `detail`.
    createStudy.mockRejectedValue(
      new ApiError(422, 'this grid expands to 900 combinations, over the 500 a study will run'),
    )
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

  it('offers the parameters of the strategy that was chosen, not a list to remember', async () => {
    // ⚠️ Before this the left field was free text and you had to know `setup.params.period` by
    // heart — reported from the screen as "no parameters, what do I fill in and how". The
    // options are **derived** from the JSON Schema the DSL generates, so nothing here is a
    // second copy of the parameter list.
    renderWithProviders(<LaunchStudy />)

    await screen.findByRole('option', { name: /MME9 · mme9_breakout/ })
    fireEvent.change(screen.getByLabelText(/Strategy/), { target: { value: 'strategy-1' } })

    const parameter = screen.getByLabelText('Parameter 1')
    expect(within(parameter).getByRole('option', { name: 'period' })).toBeInTheDocument()
    expect(within(parameter).getByRole('option', { name: 'breakeven_at_r' })).toBeInTheDocument()
  })

  it('says how to fill the values, in the parameter own bounds', async () => {
    renderWithProviders(<LaunchStudy />)

    await screen.findByRole('option', { name: /MME9/ })
    fireEvent.change(screen.getByLabelText(/Strategy/), { target: { value: 'strategy-1' } })
    fireEvent.change(screen.getByLabelText('Parameter 1'), {
      target: { value: 'setup.params.period' },
    })

    // The sentence comes from the schema's own minimum and maximum, so it tightens on its own
    // the day a bound does.
    expect(
      screen.getByText('whole numbers at least 1 and at most 1000, separated by commas'),
    ).toBeInTheDocument()
  })

  it('offers a line of values that will actually run', async () => {
    renderWithProviders(<LaunchStudy />)

    await screen.findByRole('option', { name: /MME9/ })
    fireEvent.change(screen.getByLabelText(/Strategy/), { target: { value: 'strategy-1' } })
    fireEvent.change(screen.getByLabelText('Parameter 1'), {
      target: { value: 'setup.params.period' },
    })
    fireEvent.click(screen.getByRole('button', { name: /use 4, 9, 14/ }))

    expect(screen.getByLabelText('Parameter 1 values')).toHaveValue('4, 9, 14')
    expect(screen.getByRole('status')).toHaveTextContent('3 combinations')
  })

  it('clears the axes when the strategy changes, so no path points at the old document', async () => {
    // ⚠️ Keeping them would leave `setup.params.period` selected against a structure setup that
    // has no period — refused by the server, but only after the reader had filled in values for
    // a parameter that was never going to exist.
    listStrategies.mockResolvedValue({
      total: 2,
      limit: 200,
      offset: 0,
      items: [
        {
          id: 'strategy-1',
          name: 'MME9',
          version: 1,
          schema_version: '1.0',
          setup: 'mme9_breakout',
          runs: 3,
          created_at: '2024-01-01T00:00:00Z',
        },
        {
          id: 'strategy-2',
          name: 'CHoCH',
          version: 1,
          schema_version: '1.0',
          setup: 'structure_choch',
          runs: 1,
          created_at: '2024-01-01T00:00:00Z',
        },
      ],
    })

    renderWithProviders(<LaunchStudy />)
    await screen.findByRole('option', { name: /MME9/ })
    fireEvent.change(screen.getByLabelText(/Strategy/), { target: { value: 'strategy-1' } })
    fireEvent.change(screen.getByLabelText('Parameter 1'), {
      target: { value: 'setup.params.period' },
    })
    fireEvent.change(screen.getByLabelText('Parameter 1 values'), { target: { value: '5, 9' } })

    fireEvent.change(screen.getByLabelText(/Strategy/), { target: { value: 'strategy-2' } })

    expect(screen.getByLabelText('Parameter 1')).toHaveValue('')
    expect(screen.getByLabelText('Parameter 1 values')).toHaveValue('')
    // And the options are the *new* setup's.
    const parameter = screen.getByLabelText('Parameter 1')
    expect(within(parameter).getByRole('option', { name: 'allow_secondary' })).toBeInTheDocument()
    expect(within(parameter).queryByRole('option', { name: 'period' })).not.toBeInTheDocument()
  })
})
