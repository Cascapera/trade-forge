import { fireEvent, screen, within } from '@testing-library/react'

import { ApiError } from '../api/client'
import type { CatalogEntry, CatalogPage } from '../api/types'
import { renderWithProviders } from '../test-utils'

const { create, remove, state } = vi.hoisted(() => {
  const state: {
    data: CatalogPage | undefined
    isPending: boolean
    isError: boolean
    createError: unknown
    removeError: unknown
  } = {
    data: undefined,
    isPending: false,
    isError: false,
    createError: null,
    removeError: null,
  }
  return { create: vi.fn(), remove: vi.fn(), state }
})

vi.mock('../api/hooks', () => ({
  useCatalog: () => ({ data: state.data, isPending: state.isPending, isError: state.isError }),
  useCreateCatalogEntry: () => ({
    mutate: create,
    isPending: false,
    isError: state.createError !== null,
    error: state.createError,
  }),
  useDeleteCatalogEntry: () => ({
    mutate: remove,
    isPending: false,
    isError: state.removeError !== null,
    error: state.removeError,
  }),
  // The picker asks the server what exists. Two rows, and ⚠️ neither name repeats its own
  // setup — a fixture where they matched would agree with a screen reading either one.
  useStrategies: () => ({
    data: {
      total: 2,
      limit: 200,
      offset: 0,
      items: [
        {
          id: 's1',
          name: 'MME9-20260910-172055',
          version: 1,
          schema_version: '1.0',
          setup: 'structure_choch',
          runs: 3,
          created_at: '2026-09-10T00:00:00Z',
        },
        {
          id: 's2',
          name: 'Hand-built',
          version: 1,
          schema_version: '1.0',
          setup: null,
          runs: 0,
          created_at: '2026-09-09T00:00:00Z',
        },
      ],
    },
    isPending: false,
  }),
}))

import { StrategyCatalog } from './StrategyCatalog'

/**
 * Put values on an axis, the way the control actually takes them.
 *
 * ⚠️ A **described** parameter renders a stepper, and typing into it is not the value until the
 * add button is pressed — `fireEvent.change` alone is a silent no-op, and the test then passes
 * or fails for the wrong reason. Copied in shape from the study's own tests, which met this
 * first; the undescribed fallback has no button, hence the query rather than a get.
 */
function setValues(axis: number, values: string): void {
  const label = `Parameter ${String(axis)} values`
  fireEvent.change(screen.getByLabelText(label), { target: { value: values } })
  const add = screen.queryByLabelText(`${label} add`)
  if (add !== null) fireEvent.click(add)
}

function entry(partial: Partial<CatalogEntry> & { name: string }): CatalogEntry {
  return {
    id: partial.name,
    description: null,
    strategy_id: 's1',
    strategy_name: 'MME9-20260910-172055',
    strategy_version: 1,
    setup: 'structure_choch',
    grid: {},
    points: 1,
    created_at: '2026-09-11T12:00:00Z',
    ...partial,
  }
}

const SHELF: CatalogEntry[] = [
  entry({
    id: 'e1',
    name: '9.1 sem filtro',
    description: 'A virada da média, sem nada por cima.',
  }),
  entry({
    id: 'e2',
    name: '9.1 varrido',
    grid: { 'setup.params.stop_buffer': [0.1, 0.2, 0.3] },
    points: 3,
  }),
  entry({ id: 'e3', name: 'Ponto Contínuo', setup: 'continuous_point' }),
]

function page(items: CatalogEntry[]): CatalogPage {
  return { total: items.length, items }
}

beforeEach(() => {
  state.data = page(SHELF)
  state.isPending = false
  state.isError = false
  state.createError = null
  state.removeError = null
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('the shelf', () => {
  it('shows the label somebody wrote beside the document it points at', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    // ⚠️ Both, and that is the point of the table existing. The label is readable and the
    // document's name is traceable; a screen showing only one of them is showing either a name
    // nobody can trace or an identifier nobody can read.
    const row = screen.getByText('9.1 sem filtro').closest('li') as HTMLElement

    expect(within(row).getByText('9.1 sem filtro')).toBeInTheDocument()
    // ⚠️ Scoped to the row, because three entries here point at one strategy — which is the
    // shape this table exists to allow, and an unscoped query would be ambiguous by design.
    expect(within(row).getByRole('link', { name: 'MME9-20260910-172055' })).toHaveAttribute(
      'href',
      '/strategies/s1',
    )
  })

  it('says one backtest in words and a sweep as a count', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    const plain = screen.getByText('9.1 sem filtro').closest('li')
    const swept = screen.getByText('9.1 varrido').closest('li')

    // A bare `1` beside a label reads as a count of results until it says what it counts.
    expect(within(plain as HTMLElement).getByText('one backtest')).toBeInTheDocument()
    expect(within(swept as HTMLElement).getByText('3 backtests')).toBeInTheDocument()
  })

  it('prints the sweep, and nothing at all when there is none', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    expect(screen.getByText('stop_buffer=0.1, 0.2, 0.3')).toBeInTheDocument()
    // The other half: an entry with no axes shows no caption rather than a bare separator.
    const plain = screen.getByText('9.1 sem filtro').closest('li')
    expect(within(plain as HTMLElement).queryByText(/=/)).not.toBeInTheDocument()
  })

  it('shows a description only when somebody wrote one', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    expect(screen.getByText('A virada da média, sem nada por cima.')).toBeInTheDocument()
    const swept = screen.getByText('9.1 varrido').closest('li')
    // ⚠️ `null` and `''` are different facts and the column keeps them apart. This row has
    // `null`, so there is no empty paragraph holding space open.
    expect(within(swept as HTMLElement).queryByText(/virada/)).not.toBeInTheDocument()
  })

  it('searches the setup, not only the name', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    // `continuous_point` appears in no entry name on this shelf.
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'continuous_point' } })

    expect(screen.getByText('Ponto Contínuo')).toBeInTheDocument()
    expect(screen.queryByText('9.1 sem filtro')).not.toBeInTheDocument()
  })

  it('tells an empty shelf apart from a read that failed', () => {
    state.data = page([])
    const empty = renderWithProviders(<StrategyCatalog />, '/catalog')
    expect(screen.getByText(/The shelf is empty/)).toBeInTheDocument()
    empty.unmount()

    // ⚠️ A failed read must not print as "the shelf is empty": one invites adding an entry and
    // the other means the database was never asked.
    state.data = undefined
    state.isError = true
    renderWithProviders(<StrategyCatalog />, '/catalog')
    expect(screen.getByText('Could not load the catalogue.')).toBeInTheDocument()
    expect(screen.queryByText(/The shelf is empty/)).not.toBeInTheDocument()
  })
})

