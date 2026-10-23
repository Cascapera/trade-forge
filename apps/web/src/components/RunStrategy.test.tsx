import { screen, within } from '@testing-library/react'

import type { StrategyOut } from '../api/types'
import { renderWithProviders } from '../test-utils'

const { state } = vi.hoisted(() => ({ state: { data: undefined as StrategyOut | undefined } }))

vi.mock('../api/hooks', () => ({
  useStrategy: () => ({ data: state.data }),
}))

import { RunStrategy } from './RunStrategy'

const POINT_DOCUMENT: StrategyOut = {
  id: 'p1',
  name: "CHOCH COMPLETO [M15 · htf='H4', stop_buffer=0.2]",
  version: 1,
  schema_version: '1.0',
  created_at: '2026-09-29T00:00:00Z',
  definition: {
    schema_version: '1.0',
    name: 'point',
    timeframe: 'M15',
    setup: {
      type: 'structure_choch',
      params: { htf: 'H4', htf_regions: 'with_trend', stop_buffer: 0.2, side: 'both' },
    },
    exit: { take_profit: { type: 'risk_multiple', params: { rr: 3 } } },
    risk: { sizing: { type: 'percent_risk', params: { percent: 1 } } },
  },
}

beforeEach(() => {
  state.data = POINT_DOCUMENT
})

describe('the strategy at the top of a result', () => {
  it('names the catalogue entry, the setup and the document of a sweep point', () => {
    renderWithProviders(
      <RunStrategy
        strategyId="p1"
        point={{
          entry_id: 'e1',
          entry_name: 'CHOCH COMPLETO',
          label: 'stop_buffer=0.2',
          values: { 'setup.params.stop_buffer': 0.2, 'setup.params.htf_regions': 'with_trend' },
        }}
      />,
    )
    const section = screen.getByRole('region', { name: 'strategy of this run' })

    expect(within(section).getByText('CHOCH COMPLETO')).toBeInTheDocument()
    expect(within(section).getByText(/Estrutura — CHoCH/)).toBeInTheDocument()
    expect(within(section).getByText(/ponto de uma varredura · 2 otimizados/)).toBeInTheDocument()
  })

  it('marks the parameters the sweep varied, in words', () => {
    renderWithProviders(
      <RunStrategy
        strategyId="p1"
        point={{
          entry_id: 'e1',
          entry_name: 'CHOCH COMPLETO',
          label: 'x',
          values: { 'setup.params.htf_regions': 'with_trend' },
        }}
      />,
    )

    const regions = screen.getByText('Regiões do HTF').parentElement!
    expect(regions).toHaveTextContent('só a favor da última quebra')
    expect(regions).toHaveTextContent('otimizado')
    expect(screen.getByText('Lado').parentElement!).not.toHaveTextContent('otimizado')
    expect(screen.getByText('Stop do gift').parentElement!).toHaveTextContent('(padrão)')
  })

  it('names a run of its own by its document, with nothing marked optimised', () => {
    renderWithProviders(<RunStrategy strategyId="p1" point={null} />)
    const section = screen.getByRole('region', { name: 'strategy of this run' })

    expect(within(section).getAllByText(POINT_DOCUMENT.name, { exact: false })[0]).toBeInTheDocument()
    expect(within(section).queryByText('otimizado')).not.toBeInTheDocument()
    expect(within(section).queryByText(/ponto de uma varredura/)).not.toBeInTheDocument()
  })

  it('draws nothing until the document has arrived', () => {
    state.data = undefined
    renderWithProviders(<RunStrategy strategyId="p1" point={null} />)

    expect(screen.queryByRole('region', { name: 'strategy of this run' })).not.toBeInTheDocument()
  })
})
