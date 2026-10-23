import { fireEvent, screen, within } from '@testing-library/react'

import { api } from '../api/client'
import type { Instrument } from '../api/types'
import { browsed, fakeBroker, renderWithProviders } from '../test-utils'
import { CHIPS_PER_PAGE, SymbolPicker } from './SymbolPicker'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return {
    ...actual,
    api: { ...actual.api, searchSymbols: vi.fn(), getMarkets: vi.fn(), browseSymbols: vi.fn() },
  }
})

beforeEach(() => {
  // The broker offers one market nobody has collected — the case the search exists for.
  vi.mocked(api.searchSymbols).mockResolvedValue({
    symbols: [
      {
        symbol: 'AUDUSD',
        description: 'Australian Dollar vs US Dollar',
        path: 'Forex/Majors/AUDUSD',
        digits: 5,
        visible: true,
        asset_class_from_path: 'forex',
        catalogued: false,
      },
    ],
    snapshot: null,
  })
  const broker = fakeBroker([browsed('EURUSD', 'forex', '8.0000000000')])
  vi.mocked(api.getMarkets).mockImplementation(broker.markets)
  vi.mocked(api.browseSymbols).mockImplementation(broker.browse)
})

function instrument(symbol: string): Instrument {
  return {
    id: `i-${symbol}`,
    symbol,
    name: symbol,
    asset_class: 'forex',
    currency_quote: 'USD',
    currency_base: 'EUR',
    tick_size: '0.00001',
    tick_value: '1',
    contract_size: '100000',
    digits: 5,
    default_spread_points: '8',
  }
}

const catalogue = [instrument('EURUSD'), instrument('GBPUSD')]

describe('SymbolPicker', () => {
  it('shows the chosen markets as chips that remove themselves', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <SymbolPicker
        instruments={catalogue}
        chosen={['EURUSD', 'GBPUSD']}
        onChange={onChange}
        max={20}
      />,
    )

    expect(screen.getByText(/2 chosen/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Remove EURUSD' }))
    expect(onChange).toHaveBeenCalledWith(['GBPUSD'])
  })

  it('clears every choice at once', () => {
    const onChange = vi.fn()
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={['EURUSD']} onChange={onChange} max={20} />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Clear all' }))
    expect(onChange).toHaveBeenCalledWith([])
  })

  it('says nothing is chosen yet, and how many markets are collected', () => {
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={[]} onChange={vi.fn()} max={20} />,
    )

    expect(screen.getByText(/no market chosen yet\. 2 collected so far/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'Clear all' })).not.toBeInTheDocument()
  })

  it('pages the chips once there are many', () => {
    // His ask (04/10): every long list is paged — a sweep of a whole market is hundreds of chips.
    const many = Array.from({ length: CHIPS_PER_PAGE + 5 }, (_, i) => `SYM${String(i)}`)
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={many} onChange={vi.fn()} max={500} />,
    )

    const chips = screen.getByRole('list', { name: 'Chosen markets' })
    expect(within(chips).getAllByRole('button')).toHaveLength(CHIPS_PER_PAGE)
    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    expect(within(chips).getAllByRole('button')).toHaveLength(5)
  })

  it('opens the market browser and closes it again', async () => {
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={[]} onChange={vi.fn()} max={20} />,
    )

    fireEvent.click(screen.getByRole('button', { name: 'Choose markets…' }))
    expect(await screen.findByRole('dialog', { name: 'Choose markets' })).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('finds a market outside the catalogue and hands its name to the caller', async () => {
    const onChange = vi.fn()
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={['EURUSD']} onChange={onChange} max={20} />,
    )

    fireEvent.change(screen.getByRole('combobox', { name: /find any market/i }), {
      target: { value: 'aud' },
    })
    fireEvent.mouseDown(await screen.findByRole('option', { name: /AUDUSD/ }))

    expect(onChange).toHaveBeenCalledWith(['EURUSD', 'AUDUSD'])
  })

  it('shows a chosen market never collected as a chip that says so, with the way to fix it', () => {
    // ⚠️ "no spread measured" would be the wrong sentence: the market has no candles at all, and
    // the fix is the collect screen, not a cost typed in.
    const onChange = vi.fn()
    renderWithProviders(
      <SymbolPicker
        instruments={catalogue}
        chosen={['EURUSD', 'AUDUSD']}
        onChange={onChange}
        max={20}
      />,
    )

    expect(screen.getByRole('link', { name: /collect it first/i })).toHaveAttribute(
      'href',
      '/collect',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Remove AUDUSD, never collected' }))
    expect(onChange).toHaveBeenCalledWith(['EURUSD'])
  })

  it('refuses to add past the ceiling from the search', async () => {
    const full = Array.from({ length: 20 }, (_, i) => `SYM${String(i)}`)
    const onChange = vi.fn()
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={full} onChange={onChange} max={20} />,
    )

    fireEvent.change(screen.getByRole('combobox', { name: /find any market/i }), {
      target: { value: 'aud' },
    })
    fireEvent.mouseDown(await screen.findByRole('option', { name: /AUDUSD/ }))

    expect(onChange).not.toHaveBeenCalled()
    expect(screen.getByText(/already at 20 markets/i)).toBeInTheDocument()
  })

  it('chooses a ticker found by search under the name it was collected as (09/10)', async () => {
    vi.mocked(api.searchSymbols).mockResolvedValue({
      symbols: [
        {
          symbol: 'DOL$',
          description: 'DOLAR COMERCIAL FUTURO',
          path: 'BMF/SERIES CONTINUAS/DOL$',
          digits: 3,
          visible: true,
          asset_class_from_path: 'future',
          catalogued: true,
          name: 'DOL',
        },
      ],
      snapshot: null,
    })
    const onChange = vi.fn()
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={['EURUSD']} onChange={onChange} max={20} />,
    )

    fireEvent.change(screen.getByRole('combobox', { name: /find any market/i }), {
      target: { value: 'dol' },
    })
    fireEvent.mouseDown(await screen.findByRole('option', { name: /DOL\$/ }))

    expect(onChange).toHaveBeenCalledWith(['EURUSD', 'DOL'])
  })
})
