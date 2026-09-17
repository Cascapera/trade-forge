import { fireEvent, screen } from '@testing-library/react'

import { ApiError } from '../api/client'
import { CollectionStopped } from '../api/hooks'
import type { PlannedCollection } from '../api/types'
import type { MissingDataGate } from '../collect/gate'
import { renderWithProviders } from '../test-utils'
import { MissingDataPrompt } from './MissingDataPrompt'

const MISSING: PlannedCollection[] = [
  {
    symbol: 'GBPUSD',
    timeframe: 'H1',
    covers: null,
    windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
  },
]

function gate(over: {
  missing?: PlannedCollection[] | null
  plan?: Record<string, unknown>
  collection?: Record<string, unknown>
  outstanding?: number
}): MissingDataGate {
  return {
    check: vi.fn(),
    dismiss: vi.fn(),
    collect: vi.fn(),
    outstanding: over.outstanding ?? 1,
    missing: over.missing === undefined ? MISSING : over.missing,
    plan: { isError: false, isPending: false, error: null, ...over.plan },
    collection: {
      isPending: false,
      isSuccess: false,
      isError: false,
      error: null,
      data: undefined,
      ...over.collection,
    },
  } as unknown as MissingDataGate
}

function show(g: MissingDataGate, { launching = false, run = vi.fn() } = {}) {
  return renderWithProviders(<MissingDataPrompt gate={g} onRunAnyway={run} launching={launching} />)
}

describe('MissingDataPrompt', () => {
  it('renders nothing when nothing was found missing', () => {
    expect(show(gate({ missing: null })).container).toBeEmptyDOMElement()
  })

  it('says how many collections were queued and where to follow them', () => {
    show(
      gate({
        collection: { isSuccess: true, data: [{ id: 'c1' }, { id: 'c2' }] },
        outstanding: 0,
      }),
    )

    expect(screen.getByRole('status')).toHaveTextContent(
      'Queued 2 collections. Follow them on Collect and run again once they have finished.',
    )
    expect(screen.getByRole('link', { name: 'Collect' })).toHaveAttribute('href', '/collect')
  })

  it('reads as English for a single collection', () => {
    show(gate({ collection: { isSuccess: true, data: [{ id: 'c1' }] }, outstanding: 0 }))
    expect(screen.getByRole('status')).toHaveTextContent(
      'Queued 1 collection. Follow it on Collect and run again once it has finished.',
    )
  })

  it('refuses to queue again what this screen already queued', () => {
    show(gate({ outstanding: 0 }))
    expect(screen.getByRole('button', { name: 'Already queued' })).toBeDisabled()
  })

  it('offers to queue what is still outstanding', () => {
    const g = gate({})
    show(g)
    fireEvent.click(screen.getByRole('button', { name: 'Collect what is missing' }))
    expect(g.collect).toHaveBeenCalledTimes(1)
  })

  it('says what was queued before a refusal, and the refusal', () => {
    // ⚠️ Those two are on the queue. Showing only the error would invite a second press that
    // queues them again.
    const stopped = new CollectionStopped(
      [{ id: 'c1' }, { id: 'c2' }] as never,
      new ApiError(409, 'cannot tell what kind of instrument XAUUSD is'),
    )
    show(gate({ collection: { isError: true, error: stopped } }))

    expect(screen.getByRole('status')).toHaveTextContent('Queued 2 collections.')
    expect(
      screen.getByText('The rest was not queued: cannot tell what kind of instrument XAUUSD is'),
    ).toBeInTheDocument()
  })

  it('says nothing was queued when the first request was refused', () => {
    const stopped = new CollectionStopped([], new ApiError(409, 'no'))
    show(gate({ collection: { isError: true, error: stopped } }))

    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(screen.getByText('Nothing was queued: no')).toBeInTheDocument()
  })

  it('disables the run while the launch is on its way', () => {
    // A double click would create the run twice.
    show(gate({}), { launching: true })
    expect(screen.getByRole('button', { name: 'Enqueuing…' })).toBeDisabled()
  })

  it('still offers the run when the plan could not be asked', () => {
    const run = vi.fn()
    show(
      gate({ missing: null, plan: { isError: true, error: new ApiError(404, 'Not Found') } }),
      { run },
    )

    expect(screen.getByRole('region', { name: 'missing data' })).toHaveTextContent(
      'Could not check which data is on disk: Not Found',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Run anyway' }))
    expect(run).toHaveBeenCalledTimes(1)
  })
})
