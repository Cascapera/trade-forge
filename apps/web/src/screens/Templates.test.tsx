import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { api } from '../api/client'
import type { CatalogPage, SweepTemplateOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

import { Templates } from './Templates'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: {
      ...actual.api,
      listSweepTemplates: vi.fn(),
      listCatalog: vi.fn(),
      createSweepTemplate: vi.fn(),
    },
  }
})

const navigate = vi.fn()
vi.mock('react-router-dom', async (original) => ({
  ...(await original<typeof import('react-router-dom')>()),
  useNavigate: () => navigate,
}))

const mocked = vi.mocked(api)

beforeEach(() => {
  vi.clearAllMocks()
  mocked.listSweepTemplates.mockResolvedValue([])
  mocked.listCatalog.mockResolvedValue({
    total: 1,
    items: [{ id: 'e1', name: 'CHOCH COMPLETO', points: 24192 }],
  } as unknown as CatalogPage)
  mocked.createSweepTemplate.mockResolvedValue({ id: 't9' } as SweepTemplateOut)
})

describe('Templates', () => {
  it('keeps a template with its entries, charts and window, then opens its queue', async () => {
    renderWithProviders(<Templates />)

    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'CHOCH H1+H4' } })
    fireEvent.click(await screen.findByLabelText(/CHOCH COMPLETO/))
    fireEvent.click(screen.getByLabelText('H1'))
    fireEvent.click(screen.getByLabelText('H4'))
    fireEvent.click(screen.getByRole('button', { name: 'Keep the template' }))

    await waitFor(() => {
      expect(mocked.createSweepTemplate).toHaveBeenCalledWith({
        name: 'CHOCH H1+H4',
        entry_ids: ['e1'],
        timeframes: ['H1', 'H4'],
        date_from: '2009-01-01T00:00:00Z',
        date_to: '2020-01-01T00:00:00Z',
        initial_capital: '10000',
      })
    })
    expect(navigate).toHaveBeenCalledWith('/templates/t9')
  })

  it('says what is missing before it lets a template be kept', () => {
    renderWithProviders(<Templates />)

    expect(screen.getByText('Give the template a name.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep the template' })).toBeDisabled()
  })
})