describe('adding an entry', () => {
  function openForm(): void {
    fireEvent.click(screen.getByRole('button', { name: 'Add to the catalogue' }))
  }

  it('will not save without a strategy and a name', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')
    openForm()

    const save = screen.getByRole('button', { name: 'Save to the catalogue' })
    expect(save).toBeDisabled()
    expect(screen.getByText('Choose a strategy first.')).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 's1' } })
    // The reason changes rather than disappearing: one blocker at a time, in a fixed order, so
    // the form does not argue back while it is being filled in.
    expect(screen.getByText('Give it a name you will recognise.')).toBeInTheDocument()
    expect(save).toBeDisabled()
  })

  it('sends the name, the strategy and an omitted description', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')
    openForm()

    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 's1' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: '  9.1 original  ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save to the catalogue' }))

    expect(create).toHaveBeenCalledTimes(1)
    const [body] = create.mock.calls[0] as [Record<string, unknown>]
    // Trimmed, because a name with a trailing space is a different row under a unique index.
    expect(body.name).toBe('9.1 original')
    expect(body.strategy_id).toBe('s1')
    expect(body.grid).toEqual({})
    // ⚠️ **Absent, not empty.** `null` means nobody wrote a description; `''` means somebody
    // wrote nothing, and the list reads the difference when deciding to show a subtitle.
    expect('description' in body).toBe(false)
  })

  it('sends a description when there is one', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')
    openForm()

    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 's1' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'named' } })
    fireEvent.change(screen.getByLabelText('Description'), { target: { value: ' for testing ' } })
    fireEvent.click(screen.getByRole('button', { name: 'Save to the catalogue' }))

    const [body] = create.mock.calls[0] as [Record<string, unknown>]
    expect(body.description).toBe('for testing')
  })

  it('builds the grid from the axes and counts the sweep before saving', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')
    openForm()

    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 's1' } })
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'swept' } })
    fireEvent.change(screen.getByLabelText('Parameter 1'), {
      target: { value: 'setup.params.stop_buffer' },
    })
    setValues(1, '0.1, 0.2, 0.3')

    // The product, said before anything is sent — the number that decides whether a sweep is a
    // click or an afternoon.
    expect(screen.getByText('3 backtests per sweep')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Save to the catalogue' }))
    const [body] = create.mock.calls[0] as [{ grid: Record<string, unknown[]> }]
    expect(body.grid).toEqual({ 'setup.params.stop_buffer': [0.1, 0.2, 0.3] })
  })

  it('says one backtest per sweep while there are no axes', () => {
    // The other half of the pair above. Without it, a screen that always printed a count would
    // pass — and the empty product being 1 rather than 0 is the thing worth pinning.
    renderWithProviders(<StrategyCatalog />, '/catalog')
    openForm()

    expect(screen.getByText('One backtest per sweep')).toBeInTheDocument()
  })

  it('shows the server sentence when the name is taken', () => {
    state.createError = new ApiError(409, "a catalogue entry named '9.1 sem filtro' already exists")
    renderWithProviders(<StrategyCatalog />, '/catalog')
    openForm()

    // The server's own words, through the shared reader — not "API error 409".
    expect(screen.getByText(/already exists/)).toBeInTheDocument()
  })
})

describe('removing an entry', () => {
  it('asks first, and says what it does not remove', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)

    expect(remove).not.toHaveBeenCalled()
    // ⚠️ Said out loud on the screen, because the two wrong beliefs about this button send a
    // reader in opposite directions.
    expect(screen.getByText('Remove the label? The strategy and its runs stay.')).toBeInTheDocument()
  })

  it('removes the entry it named once confirmed', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)
    fireEvent.click(screen.getByRole('button', { name: 'Remove 9.1 sem filtro' }))

    // The id, not the name — the label is what the button says, the id is what the API takes.
    expect(remove).toHaveBeenCalledWith('e1')
  })

  it('keeps the entry when the second thought wins', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    fireEvent.click(screen.getAllByRole('button', { name: 'Remove' })[0]!)
    fireEvent.click(screen.getByRole('button', { name: 'Keep it' }))

    expect(remove).not.toHaveBeenCalled()
    expect(screen.getAllByRole('button', { name: 'Remove' })).toHaveLength(3)
  })
})
