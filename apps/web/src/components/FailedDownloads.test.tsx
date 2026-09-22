import { render, screen } from '@testing-library/react'

import type { Collection } from '../api/types'
import { FailedDownloads } from './FailedDownloads'

function download(over: Partial<Collection>): Collection {
  return {
    id: 'c1',
    symbol: 'GBPUSD',
    timeframe: 'M15',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-12-31T00:00:00Z',
    asset_class: null,
    status: 'failed',
    years_done: 0,
    years_total: 1,
    candles: null,
    gaps: null,
    error: 'the terminal said no',
    requested_at: '',
    started_at: null,
    finished_at: null,
    ...over,
  }
}

describe('the downloads that failed under a group of runs', () => {
  it('says nothing when nothing failed', () => {
    const { container } = render(<FailedDownloads downloads={[]} />)
    expect(container).toBeEmptyDOMElement()
  })

  it('names each download with its reason, and what the runs did about it', () => {
    // His rule (22/09): the runs went ahead on what was on disk. Naming only the failure would
    // read as runs that never happened, which is what `skipped` means and this does not.
    render(
      <FailedDownloads
        downloads={[
          download({}),
          download({ id: 'c2', symbol: 'EURUSD', timeframe: 'H1', error: null }),
        ]}
      />,
    )
    const note = screen.getByRole('status', { name: 'failed downloads' })
    expect(note).toHaveTextContent(
      '2 downloads failed — the runs that needed them went ahead on the data already on disk:',
    )
    expect(note).toHaveTextContent('GBPUSD M15 — the terminal said no')
    // A reason nobody recorded is said to be missing, not left as a dangling dash.
    expect(note).toHaveTextContent('EURUSD H1 — no reason recorded')
  })

  it('speaks in the singular for one', () => {
    render(<FailedDownloads downloads={[download({})]} />)
    expect(screen.getByRole('status')).toHaveTextContent(
      'A download failed — the runs that needed it went ahead',
    )
  })
})
