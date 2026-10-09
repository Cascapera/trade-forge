import { fireEvent, screen } from '@testing-library/react'

import type { LiveSetup } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useStrategy: vi.fn(),
  useEditLiveSetup: vi.fn(),
}))

import { useEditLiveSetup, useStrategy } from '../api/hooks'
import { SetupEditor } from './SetupEditor'

const definition = {
  schema_version: '1.0',
  name: 'CHOCH BASE',
  timeframe: 'H1',
  setup: {
    type: 'structure_choch',
    params: { htf: null, side: 'long', stop_buffer: 0.15, volume_filter: false },
  },
  exit: { take_profit: { type: 'risk_multiple', params: { rr: 3 } } },
  risk: { sizing: { type: 'percent_risk', params: { percent: 1 } } },
}

const setup = { id: 's1', strategy_id: 'st1' } as LiveSetup

function mount(doc: Record<string, unknown> = definition): ReturnType<typeof vi.fn> {
  const mutate = vi.fn()
  vi.mocked(useStrategy).mockReturnValue({ data: { definition: doc } } as never)
  vi.mocked(useEditLiveSetup).mockReturnValue({
    mutate,
    isPending: false,
    error: null,
  } as never)
  renderWithProviders(<SetupEditor setup={setup} onClose={vi.fn()} />)
  return mutate
}

function sent(mutate: ReturnType<typeof vi.fn>): Record<string, unknown> {
  const [call] = mutate.mock.calls as [[{ definition: Record<string, unknown> }]]
  return call[0].definition
}

describe('SetupEditor', () => {
  it('draws the setup’s own parameters from the schema, with their values', () => {
    mount()

    expect(screen.getByLabelText('side')).toHaveValue('long')
    expect(screen.getByLabelText('stop_buffer')).toHaveValue(0.15)
    expect(screen.getByLabelText('take profit (R)')).toHaveValue(3)
    expect(screen.getByLabelText('volume_filter')).not.toBeChecked()
  })

  it('saves the edited parameters as a new version', () => {
    const mutate = mount()

    fireEvent.change(screen.getByLabelText('side'), { target: { value: 'both' } })
    fireEvent.click(screen.getByLabelText('take profit (R) off'))
    fireEvent.click(screen.getByRole('button', { name: 'Save as a new version' }))

    const doc = sent(mutate)
    const params = (doc.setup as { params: Record<string, unknown> }).params
    expect(params.side).toBe('both')
    expect(params.stop_buffer).toBe(0.15)
    expect((doc.exit as { take_profit: unknown }).take_profit).toBeNull()
    expect(doc.risk).toEqual(definition.risk)
  })

  it('turns a parameter that may be off back on, at its default', () => {
    const mutate = mount({
      ...definition,
      exit: { take_profit: null },
    })

    fireEvent.click(screen.getByLabelText('take profit (R) off'))
    fireEvent.click(screen.getByRole('button', { name: 'Save as a new version' }))

    const target = (sent(mutate).exit as { take_profit: { params: { rr: number } } }).take_profit
    expect(target.params.rr).toBeGreaterThan(0)
  })

  it('switches a filter off from its menu and edits a number', () => {
    const mutate = mount({
      ...definition,
      setup: { ...definition.setup, params: { ...definition.setup.params, htf: 'H4' } },
    })

    fireEvent.change(screen.getByLabelText('htf'), { target: { value: '' } })
    fireEvent.change(screen.getByLabelText('stop_buffer'), { target: { value: '0.3' } })
    fireEvent.click(screen.getByLabelText('volume_filter'))
    fireEvent.click(screen.getByRole('button', { name: 'Save as a new version' }))

    const params = (sent(mutate).setup as { params: Record<string, unknown> }).params
    expect(params.htf).toBeNull()
    expect(params.stop_buffer).toBe(0.3)
    expect(params.volume_filter).toBe(true)
  })

  it('waits for the setup to load', () => {
    vi.mocked(useStrategy).mockReturnValue({ data: undefined } as never)
    vi.mocked(useEditLiveSetup).mockReturnValue({ mutate: vi.fn() } as never)
    renderWithProviders(<SetupEditor setup={setup} onClose={vi.fn()} />)
    expect(screen.getByText('Loading the setup…')).toBeInTheDocument()
  })

  it('says a strategy built from conditions is edited in the catalogue', () => {
    mount({ ...definition, setup: undefined })
    expect(screen.getByText(/edit it in\s+the catalogue/)).toBeInTheDocument()
  })
})
