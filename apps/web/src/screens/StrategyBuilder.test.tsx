import { fireEvent, screen } from '@testing-library/react'

import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

const { mutate } = vi.hoisted(() => ({ mutate: vi.fn() }))

vi.mock('../api/hooks', () => ({
  useCreateStrategy: () => ({ mutate, isPending: false, isError: false }),
}))

import { StrategyBuilder } from './StrategyBuilder'

afterEach(() => {
  vi.clearAllMocks()
  useSession.getState().clear()
})

describe('StrategyBuilder', () => {
  it('starts from the MA-cross template and can save it', () => {
    mutate.mockImplementation(
      (_document: unknown, options: { onSuccess: (s: { id: string; name: string }) => void }) => {
        options.onSuccess({ id: 's1', name: 'MA cross' })
      },
    )
    renderWithProviders(<StrategyBuilder />)

    const save = screen.getByRole('button', { name: /save & configure/i })
    expect(save).toBeEnabled()

    fireEvent.click(save)
    expect(mutate).toHaveBeenCalledTimes(1)
    // On success the session records the new strategy so the launch screen can use it.
    expect(useSession.getState().strategyId).toBe('s1')
  })

  it('blocks saving and shows errors when the strategy is invalid', () => {
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('name'), { target: { value: '' } })
    expect(screen.getByText(/not valid yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /save & configure/i })).toBeDisabled()
  })

  it('edits indicators, sides and exits', () => {
    renderWithProviders(<StrategyBuilder />)

    // Indicators: add one, edit every field, then remove it.
    fireEvent.click(screen.getByRole('button', { name: '+ indicator' }))
    const ids = screen.getAllByLabelText('indicator id')
    const last = ids.length - 1
    fireEvent.change(ids[last]!, { target: { value: 'mid' } })
    fireEvent.change(screen.getAllByLabelText('indicator kind')[last]!, {
      target: { value: 'EMA' },
    })
    fireEvent.change(screen.getAllByLabelText('indicator period')[last]!, {
      target: { value: '50' },
    })
    fireEvent.change(screen.getAllByLabelText('indicator source')[last]!, {
      target: { value: 'high' },
    })
    const removeButtons = screen.getAllByRole('button', { name: 'remove' })
    fireEvent.click(removeButtons[removeButtons.length - 1]!)

    // Long side: edit the existing row, add a second so the all/any selector appears, switch it.
    fireEvent.change(screen.getByLabelText('Long left 0'), { target: { value: 'fast' } })
    fireEvent.change(screen.getByLabelText('Long op 0'), { target: { value: 'gt' } })
    fireEvent.change(screen.getByLabelText('Long right 0'), { target: { value: 'slow' } })
    fireEvent.click(screen.getAllByRole('button', { name: '+ condition' })[0]!)
    fireEvent.change(screen.getByLabelText('Long combine'), { target: { value: 'any' } })
    // Remove the row we just added.
    const longRemoves = screen.getAllByRole('button', { name: 'remove' })
    fireEvent.click(longRemoves[longRemoves.length - 1]!)

    // Enable the short side and give it a condition (its "+ condition" is the second one).
    fireEvent.click(screen.getByLabelText('Short'))
    fireEvent.click(screen.getAllByRole('button', { name: '+ condition' })[1]!)
    fireEvent.change(screen.getByLabelText('Short right 0'), { target: { value: 'slow' } })

    // Exit: adjust the stop and target, then toggle them off.
    fireEvent.change(screen.getByLabelText('stop lookback'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('stop side'), { target: { value: 'high' } })
    fireEvent.change(screen.getByLabelText('take profit rr'), { target: { value: '1.5' } })
    fireEvent.click(screen.getByLabelText('Stop at candle extreme'))
    fireEvent.click(screen.getByLabelText('Take profit at R:R'))

    // Scalars, and reloading the template.
    fireEvent.change(screen.getByLabelText('percent'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('timeframe'), { target: { value: 'M15' } })
    fireEvent.click(screen.getByRole('button', { name: /load ma-cross template/i }))

    expect(screen.getByLabelText('timeframe')).toHaveValue('H1')
  })
})
