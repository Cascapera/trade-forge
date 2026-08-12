import { fireEvent, screen } from '@testing-library/react'

import type { Instrument } from '../api/types'
import { MAX_SYMBOLS } from '../basket/settings'
import { renderWithProviders } from '../test-utils'
import { SymbolPicker } from './SymbolPicker'

function instrument(symbol: string, spread: string | null): Instrument {
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
    default_spread_points: spread,
  }
}

const catalogue = [
  instrument('EURUSD', '8.0000000000'),
  instrument('GBPUSD', '9.0000000000'),
  instrument('US500', null),
]

describe('SymbolPicker', () => {
  it('shows what each market will be charged, beside the tick that chooses it', () => {
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={[]} onToggle={vi.fn()} />,
    )

    expect(screen.getByText('8 ticks')).toBeInTheDocument()
    expect(screen.getByText('9 ticks')).toBeInTheDocument()
  })

  it('says an unmeasured market has no spread rather than showing it as free', () => {
    // ⚠️ The assertion this component exists for. "0 ticks" is the claim that US500 costs nothing
    // to trade; the truth is that nobody has measured it. Rendered as zero it would sit in the
    // same column as the measured 8 and 9 and read as the cheapest market in the catalogue.
    renderWithProviders(<SymbolPicker instruments={catalogue} chosen={[]} onToggle={vi.fn()} />)

    expect(screen.getByText('no spread measured')).toBeInTheDocument()
    expect(screen.queryByText('0 ticks')).not.toBeInTheDocument()
  })

  it('puts the cost in the accessible name, so it is not a sighted-only column', () => {
    renderWithProviders(<SymbolPicker instruments={catalogue} chosen={[]} onToggle={vi.fn()} />)

    expect(screen.getByRole('checkbox', { name: 'EURUSD, 8 ticks' })).toBeInTheDocument()
    expect(screen.getByRole('checkbox', { name: 'US500, no spread measured' })).toBeInTheDocument()
  })

  it('reports a tick to the caller', () => {
    const onToggle = vi.fn()
    renderWithProviders(<SymbolPicker instruments={catalogue} chosen={[]} onToggle={onToggle} />)

    fireEvent.click(screen.getByRole('checkbox', { name: 'GBPUSD, 9 ticks' }))

    expect(onToggle).toHaveBeenCalledWith('GBPUSD')
  })

  it('shows which markets are already chosen', () => {
    renderWithProviders(
      <SymbolPicker instruments={catalogue} chosen={['EURUSD']} onToggle={vi.fn()} />,
    )

    expect(screen.getByRole('checkbox', { name: 'EURUSD, 8 ticks' })).toBeChecked()
    expect(screen.getByRole('checkbox', { name: 'GBPUSD, 9 ticks' })).not.toBeChecked()
    expect(screen.getByText(/1 chosen/)).toBeInTheDocument()
  })

  it('blocks the ceiling but never the markets already ticked', () => {
    // A full picker that disabled everything would trap the reader: they could not untick one to
    // make room, which is the only way out of the ceiling.
    const full = Array.from({ length: MAX_SYMBOLS }, (_, i) => `SYM${String(i)}`)
    renderWithProviders(
      <SymbolPicker
        instruments={[...catalogue, ...full.map((s) => instrument(s, '1'))]}
        chosen={full}
        onToggle={vi.fn()}
      />,
    )

    expect(screen.getByRole('checkbox', { name: 'EURUSD, 8 ticks' })).toBeDisabled()
    expect(screen.getByRole('checkbox', { name: 'SYM0, 1 ticks' })).toBeEnabled()
  })

  it('says the catalogue is loading rather than showing an empty grid', () => {
    // An empty grid and a grid that has not arrived look identical, and one of them is a bug.
    renderWithProviders(<SymbolPicker instruments={undefined} chosen={[]} onToggle={vi.fn()} />)
    expect(screen.getByText(/loading the catalogue/i)).toBeInTheDocument()

    renderWithProviders(<SymbolPicker instruments={[]} chosen={[]} onToggle={vi.fn()} />)
    expect(screen.getByText(/no instruments catalogued/i)).toBeInTheDocument()
  })
})
