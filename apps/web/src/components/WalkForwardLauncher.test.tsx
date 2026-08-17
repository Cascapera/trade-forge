import { fireEvent, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { ApiError, api } from '../api/client'
import type { StudyOut } from '../api/types'
import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

import { WalkForwardLauncher } from './WalkForwardLauncher'

// The client is mocked and the hook is not: the payload this form builds is the thing under
// test, and a mocked hook would let the form send `"4"` where the API wants `4` with nothing
// able to notice until a real request came back 422.
vi.mock('../api/client', async () => {
  const actual = await vi.importActual<typeof import('../api/client')>('../api/client')
  return { ...actual, api: { ...actual.api, createWalkForward: vi.fn() } }
})

const createWalkForward = vi.mocked(api.createWalkForward)

/** A finished study of `points` grid points — all this form reads off one. */
function study(points: number): StudyOut {
  return {
    id: 'study-1',
    strategy_id: 'base',
    strategy_name: 'MME9 breakout',
    symbol: 'AAPL',
    timeframe: 'H1',
    date_from: '2024-01-01T00:00:00Z',
    date_to: '2024-02-01T00:00:00Z',
    initial_capital: '10000',
    created_at: '2024-01-01T00:00:00Z',
    grid: { 'setup.params.period': [5, 9] },
    points: [],
    aggregate: {
      points_total: points,
      points_finished: points,
      points_failed: 0,
      points_profitable: 0,
      best_label: null,
      best_return: null,
      worst_label: null,
      worst_return: null,
      median_return: null,
    },
    runs: [],
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  createWalkForward.mockResolvedValue({ id: 'wf-1', runs_queued: 300, folds: [] })
  useSession.setState({ walkForwardId: null, walkForwardLabel: null })
})

describe('WalkForwardLauncher', () => {
  it('states what the experiment will cost before the button, and keeps it true', () => {
    // ⚠️ **Every fold re-runs the whole grid**, so the cost is `points × folds` — not the grid
    // size the reader just watched land. Fifty points feels launched already; five hundred
    // backtests is a different decision, and it has to be visible while the fold count is still
    // being chosen rather than after the button.
    renderWithProviders(<WalkForwardLauncher study={study(50)} />)

    expect(screen.getByRole('status')).toHaveTextContent('50 points over 6 folds — 300 backtests')

    fireEvent.change(screen.getByLabelText('Folds'), { target: { value: '10' } })

    expect(screen.getByRole('status')).toHaveTextContent('50 points over 10 folds — 500 backtests')
  })

  it('will not launch a grid with nothing in it', () => {
    // A study whose points all failed has nothing to choose between, so every fold would choose
    // nothing and the report would be six empty rows — an experiment that ran and says nothing,
    // which reads exactly like one whose method did not survive.
    renderWithProviders(<WalkForwardLauncher study={study(0)} />)

    expect(screen.getByRole('button', { name: 'Run the walk-forward' })).toBeDisabled()
  })

  it('sends the numbers as numbers, and every choice the form offered', async () => {
    // ⚠️ Both fields hand back **text**. `folds: "4"` fails the request validator, which is the
    // good case; the one to fear is a form that quietly stopped carrying the metric or the
    // checkbox, because then the experiment runs to completion and answers a question the
    // reader did not ask.
    renderWithProviders(<WalkForwardLauncher study={study(50)} />)

    fireEvent.change(screen.getByLabelText('Folds'), { target: { value: '4' } })
    fireEvent.change(screen.getByLabelText('Training window'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('Chosen by'), { target: { value: 'sharpe' } })
    fireEvent.click(screen.getByLabelText('Anchored training'))
    fireEvent.click(screen.getByRole('button', { name: 'Run the walk-forward' }))

    await waitFor(() => {
      expect(createWalkForward).toHaveBeenCalledWith({
        study_id: 'study-1',
        folds: 4,
        train_multiple: 2,
        anchored: true,
        metric: 'sharpe',
      })
    })
  })

  it('says which question the training window is answering, and swaps it with the checkbox', () => {
    // The checkbox is one word, and the two settings answer different questions: rolling makes
    // the folds comparable to each other, anchored uses more history and gives that up. Without
    // the sentence, "Anchored training" is a switch whose consequence lives in a spec file.
    renderWithProviders(<WalkForwardLauncher study={study(50)} />)

    expect(screen.getByText(/^Rolling:/)).toBeInTheDocument()

    fireEvent.click(screen.getByLabelText('Anchored training'))

    expect(screen.getByText(/^Anchored:/)).toBeInTheDocument()
    expect(screen.queryByText(/^Rolling:/)).not.toBeInTheDocument()
  })

  it('remembers the experiment it launched, so the screen it opens can name it', async () => {
    renderWithProviders(<WalkForwardLauncher study={study(50)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Run the walk-forward' }))

    await waitFor(() => {
      expect(useSession.getState().walkForwardId).toBe('wf-1')
    })
    // The market and timeframe, because a walk-forward is read minutes after it is launched and
    // the id alone says nothing about which experiment it was.
    expect(useSession.getState().walkForwardLabel).toBe('AAPL H1 walk-forward')
  })

  it('shows why the server refused, not merely that it did', async () => {
    // ⚠️ `ApiError.message` is built from the status alone, so a screen that printed it would
    // show "API error 422" over a form whose fold count is the thing to change. The reason is in
    // `detail`, and `launchFailure` is where the two are told apart — the same defect #106 fixed
    // for the study form.
    createWalkForward.mockRejectedValue(
      new ApiError(422, '90 candles cut into 4 folds leaves 12 per test window'),
    )
    renderWithProviders(<WalkForwardLauncher study={study(50)} />)

    fireEvent.click(screen.getByRole('button', { name: 'Run the walk-forward' }))

    expect(await screen.findByRole('alert')).toHaveTextContent(
      '90 candles cut into 4 folds leaves 12 per test window',
    )
  })
})
