import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import { useState } from 'react'

import { api } from '../api/client'
import { browsed, fakeBroker, renderWithProviders } from '../test-utils'
import { browsedCost } from '../basket/settings'
import { BROWSE_PAGE, MarketBrowser } from './MarketBrowser'

vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, getMarkets: vi.fn(), browseSymbols: vi.fn() } }
})

// Thirty collected crypto, two currencies (one never measured), one US share never collected.
const rows = [
  browsed('EURUSD', 'forex', '8.0000000000'),
  browsed('GBPUSD', 'forex', null),
  ...Array.from({ length: 30 }, (_, i) =>
    browsed(`COIN${String(i).padStart(2, '0')}`, 'crypto', '50'),
  ),
  browsed('AAPL.US', 'stocks_us', null, false),
]

beforeEach(() => {
  const broker = fakeBroker(rows)
  vi.mocked(api.getMarkets).mockImplementation(broker.markets)
  vi.mocked(api.browseSymbols).mockImplementation(broker.browse)
})

/** The browser over a form that keeps what it is handed, as the launch screens do. */
function Harness(props: { start?: string[]; max?: number; onClose?: () => void }) {
  const [chosen, setChosen] = useState<string[]>(props.start ?? [])
  return (
    <>
      <output aria-label="chosen">{chosen.join(',')}</output>
      <MarketBrowser
        chosen={chosen}
        max={props.max ?? 500}
        onChange={setChosen}
        onClose={props.onClose ?? vi.fn()}
      />
    </>
  )
}

function chosen(): string {
  return screen.getByRole('status', { name: 'chosen' }).textContent
}

async function openTab(name: RegExp): Promise<void> {
  fireEvent.click(await screen.findByRole('tab', { name }))
}

describe('MarketBrowser', () => {
  it('opens on the first market with something collected, the cost beside each tick', async () => {
    renderWithProviders(<Harness />)

    expect(await screen.findByRole('checkbox', { name: 'EURUSD, 8 ticks' })).toBeInTheDocument()
    // ⚠️ Unmeasured is not free: "0 ticks" would read as the cheapest market in the list.
    expect(screen.getByRole('checkbox', { name: 'GBPUSD, no spread measured' })).toBeInTheDocument()
    expect(screen.queryByText('0 ticks')).not.toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /forex/ })).toHaveAttribute('aria-selected', 'true')
  })

  it('ticks and unticks a market', async () => {
    renderWithProviders(<Harness />)

    const box = await screen.findByRole('checkbox', { name: 'EURUSD, 8 ticks' })
    fireEvent.click(box)
    expect(chosen()).toBe('EURUSD')
    fireEvent.click(box)
    expect(chosen()).toBe('')
  })

  it('shows a market a page at a time', async () => {
    renderWithProviders(<Harness />)
    await openTab(/crypto/)

    await screen.findByRole('checkbox', { name: 'COIN00, 50 ticks' })
    expect(screen.getAllByRole('checkbox', { name: /^COIN/ })).toHaveLength(BROWSE_PAGE)
    expect(screen.getByText('1–25 of 30')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('button', { name: 'Next →' }))
    expect(await screen.findByRole('checkbox', { name: 'COIN29, 50 ticks' })).toBeInTheDocument()
    expect(screen.getAllByRole('checkbox', { name: /^COIN/ })).toHaveLength(5)
  })

  it('chooses this page, then the whole market, then clears it', async () => {
    renderWithProviders(<Harness />)
    await openTab(/crypto/)
    await screen.findByRole('checkbox', { name: 'COIN00, 50 ticks' })

    fireEvent.click(screen.getByRole('button', { name: 'Choose this page' }))
    expect(chosen().split(',')).toHaveLength(BROWSE_PAGE)

    fireEvent.click(screen.getByRole('button', { name: 'Choose all 30' }))
    await waitFor(() => {
      expect(chosen().split(',')).toHaveLength(30)
    })

    fireEvent.click(screen.getByRole('button', { name: 'Clear these' }))
    await waitFor(() => {
      expect(chosen()).toBe('')
    })
  })

  it('stops at the ceiling and says how many were left out', async () => {
    renderWithProviders(<Harness max={20} />)
    await openTab(/crypto/)
    await screen.findByRole('checkbox', { name: 'COIN00, 50 ticks' })

    fireEvent.click(screen.getByRole('button', { name: 'Choose all 30' }))

    expect(await screen.findByText(/20 markets at most — 10 of these were left out/)).toBeVisible()
    expect(chosen().split(',')).toHaveLength(20)
    // A full browser still lets a tick go — the only way out of the ceiling.
    expect(screen.getByRole('checkbox', { name: 'COIN00, 50 ticks' })).toBeEnabled()
    expect(screen.getByRole('checkbox', { name: 'COIN24, 50 ticks' })).toBeDisabled()
  })

  it('searches the market by name', async () => {
    renderWithProviders(<Harness />)
    await openTab(/crypto/)

    fireEvent.change(screen.getByRole('searchbox', { name: /search this market/i }), {
      target: { value: 'coin1' },
    })

    await waitFor(() => {
      expect(screen.getAllByRole('checkbox', { name: /^COIN/ })).toHaveLength(10)
    })
  })

  it('shows the markets never collected only when asked', async () => {
    renderWithProviders(<Harness />)
    await openTab(/stocks_us/)
    expect(await screen.findByText('Nothing here matches.')).toBeInTheDocument()

    fireEvent.click(screen.getByRole('checkbox', { name: 'Only collected' }))

    expect(
      await screen.findByRole('checkbox', { name: 'AAPL.US, never collected' }),
    ).toBeInTheDocument()
  })

  it('counts each tab by what it shows', async () => {
    renderWithProviders(<Harness />)

    const crypto = await screen.findByRole('tab', { name: /crypto/ })
    expect(within(crypto).getByText('30')).toBeInTheDocument()
    expect(within(screen.getByRole('tab', { name: /stocks_us/ })).getByText('0')).toBeVisible()
  })

  it('closes on Escape and on a click outside', async () => {
    const onClose = vi.fn()
    renderWithProviders(<Harness onClose={onClose} />)
    const dialog = await screen.findByRole('dialog')

    fireEvent.keyDown(document, { key: 'Escape' })
    fireEvent.mouseDown(dialog)
    expect(onClose).toHaveBeenCalledTimes(1)

    fireEvent.mouseDown(dialog.parentElement!)
    expect(onClose).toHaveBeenCalledTimes(2)
  })

  it('says the broker was never synced rather than showing an empty market', async () => {
    vi.mocked(api.getMarkets).mockResolvedValue({ markets: [], snapshot: null })
    vi.mocked(api.browseSymbols).mockResolvedValue({ total: 0, offset: 0, limit: 25, items: [] })
    renderWithProviders(<Harness />)

    expect(await screen.findByRole('link', { name: /sync it/i })).toHaveAttribute(
      'href',
      '/collect',
    )
  })
})

describe('browsedCost', () => {
  it('names what is known about the cost, and never calls an unknown one zero', () => {
    expect(browsedCost(browsed('A', 'forex', '8.5000000000'))).toBe('8.5 ticks')
    expect(browsedCost(browsed('B', 'forex', null))).toBe('no spread measured')
    expect(browsedCost(browsed('C', 'forex', '3', false))).toBe('never collected')
  })
})
