import { fireEvent, screen } from '@testing-library/react'

import { ApiError } from '../api/client'
import type { PlannedCollection } from '../api/types'
import type { MissingDataGate } from '../collect/gate'
import { renderWithProviders } from '../test-utils'
import { MissingDataPrompt } from './MissingDataPrompt'

const MISSING: PlannedCollection[] = [
  {
    symbol: 'GBPUSD',
    timeframe: 'H1',
    covers: null,
    in_window: false,
    windows: [{ date_from: '2024-01-01T00:00:00Z', date_to: '2024-12-31T23:59:59.999999Z' }],
  },
]

function gate(over: {
  missing?: PlannedCollection[] | null
  plan?: Record<string, unknown>
}): MissingDataGate {
  return {
    check: vi.fn(),
    dismiss: vi.fn(),
    missing: over.missing === undefined ? MISSING : over.missing,
    plan: { isError: false, isPending: false, error: null, ...over.plan },
  } as unknown as MissingDataGate
}

function show(
  g: MissingDataGate,
  { launching = false, run = vi.fn(), collectAndRun = vi.fn(), canRun = true } = {},
) {
  return renderWithProviders(
    <MissingDataPrompt
      gate={g}
      onRunAnyway={run}
      onCollectAndRun={collectAndRun}
      launching={launching}
      canRun={canRun}
    />,
  )
}

describe('MissingDataPrompt', () => {
  it('renders nothing when nothing was found missing', () => {
    expect(show(gate({ missing: null })).container).toBeEmptyDOMElement()
  })

  it('names every market that is short, and what would be fetched for it', () => {
    show(gate({}))
    expect(screen.getByRole('region', { name: 'missing data' })).toHaveTextContent(
      'GBPUSD H1 — never collected; would fetch 2024',
    )
  })

  it('hands the collecting to the server in one press', () => {
    // ⚠️ The screen sends no windows: the server plans them, queues the downloads and links them
    // to the runs, which start by themselves.
    const collectAndRun = vi.fn()
    show(gate({}), { collectAndRun })

    fireEvent.click(screen.getByRole('button', { name: 'Collect and run' }))
    expect(collectAndRun).toHaveBeenCalledTimes(1)
  })

  it('disables both answers while a launch is on its way', () => {
    // A double click would create the runs twice.
    show(gate({}), { launching: true })
    for (const button of screen.getAllByRole('button')) expect(button).toBeDisabled()
  })

  it('does not offer a run that would read nothing', () => {
    // The launch would answer 422; the only thing left to do here is collect.
    show(gate({}), { canRun: false })

    expect(screen.queryByRole('button', { name: 'Run with what there is' })).not.toBeInTheDocument()
    expect(screen.getByText('Nothing would run until this is collected.')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Collect and run' })).toBeEnabled()
  })

  it('offers both answers when the plan could not be asked', () => {
    // ⚠️ Collect and run needs no plan: the server makes its own. Offering only "Run anyway"
    // would send the person into the refusal this whole flow exists to avoid.
    const collectAndRun = vi.fn()
    const run = vi.fn()
    show(gate({ missing: null, plan: { isError: true, error: new ApiError(404, 'Not Found') } }), {
      collectAndRun,
      run,
    })

    expect(screen.getByRole('region', { name: 'missing data' })).toHaveTextContent(
      'Could not check which data is on disk: Not Found',
    )
    fireEvent.click(screen.getByRole('button', { name: 'Collect and run' }))
    fireEvent.click(screen.getByRole('button', { name: 'Run anyway' }))
    expect(collectAndRun).toHaveBeenCalledTimes(1)
    expect(run).toHaveBeenCalledTimes(1)
  })
})
