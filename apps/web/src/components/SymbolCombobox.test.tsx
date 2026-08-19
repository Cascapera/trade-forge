import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { useState } from 'react'
import { vi } from 'vitest'

import type { BrokerSymbol, SymbolSearch } from '../api/types'

const sync = vi.fn()
let answer: SymbolSearch = { symbols: [], snapshot: null }
let lastQuery = ''

vi.mock('../api/hooks', () => ({
  useSymbolSearch: (q: string) => {
    lastQuery = q
    return { data: answer }
  },
  useSyncSymbols: () => ({ mutate: sync, isPending: false }),
}))

import { SymbolCombobox } from './SymbolCombobox'

function symbol(patch: Partial<BrokerSymbol> = {}): BrokerSymbol {
  return {
    symbol: 'EURUSD',
    description: 'Euro vs US Dollar',
    path: 'Forex\\Majors\\EURUSD',
    digits: 5,
    visible: true,
    catalogued: true,
    ...patch,
  }
}

const SNAPSHOT = { server: 'Tradeview-Demo', synced_at: '2026-08-19T12:17:23Z' }

/** Controlled, like the real caller: the input has to reflect what the form was told. */
function Harness(): React.JSX.Element {
  const [value, setValue] = useState('')
  return (
    <SymbolCombobox
      value={value}
      onChange={(next) => {
        setValue(next)
      }}
    />
  )
}

function type(text: string): void {
  fireEvent.change(screen.getByLabelText('Symbol'), { target: { value: text } })
}

beforeEach(() => {
  answer = { symbols: [], snapshot: null }
  lastQuery = ''
  sync.mockClear()
})

it('asks the catalogue after a single letter', async () => {
  // The whole point of the field: you do not have to know the full ticker to find it.
  answer = { symbols: [symbol({ symbol: 'XAUUSD' })], snapshot: SNAPSHOT }
  render(<Harness />)

  type('x')

  await waitFor(() => {
    expect(lastQuery).toBe('x')
  })
  expect(await screen.findByRole('option', { name: /XAUUSD/ })).toBeInTheDocument()
})

it('marks a symbol the broker has and this system has never collected', async () => {
  /**
   * ⚠️ The screen would otherwise offer 84 identical choices of which one runs, and the user
   * would learn which by clicking. Measured on this broker on 19/08/2026: 1 catalogued
   * instrument against 84 the account can see.
   */
  answer = {
    symbols: [symbol({ symbol: 'EURUSD', catalogued: true }), symbol({ symbol: 'EURJPY', catalogued: false })],
    snapshot: SNAPSHOT,
  }
  render(<Harness />)

  type('eur')

  const runnable = await screen.findByRole('option', { name: /EURUSD/ })
  const notRunnable = await screen.findByRole('option', { name: /EURJPY/ })
  expect(runnable).not.toHaveTextContent('no data')
  expect(notRunnable).toHaveTextContent('no data')
})

it('reports a symbol outside Market Watch like any other', async () => {
  // Measured: 74 of this broker's 84 symbols are hidden, and every one searches and collects
  // exactly the same. Treating them differently here would hide seven eighths of the catalogue.
  answer = { symbols: [symbol({ symbol: 'AUDCAD', visible: false })], snapshot: SNAPSHOT }
  render(<Harness />)

  type('aud')

  expect(await screen.findByRole('option', { name: /AUDCAD/ })).toBeInTheDocument()
})

it('choosing one puts it in the field', async () => {
  answer = { symbols: [symbol({ symbol: 'XAUUSD' })], snapshot: SNAPSHOT }
  render(<Harness />)
  type('x')

  fireEvent.mouseDown(await screen.findByRole('option', { name: /XAUUSD/ }))

  expect(screen.getByLabelText('Symbol')).toHaveValue('XAUUSD')
})

it('the arrow keys and enter pick without the mouse', async () => {
  answer = {
    symbols: [symbol({ symbol: 'EURGBP' }), symbol({ symbol: 'EURJPY' })],
    snapshot: SNAPSHOT,
  }
  render(<Harness />)
  type('eur')
  await screen.findByRole('option', { name: /EURGBP/ })

  const input = screen.getByLabelText('Symbol')
  fireEvent.keyDown(input, { key: 'ArrowDown' })
  fireEvent.keyDown(input, { key: 'Enter' })

  expect(input).toHaveValue('EURJPY')
})

it('an empty result with no catalogue sends you to sync, not to type more', async () => {
  /**
   * ⚠️ The two ways this list can be empty mean opposite things, and only one of them is fixed
   * by typing differently. Flattening them would tell somebody with no catalogue at all that
   * their search found nothing — true, and the least useful true sentence available.
   */
  answer = { symbols: [], snapshot: null }
  render(<Harness />)

  type('eur')

  expect(await screen.findByText(/no broker catalogue yet/)).toBeInTheDocument()
})

it('an empty result with a catalogue says nothing matched', async () => {
  answer = { symbols: [], snapshot: SNAPSHOT }
  render(<Harness />)

  type('zzz')

  expect(await screen.findByText(/no symbol starts with/)).toBeInTheDocument()
})

it('shows where the list came from, so a stale one is visible as stale', () => {
  // A user who switched brokers and forgot is exactly who an unlabelled list misleads.
  answer = { symbols: [], snapshot: SNAPSHOT }
  render(<Harness />)

  expect(screen.getByText(/Tradeview-Demo/)).toBeInTheDocument()
})

it('says so when nothing has ever been synced', () => {
  render(<Harness />)

  expect(screen.getByText('never synced')).toBeInTheDocument()
})

it('the sync button asks the host agent', () => {
  render(<Harness />)

  fireEvent.click(screen.getByRole('button', { name: 'sync from MT5' }))

  expect(sync).toHaveBeenCalledOnce()
})
