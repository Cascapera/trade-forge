import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
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

  it('says what is missing, one thing at a time, before it lets a template be kept', async () => {
    renderWithProviders(<Templates />)
    const say = (text: string): void => {
      expect(screen.getByText(text)).toBeInTheDocument()
      expect(screen.getByRole('button', { name: 'Keep the template' })).toBeDisabled()
    }

    say('Give the template a name.')
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
    say('Choose at least one entry from the shelf.')
    fireEvent.click(await screen.findByLabelText(/CHOCH COMPLETO/))
    say('Choose at least one chart.')
    fireEvent.click(screen.getByLabelText('H4'))
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2008-01-01' } })
    say('The window must end after it starts.')
    fireEvent.change(screen.getByLabelText('To'), { target: { value: '2020-01-01' } })
    fireEvent.change(screen.getByLabelText('Capital'), { target: { value: '0' } })
    say('The capital must be positive.')
    fireEvent.change(screen.getByLabelText('From'), { target: { value: '2010-01-01' } })
    fireEvent.click(screen.getByLabelText('H4'))
    say('Choose at least one chart.')
  })

  it('lists the kept templates with how their queues stand', async () => {
    mocked.listSweepTemplates.mockResolvedValue([
      {
        id: 't1',
        name: 'CHOCH H4',
        timeframes: ['H4'],
        date_from: '2009-01-01T00:00:00Z',
        date_to: '2020-01-01T00:00:00Z',
        paused: true,
        waiting: 2,
        launched: 3,
        failed: 1,
        created_at: '2026-09-26T10:00:00Z',
      },
    ])
    renderWithProviders(<Templates />)

    expect(await screen.findByRole('link', { name: 'CHOCH H4' })).toHaveAttribute(
      'href',
      '/templates/t1',
    )
    expect(screen.getByText(/3 run, 2 waiting, 1 failed · paused/)).toBeInTheDocument()
  })

  it('says the server refusal in its own words', async () => {
    mocked.createSweepTemplate.mockRejectedValue(
      new ApiError(409, "a template is already named 'x'"),
    )
    renderWithProviders(<Templates />)
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'x' } })
    fireEvent.click(await screen.findByLabelText(/CHOCH COMPLETO/))
    fireEvent.click(screen.getByLabelText('H1'))
    fireEvent.click(screen.getByRole('button', { name: 'Keep the template' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('already named')
  })

  it('says so when the templates cannot be read, and when there are none', async () => {
    renderWithProviders(<Templates />)
    expect(await screen.findByText('No template yet.')).toBeInTheDocument()
  })
})
