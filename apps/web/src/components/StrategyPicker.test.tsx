import { fireEvent, screen } from '@testing-library/react'

import type { StrategyListItem } from '../api/types'
import { renderWithProviders } from '../test-utils'

const items: StrategyListItem[] = [
  {
    id: 'base',
    name: 'SCHOCH-20260922-222429',
    version: 1,
    schema_version: '1.0',
    setup: 'structure_choch',
    catalog: ['CHOCH COMPLETO', 'CHOCH COMPLETO sem HTF'],
    runs: 0,
    created_at: '2026-09-22T00:00:00Z',
  },
  {
    id: 'draft',
    name: 'MM9 CASCA',
    version: 1,
    schema_version: '1.0',
    setup: 'mme9_breakout',
    catalog: [],
    runs: 2,
    created_at: '2026-09-20T00:00:00Z',
  },
]

vi.mock('../api/hooks', () => ({
  useStrategies: () => ({
    data: { total: items.length, limit: 200, offset: 0, items },
    isPending: false,
    isError: false,
  }),
}))

import { labelOf } from '../strategy/catalogue'

import { StrategyPicker } from './StrategyPicker'

describe('the strategy picker', () => {
  it('leads with the catalogue names a lineage is shelved under, the document beside them', () => {
    // 29/09: the base CHoCH is "CHOCH COMPLETO" to him and a generated name to the document.
    renderWithProviders(<StrategyPicker value="" onChange={vi.fn()} />)

    expect(
      screen.getByRole('option', {
        name: /^CHOCH COMPLETO \/ CHOCH COMPLETO sem HTF — SCHOCH-20260922-222429 · structure_choch/,
      }),
    ).toBeInTheDocument()
    expect(screen.getByRole('option', { name: /^MM9 CASCA · mme9_breakout/ })).toBeInTheDocument()
  })

  it('hands back the whole row that was chosen', () => {
    const onChange = vi.fn()
    renderWithProviders(<StrategyPicker value="" onChange={onChange} />)

    fireEvent.change(screen.getByLabelText('Strategy'), { target: { value: 'base' } })

    expect(onChange).toHaveBeenCalledWith(items[0])
  })

  it('names a row from an older server by its document alone', () => {
    const older: StrategyListItem = { ...items[0]! }
    delete older.catalog
    expect(labelOf(older)).toBe('SCHOCH-20260922-222429')
  })
})
