import { fireEvent, screen, within } from '@testing-library/react'

import type { StrategiesPage, StrategyListItem } from '../api/types'
import { renderWithProviders } from '../test-utils'

const { state } = vi.hoisted(() => {
  const state: { data: StrategiesPage | undefined; isPending: boolean; isError: boolean } = {
    data: undefined,
    isPending: false,
    isError: false,
  }
  return { state }
})

vi.mock('../api/hooks', () => ({
  useStrategies: () => state,
}))

import { StrategyCatalog } from './StrategyCatalog'

function row(partial: Partial<StrategyListItem> & { name: string }): StrategyListItem {
  return {
    id: partial.name,
    version: 1,
    schema_version: '1.0.0',
    setup: null,
    runs: 0,
    created_at: '2026-09-01T12:30:00Z',
    ...partial,
  }
}

// The name never repeats its own setup, so a screen reading the setup out of the name looks
// exactly like a screen reading it out of the document — until one of these rows is inspected.
const ITEMS: StrategyListItem[] = [
  row({ id: 's1', name: 'Structure — CHoCH 56454', setup: 'mme9_breakout', runs: 12 }),
  row({ id: 's2', name: '9.1 sem filtro', setup: 'mme9_turn', version: 3 }),
  row({ id: 's3', name: 'Ponto Contínuo', setup: 'continuous_point', runs: 4 }),
]

function page(items: StrategyListItem[], total = items.length): StrategiesPage {
  return { total, limit: 200, offset: 0, items }
}

beforeEach(() => {
  state.data = page(ITEMS)
  state.isPending = false
  state.isError = false
})

afterEach(() => {
  vi.clearAllMocks()
})

describe('StrategyCatalog', () => {
  it('lists every saved strategy with the setup the document runs', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    const line = screen.getByRole('row', { name: /Structure — CHoCH 56454/ })
    // The evidence, not the caption: this row is *named* after a structure setup and *runs*
    // `mme9_breakout`. A screen deriving the setup from the name would print the wrong one here
    // and nowhere else.
    expect(within(line).getByText('mme9_breakout')).toBeInTheDocument()
    expect(screen.getAllByRole('row')).toHaveLength(4) // three strategies plus the header
  })

  it('links each row to the builder opened on that strategy', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    expect(screen.getByRole('link', { name: '9.1 sem filtro' })).toHaveAttribute(
      'href',
      '/strategies/s2',
    )
  })

  it('says "never run" instead of a zero', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    const never = screen.getByRole('row', { name: /9.1 sem filtro/ })
    expect(within(never).getByText('never run')).toBeInTheDocument()
    // And the row that *has* run still prints its count, so the branch above is a branch rather
    // than a column that never says a number at all.
    const ran = screen.getByRole('row', { name: /Ponto Contínuo/ })
    expect(within(ran).getByText('4')).toBeInTheDocument()
  })

  it('offers only the setups the shelf actually holds', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    const options = within(screen.getByLabelText('Setup')).getAllByRole('option')
    // "All" plus the three present. A filter built from the schema's full list of setups would
    // have many more, and every extra one empties the table when chosen.
    expect(options.map((option) => option.textContent)).toEqual([
      'All',
      'continuous_point',
      'mme9_breakout',
      'mme9_turn',
    ])
  })

  it('searches the setup, not only the name', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    // `mme9_breakout` appears in no name on this shelf.
    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'mme9_breakout' } })

    expect(screen.getByRole('link', { name: 'Structure — CHoCH 56454' })).toBeInTheDocument()
    expect(screen.queryByRole('link', { name: '9.1 sem filtro' })).not.toBeInTheDocument()
  })

  it('says so when a search leaves nothing', () => {
    renderWithProviders(<StrategyCatalog />, '/catalog')

    fireEvent.change(screen.getByLabelText('Search'), { target: { value: 'nothing here' } })

    // Different words from the empty-database case below: "you have none" and "none match what
    // you typed" send the reader to different places.
    expect(screen.getByText('No strategy matches that.')).toBeInTheDocument()
  })

  it('invites the builder when nothing is saved at all', () => {
    state.data = page([])
    renderWithProviders(<StrategyCatalog />, '/catalog')

    expect(screen.getByText(/Nothing saved yet/)).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'New backtest' })).toHaveAttribute('href', '/')
  })

  it('admits when it is holding fewer lineages than exist', () => {
    state.data = page(ITEMS, 250)
    renderWithProviders(<StrategyCatalog />, '/catalog')

    expect(screen.getByText(/250 saved, showing the newest 3/)).toBeInTheDocument()
  })

  it('says nothing about truncation when it has the whole shelf', () => {
    // The other half of the pair: without it, a screen that always printed the notice — or one
    // that compared the wrong two numbers — passes the case above.
    renderWithProviders(<StrategyCatalog />, '/catalog')

    expect(screen.queryByText(/saved, showing the newest/)).not.toBeInTheDocument()
  })

  it('reports a failure to load rather than an empty shelf', () => {
    state.data = undefined
    state.isError = true
    renderWithProviders(<StrategyCatalog />, '/catalog')

    // ⚠️ A read that failed must not print as "nothing saved yet": one invites building a
    // strategy and the other means the database was never asked.
    expect(screen.getByText('Could not load the catalogue.')).toBeInTheDocument()
    expect(screen.queryByText(/Nothing saved yet/)).not.toBeInTheDocument()
  })
})
