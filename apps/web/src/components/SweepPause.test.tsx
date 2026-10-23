import { fireEvent, render, screen } from '@testing-library/react'

vi.mock('../api/hooks', () => ({
  usePauseSweep: vi.fn(),
}))

import { usePauseSweep } from '../api/hooks'

import { SweepPause } from './SweepPause'

const hook = vi.mocked(usePauseSweep)
const mutate = vi.fn()

function state(over: Record<string, unknown> = {}): never {
  return {
    mutate,
    isPending: false,
    isError: false,
    error: null,
    data: undefined,
    ...over,
  } as never
}

beforeEach(() => {
  mutate.mockReset()
  hook.mockReturnValue(state())
})

describe('SweepPause', () => {
  it('offers a pause while runs wait or run, and asks for it', () => {
    render(<SweepPause sweepId="s1" pausedAt={null} counts={{ queued: 10, running: 2 }} />)

    fireEvent.click(screen.getByRole('button', { name: 'Pause' }))

    expect(mutate).toHaveBeenCalledWith('pause')
  })

  it('says to wait while runs a worker holds are finishing', () => {
    render(
      <SweepPause
        sweepId="s1"
        pausedAt="2026-10-03T12:00:00Z"
        counts={{ queued: 10, running: 2 }}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent(
      '2 runs still finishing; wait before turning off',
    )
  })

  it('says when it is safe to turn off, and resumes', () => {
    render(
      <SweepPause
        sweepId="s1"
        pausedAt="2026-10-03T12:00:00Z"
        counts={{ queued: 1, running: 0 }}
      />,
    )

    expect(screen.getByRole('status')).toHaveTextContent('Paused — safe to turn off. 1 run waiting')
    fireEvent.click(screen.getByRole('button', { name: 'Resume' }))
    expect(mutate).toHaveBeenCalledWith('resume')
  })

  it('shows nothing for a sweep with nothing left to run', () => {
    const { container } = render(
      <SweepPause sweepId="s1" pausedAt={null} counts={{ queued: 0, running: 0 }} />,
    )

    expect(container).toBeEmptyDOMElement()
  })

  it('says it is working, why it failed, and what a resume released', () => {
    hook.mockReturnValue(state({ isPending: true }))
    const { rerender } = render(
      <SweepPause sweepId="s1" pausedAt={null} counts={{ queued: 3, running: 0 }} />,
    )
    expect(screen.getByRole('button', { name: 'Pausing…' })).toBeDisabled()

    hook.mockReturnValue(state({ isError: true, error: new Error('boom') }))
    rerender(
      <SweepPause
        sweepId="s1"
        pausedAt="2026-10-03T12:00:00Z"
        counts={{ queued: 3, running: 1 }}
      />,
    )
    expect(screen.getByRole('alert')).toHaveTextContent('Could not resume: boom')

    hook.mockReturnValue(state({ data: { paused_at: null, queued: 3, running: 0, released: 2 } }))
    rerender(<SweepPause sweepId="s1" pausedAt={null} counts={{ queued: 3, running: 0 }} />)
    expect(screen.getByText(/2 runs left running by a machine turned off/)).toBeInTheDocument()
  })
})
