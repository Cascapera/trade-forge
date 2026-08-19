import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { vi } from 'vitest'

// ⚠️ This component used to fetch nothing at all — it took `instruments` as a prop and was
// pure. The symbol field is now a combobox over the broker's catalogue, which searches, so the
// two hooks behind it are stubbed. The alternative was threading six props of query state
// through every caller, which is plumbing, not a boundary.
vi.mock('../api/hooks', () => ({
  useSymbolSearch: () => ({ data: { symbols: [], snapshot: null } }),
  useSyncSymbols: () => ({ mutate: () => undefined, isPending: false }),
}))

import type { Instrument } from '../api/types'
import { emptyBacktestForm, type BacktestForm } from '../backtest/settings'
import { BacktestSettings } from './BacktestSettings'

function instrument(patch: Partial<Instrument> = {}): Instrument {
  return {
    id: 'i1',
    symbol: 'AAPL',
    name: 'Apple Inc.',
    asset_class: 'stock',
    currency_quote: 'USD',
    currency_base: null,
    tick_size: '0.01',
    tick_value: '0.01',
    contract_size: '1',
    digits: 2,
    default_spread_points: '1.0000000000',
    ...patch,
  }
}

const AAPL = instrument()
const EURUSD = instrument({
  id: 'i2',
  symbol: 'EURUSD',
  asset_class: 'forex',
  tick_size: '0.00001',
  default_spread_points: '12.0000000000',
})
const UNMEASURED = instrument({ id: 'i3', symbol: 'US500', default_spread_points: null })

/** The component is controlled, so a test that changes a field needs something holding the
 *  state — otherwise every assertion would be about the props it was handed, not about what
 *  choosing a symbol actually does. */
function Harness({ instruments }: { instruments: Instrument[] }): React.JSX.Element {
  const [form, setForm] = useState<BacktestForm>(emptyBacktestForm())
  return <BacktestSettings form={form} instruments={instruments} onChange={setForm} />
}

function choose(symbol: string): void {
  fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: symbol } })
}

describe('BacktestSettings', () => {
  it('charges the instrument’s own spread as soon as one is chosen', () => {
    render(<Harness instruments={[AAPL, EURUSD]} />)
    // Nothing chosen yet, so there is nothing honest to charge and no warning to give.
    expect(screen.getByLabelText('cost model')).toHaveValue('none')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    choose('AAPL')

    expect(screen.getByLabelText('cost model')).toHaveValue('spread')
    expect(screen.getByLabelText('spread points')).toHaveValue(1)
  })

  it('replaces one instrument’s spread with the next one’s', () => {
    // The number belongs to the symbol. Leaving AAPL's 1 behind on EURUSD would undercharge
    // by a factor of twelve, on the instrument where the spread actually matters.
    render(<Harness instruments={[AAPL, EURUSD]} />)

    choose('AAPL')
    expect(screen.getByLabelText('spread points')).toHaveValue(1)

    choose('EURUSD')
    expect(screen.getByLabelText('spread points')).toHaveValue(12)
  })

  it('shows what the broker quotes beside the field you can override', () => {
    render(<Harness instruments={[EURUSD]} />)
    choose('EURUSD')
    expect(screen.getByText('EURUSD quotes 12')).toBeInTheDocument()
  })

  it('lets the spread be typed over without losing the choice', () => {
    render(<Harness instruments={[EURUSD]} />)
    choose('EURUSD')

    fireEvent.change(screen.getByLabelText('spread points'), { target: { value: '20' } })

    expect(screen.getByLabelText('spread points')).toHaveValue(20)
    expect(screen.getByLabelText('cost model')).toHaveValue('spread')
  })

  it('warns, and says why, when the catalogue has no spread for the instrument', () => {
    render(<Harness instruments={[UNMEASURED]} />)
    choose('US500')

    expect(screen.getByLabelText('cost model')).toHaveValue('none')
    const notice = screen.getByRole('status')
    expect(notice).toHaveTextContent('no spread has been catalogued')
    expect(notice).toHaveTextContent('upper bound')
  })

  it('warns differently when costs were switched off on purpose', () => {
    // Two costless runs, two different reasons. Collapsing them would hide from the reader
    // whether the gap is theirs or the catalogue's.
    render(<Harness instruments={[AAPL]} />)
    choose('AAPL')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('cost model'), { target: { value: 'none' } })

    expect(screen.getByRole('status')).toHaveTextContent('switched off for this run')
  })

  it('never claims a measured instrument costs nothing', () => {
    // The regression that matters: a run on an instrument with a known spread must not be
    // able to reach the API charging zero without the screen having said so.
    render(<Harness instruments={[EURUSD]} />)
    choose('EURUSD')

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByLabelText('cost model')).toHaveValue('spread')
  })
})
