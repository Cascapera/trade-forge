import { fireEvent, screen } from '@testing-library/react'

import type { WatchItem } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useWatchItems: vi.fn(),
  useChangeWatchItem: vi.fn(),
  useRemoveWatchItem: vi.fn(),
}))

import { useChangeWatchItem, useRemoveWatchItem, useWatchItems } from '../api/hooks'
import { Watchlist } from './Watchlist'

const item: WatchItem = {
  id: 'w1',
  symbol: 'WIN',
  broker: 'xp',
  strategy_id: 's1',
  strategy_name: 'CHOCH',
  timeframe: 'H1',
  no_target_r: '5.00000000',
  active: true,
  note: 'melhor H1',
  source_backtest_id: 'b1',
  created_at: '2026-10-08T12:00:00Z',
}

function mount(items: WatchItem[]): { change: ReturnType<typeof vi.fn>; remove: ReturnType<typeof vi.fn> } {
  const change = vi.fn()
  const remove = vi.fn()
  vi.mocked(useWatchItems).mockReturnValue({ data: items, isPending: false } as never)
  vi.mocked(useChangeWatchItem).mockReturnValue({ mutate: change, isPending: false, error: null } as never)
  vi.mocked(useRemoveWatchItem).mockReturnValue({ mutate: remove, isPending: false, error: null } as never)
  renderWithProviders(<Watchlist />)
  return { change, remove }
}

describe('Watchlist', () => {
  it('lists each setup with its market, broker, R and a link to the run it came from', () => {
    mount([item])
    expect(screen.getByText('WIN')).toBeInTheDocument()
    expect(screen.getByText('xp')).toBeInTheDocument()
    expect(screen.getByText('5R')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'CHOCH' })).toHaveAttribute('href', '/results/b1')
  })

  it('turns an item off and removes one', () => {
    const { change, remove } = mount([item])
    fireEvent.click(screen.getByRole('button', { name: 'Turn off' }))
    expect(change).toHaveBeenCalledWith({ id: 'w1', patch: { active: false } })
    fireEvent.click(screen.getByRole('button', { name: 'Remove WIN H1 CHOCH' }))
    expect(remove).toHaveBeenCalledWith('w1')
  })

  it('says when nothing is watched', () => {
    mount([])
    expect(screen.getByText('Nothing is watched yet.')).toBeInTheDocument()
  })
})
