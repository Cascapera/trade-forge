import { fireEvent, render, screen } from '@testing-library/react'
import { vi } from 'vitest'

import type { BrokerSymbol, SymbolSearch } from '../api/types'

let answer: SymbolSearch = { symbols: [], snapshot: null }
const sync = vi.fn()

vi.mock('../api/hooks', () => ({
  useSymbolSearch: () => ({ data: answer }),
  useSyncSymbols: () => ({ mutate: sync, isPending: false }),
}))

import { SymbolMultiCombobox } from './SymbolMultiCombobox'

function symbol(patch: Partial<BrokerSymbol> = {}): BrokerSymbol {
  return {
    symbol: 'EURUSD',
    description: 'Euro vs US Dollar',
    path: 'Forex\\Majors\\EURUSD',
    asset_class_from_path: 'forex',
    digits: 5,
    visible: true,
    catalogued: true,
    ...patch,
  }
}

const MAX = 3

function show(chosen: BrokerSymbol[] = []): { onToggle: ReturnType<typeof vi.fn> } {
  const onToggle = vi.fn()
  render(<SymbolMultiCombobox chosen={chosen} onToggle={onToggle} max={MAX} />)
  return { onToggle }
}

function typeInSearch(value: string): void {
  fireEvent.change(screen.getByRole('combobox'), { target: { value } })
}

beforeEach(() => {
  answer = { symbols: [], snapshot: null }
  sync.mockClear()
})

it('picking a result toggles it rather than replacing the field', () => {
  answer = { symbols: [symbol()], snapshot: null }
  const { onToggle } = show()
  typeInSearch('eur')

  fireEvent.mouseDown(screen.getByRole('option', { name: /EURUSD/ }))

  expect(onToggle).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'EURUSD' }))
})

it('clears the search after a pick so the next ticker can be typed', () => {
  /**
   * ⚠️ The behaviour that separates this from the single-symbol field, where the input *keeps*
   * the choice. Here the input is a search box, not a value: leaving `eur` in it would make the
   * next symbol require a manual delete before anything else could be found.
   */
  answer = { symbols: [symbol()], snapshot: null }
  show()
  typeInSearch('eur')

  fireEvent.mouseDown(screen.getByRole('option', { name: /EURUSD/ }))

  expect(screen.getByRole('combobox')).toHaveValue('')
})

it('shows a chip per chosen symbol', () => {
  show([symbol(), symbol({ symbol: 'GBPUSD' })])

  expect(screen.getByRole('button', { name: 'Remove EURUSD' })).toBeInTheDocument()
  expect(screen.getByRole('button', { name: 'Remove GBPUSD' })).toBeInTheDocument()
})

it('each chip is named for its own symbol, not a shared label', () => {
  /**
   * ⚠️ A defect, not test friction. Two buttons both called "Remove" would make
   * `getByRole('button', { name: 'Remove' })` throw "found multiple elements" — and a screen
   * reader would announce twenty identical controls with no way to tell which removes what.
   */
  show([symbol(), symbol({ symbol: 'GBPUSD' }), symbol({ symbol: 'USDJPY' })])

  const names = screen
    .getAllByRole('button', { name: /^Remove / })
    .map((node) => node.getAttribute('aria-label') ?? node.textContent)

  expect(new Set(names).size).toBe(3)
})

it('removing a chip toggles that symbol back off', () => {
  const { onToggle } = show([symbol(), symbol({ symbol: 'GBPUSD' })])

  fireEvent.click(screen.getByRole('button', { name: 'Remove GBPUSD' }))

  expect(onToggle).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'GBPUSD' }))
})

it('marks a result that is already chosen', () => {
  // Without this the list offers a symbol that is already in the batch and clicking it removes
  // it — an action whose effect is the opposite of what an unmarked row implies.
  answer = { symbols: [symbol()], snapshot: null }
  show([symbol()])
  typeInSearch('eur')

  expect(screen.getByRole('option', { name: /EURUSD/ })).toHaveTextContent(/chosen/i)
})

it('says how many of the ceiling are used', () => {
  show([symbol(), symbol({ symbol: 'GBPUSD' })])

  expect(screen.getByText(`2 of ${String(MAX)}`)).toBeInTheDocument()
})

it('at the ceiling it refuses to add and says why', () => {
  /**
   * ⚠️ Tested **at** the ceiling, and the test below tests one under it. A pair like this is
   * what separates `>` from `>=`; a single test at the limit passes against both.
   */
  answer = { symbols: [symbol({ symbol: 'USDCHF' })], snapshot: null }
  const { onToggle } = show([symbol(), symbol({ symbol: 'GBPUSD' }), symbol({ symbol: 'USDJPY' })])
  typeInSearch('usd')

  fireEvent.mouseDown(screen.getByRole('option', { name: /USDCHF/ }))

  expect(onToggle).not.toHaveBeenCalled()
  expect(screen.getByText(new RegExp(`at most ${String(MAX)}`, 'i'))).toBeInTheDocument()
})

it('one under the ceiling still adds', () => {
  answer = { symbols: [symbol({ symbol: 'USDCHF' })], snapshot: null }
  const { onToggle } = show([symbol(), symbol({ symbol: 'GBPUSD' })])
  typeInSearch('usd')

  fireEvent.mouseDown(screen.getByRole('option', { name: /USDCHF/ }))

  expect(onToggle).toHaveBeenCalled()
})

it('a full list still lets an already-chosen symbol be removed from the results', () => {
  /**
   * ⚠️ The ceiling blocks *adding*, never removing. Blocking every click while full would trap
   * a person who filled the list and then wanted to swap one out — the only escape being the
   * chips, which is not where they are looking.
   */
  answer = { symbols: [symbol()], snapshot: null }
  const { onToggle } = show([symbol(), symbol({ symbol: 'GBPUSD' }), symbol({ symbol: 'USDJPY' })])
  typeInSearch('eur')

  fireEvent.mouseDown(screen.getByRole('option', { name: /EURUSD/ }))

  expect(onToggle).toHaveBeenCalledWith(expect.objectContaining({ symbol: 'EURUSD' }))
})

it('with nothing chosen it says so rather than showing an empty strip', () => {
  show()

  expect(screen.getByText(/no symbols chosen/i)).toBeInTheDocument()
})
