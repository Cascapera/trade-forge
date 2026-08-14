import { fireEvent, screen } from '@testing-library/react'

import { useSession } from '../store'
import { renderWithProviders } from '../test-utils'

const { save, run } = vi.hoisted(() => ({ save: vi.fn(), run: vi.fn() }))

vi.mock('../api/hooks', () => ({
  useSaveStrategy: () => ({ mutate: save, isPending: false, isError: false, error: null }),
  useCreateBacktest: () => ({ mutate: run, isPending: false, isError: false, error: null }),
  useInstruments: () => ({ data: [{ id: 'i1', symbol: 'AAPL' }] }),
}))

import { StrategyBuilder } from './StrategyBuilder'

/** Fill in what only a person can decide, so the run is otherwise ready. */
function answerTheOpenQuestions(side = 'long'): void {
  const sideField = screen.queryByLabelText('setup side')
  if (sideField !== null) fireEvent.change(sideField, { target: { value: side } })
  fireEvent.change(screen.getByLabelText('symbol'), { target: { value: 'AAPL' } })
}

/** Both calls succeed: the strategy is saved, then the backtest is enqueued. */
function succeed(strategyId = 's1', backtestId = 'b1'): void {
  save.mockImplementation(
    (
      payload: { definition: { name: string } },
      options: { onSuccess: (s: { id: string; name: string }) => void },
    ) => {
      options.onSuccess({ id: strategyId, name: payload.definition.name })
    },
  )
  run.mockImplementation((_payload: unknown, options: { onSuccess: (b: { id: string }) => void }) => {
    options.onSuccess({ id: backtestId })
  })
}

// The screen reads the wall clock to stamp a run's name, so the clock is pinned. Built from local
// components, which makes the rendered digits the same on a machine in São Paulo and on a CI runner
// in UTC — see `naming.ts` for why the stamp is local time in the first place.
const PICKED_AT = new Date(2026, 7, 7, 15, 12, 30)

beforeEach(() => {
  vi.useFakeTimers({ shouldAdvanceTime: true })
  vi.setSystemTime(PICKED_AT)
})

afterEach(() => {
  vi.useRealTimers()
  vi.clearAllMocks()
  useSession.getState().clear()
})

describe('the strategy picker', () => {
  it('offers every setup by name, and the condition examples, in two groups', () => {
    renderWithProviders(<StrategyBuilder />)
    const picker = screen.getByLabelText('strategy')
    expect(Array.from(picker.querySelectorAll('option')).map((o) => o.textContent)).toEqual([
      'MME9 breakout',
      'Ponto Contínuo',
      'Structure — CHoCH',
      'Structure — Continuation',
      'Moving-average cross',
      'RSI oversold',
    ])
    expect(Array.from(picker.querySelectorAll('optgroup')).map((g) => g.label)).toEqual([
      'Setups',
      'Conditions',
    ])
  })

  it('opens on a setup, so the strategies are visible without choosing a mode first', () => {
    renderWithProviders(<StrategyBuilder />)
    expect(screen.getByLabelText('strategy')).toHaveValue('mme9_breakout')
    expect(screen.getByLabelText('setup period')).toHaveValue(9)
  })

  it('loads the chosen setup, defaults and all', () => {
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'ponto_continuo' } })

    expect(screen.getByLabelText('setup period')).toHaveValue(20)
    expect(screen.getByLabelText('setup average')).toHaveValue('EMA')
    // The name is stamped, not typed: the abbreviation of the setup and the instant it was picked.
    expect(screen.getByLabelText('name')).toHaveValue('PCONT-20260807-151230')
    // The author's target, on every setup.
    expect(screen.getByLabelText('take profit rr')).toHaveValue(5)
  })

  it('stamps a fresh name every time a strategy is picked', () => {
    // Picking a strategy is the act that starts a new lineage, so it is the act that mints a new
    // name. Two picks a minute apart must not collide — that collision is the 409 this replaces.
    renderWithProviders(<StrategyBuilder />)

    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'structure_choch' } })
    expect(screen.getByLabelText('name')).toHaveValue('SCHOCH-20260807-151230')

    vi.setSystemTime(new Date(2026, 7, 7, 15, 13, 45))
    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'structure_choch' } })
    expect(screen.getByLabelText('name')).toHaveValue('SCHOCH-20260807-151345')
  })

  it('keeps the stamped name while a parameter is nudged', () => {
    // The other half of the design, and the reason the clock is read on *pick* rather than on
    // launch: editing a parameter and running again is the same lineage's next version, so the
    // name has to hold still. A name re-stamped per launch would make every run a new lineage.
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'ponto_continuo' } })

    vi.setSystemTime(new Date(2026, 7, 7, 16, 0, 0))
    fireEvent.change(screen.getByLabelText('setup period'), { target: { value: '30' } })

    expect(screen.getByLabelText('setup period')).toHaveValue(30)
    expect(screen.getByLabelText('name')).toHaveValue('PCONT-20260807-151230')
  })

  it('still lets the name be typed over', () => {
    // Generated, not imposed. Labelling a run "the wide-stop test" stays possible.
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'teste do stop largo' } })
    expect(screen.getByLabelText('name')).toHaveValue('teste do stop largo')
  })

  it('swaps the whole form when a condition strategy is chosen', () => {
    renderWithProviders(<StrategyBuilder />)
    expect(screen.queryByLabelText('Long left 0')).not.toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'ma_cross' } })

    // A setup owns its own indicators, entry and stop; a condition document is the other shape, and
    // the two are never offered together because the API refuses a document carrying both.
    expect(screen.getByLabelText('Long left 0')).toBeInTheDocument()
    expect(screen.queryByLabelText('setup period')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Stop at candle extreme')).toBeInTheDocument()
  })

  it('drops side when the structure family is chosen, since it is not directional', () => {
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('strategy'), {
      target: { value: 'structure_continuation' },
    })
    expect(screen.queryByLabelText('setup side')).not.toBeInTheDocument()
    expect(screen.getByLabelText('setup stop_buffer')).toHaveValue(0.1)
    // `max_bos` defaults to null, which is uncapped — an empty box, not a zero.
    expect(screen.getByLabelText('setup max_bos')).toHaveValue(null)
  })
})

describe('running the backtest', () => {
  it('will not start until the questions only a person can answer are answered', () => {
    renderWithProviders(<StrategyBuilder />)
    const button = screen.getByRole('button', { name: /run backtest/i })

    // `side` has no schema default on purpose: the engine classes fall back to long, and a
    // pre-selected field would turn a forgotten choice into a long-only run read as the result.
    expect(screen.getByLabelText('setup side')).toHaveValue('')
    expect(button).toBeDisabled()

    fireEvent.change(screen.getByLabelText('setup side'), { target: { value: 'short' } })
    expect(button).toBeDisabled()
    expect(screen.getByText(/choose an instrument/i)).toBeInTheDocument()

    fireEvent.change(screen.getByLabelText('symbol'), { target: { value: 'AAPL' } })
    expect(button).toBeEnabled()
  })

  it('says why it cannot start when the window is backwards', () => {
    renderWithProviders(<StrategyBuilder />)
    answerTheOpenQuestions()
    fireEvent.change(screen.getByLabelText('from'), { target: { value: '2024-12-31' } })
    fireEvent.change(screen.getByLabelText('to'), { target: { value: '2024-01-01' } })

    expect(screen.getByText(/window ends before it starts/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run backtest/i })).toBeDisabled()
  })

  it('saves the strategy, enqueues the backtest and goes to the results', () => {
    succeed('s1', 'b1')
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'ponto_continuo' } })
    answerTheOpenQuestions()
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    expect(save).toHaveBeenCalledTimes(1)
    // ⚠️ The document, and only the document. Whether this is a new lineage or the next version
    // of one is no longer the screen's call — it used to be decided from the id *this tab* had
    // created, which is exactly why saving a name from another tab was a 409. `useSaveStrategy`
    // asks the server now, and its own test pins that.
    expect(save.mock.calls[0]?.[0]).toMatchObject({
      definition: { setup: { type: 'ponto_continuo', params: { side: 'long', period: 20 } } },
    })

    expect(run).toHaveBeenCalledTimes(1)
    expect(run.mock.calls[0]?.[0]).toMatchObject({
      strategy_id: 's1',
      symbol: 'AAPL',
      // The strategy's own timeframe, not a second one the screen asked for separately.
      timeframe: 'H1',
      date_from: '2024-01-01T00:00:00Z',
      date_to: '2024-12-31T00:00:00Z',
      initial_capital: '10000',
      cost_model: { type: 'none' },
    })
    expect(useSession.getState().strategyId).toBe('s1')
  })

  it('saves the next version when the same name is run again', () => {
    // `POST` always writes version 1 and (name, version) is unique, so iterating on a parameter can
    // only work as a new version of the same lineage. Without this, nudging the period and running
    // again would be a 409 every time.
    succeed('s1', 'b1')
    renderWithProviders(<StrategyBuilder />)
    answerTheOpenQuestions()
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    fireEvent.change(screen.getByLabelText('setup period'), { target: { value: '21' } })
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    // The edited document reaches the save both times; which of them becomes a new version is
    // `useSaveStrategy`'s decision, taken against the database rather than against this tab.
    expect(save).toHaveBeenCalledTimes(2)
    expect(save.mock.calls[1]?.[0]).toMatchObject({
      definition: { setup: { params: { period: 21 } } },
    })
  })

  it('starts a fresh lineage when the name changes', () => {
    succeed('s1', 'b1')
    renderWithProviders(<StrategyBuilder />)
    answerTheOpenQuestions()
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    fireEvent.change(screen.getByLabelText('name'), { target: { value: 'MME9 wider stop' } })
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    // The new name reaches the save; that a new name starts a new lineage is now a fact the
    // server settles, not one the screen assumes.
    expect(save.mock.calls[1]?.[0]).toMatchObject({
      definition: { name: 'MME9 wider stop' },
    })
  })

  it('carries the spread through when costs are switched on', () => {
    succeed()
    renderWithProviders(<StrategyBuilder />)
    answerTheOpenQuestions()
    fireEvent.change(screen.getByLabelText('cost model'), { target: { value: 'spread' } })
    fireEvent.change(screen.getByLabelText('spread points'), { target: { value: '25' } })
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    expect(run.mock.calls[0]?.[0]).toMatchObject({
      cost_model: { type: 'spread', spread_points: 25 },
    })
  })

  it('does not enqueue a backtest when the strategy could not be saved', () => {
    save.mockImplementation(() => {
      /* the mutation fails, so onSuccess never runs */
    })
    renderWithProviders(<StrategyBuilder />)
    answerTheOpenQuestions()
    fireEvent.click(screen.getByRole('button', { name: /run backtest/i }))

    expect(save).toHaveBeenCalledTimes(1)
    expect(run).not.toHaveBeenCalled()
  })
})

describe('editing a condition strategy still works', () => {
  it('edits indicators, sides and exits', () => {
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'ma_cross' } })

    fireEvent.click(screen.getByRole('button', { name: '+ indicator' }))
    const ids = screen.getAllByLabelText('indicator id')
    const last = ids.length - 1
    fireEvent.change(ids[last]!, { target: { value: 'mid' } })
    fireEvent.change(screen.getAllByLabelText('indicator kind')[last]!, { target: { value: 'EMA' } })
    fireEvent.change(screen.getAllByLabelText('indicator period')[last]!, {
      target: { value: '50' },
    })
    fireEvent.change(screen.getAllByLabelText('indicator source')[last]!, {
      target: { value: 'high' },
    })
    const removeButtons = screen.getAllByRole('button', { name: 'remove' })
    fireEvent.click(removeButtons[removeButtons.length - 1]!)

    fireEvent.change(screen.getByLabelText('Long left 0'), { target: { value: 'fast' } })
    fireEvent.change(screen.getByLabelText('Long op 0'), { target: { value: 'gt' } })
    fireEvent.change(screen.getByLabelText('Long right 0'), { target: { value: 'slow' } })
    fireEvent.click(screen.getAllByRole('button', { name: '+ condition' })[0]!)
    fireEvent.change(screen.getByLabelText('Long combine'), { target: { value: 'any' } })
    const longRemoves = screen.getAllByRole('button', { name: 'remove' })
    fireEvent.click(longRemoves[longRemoves.length - 1]!)

    fireEvent.click(screen.getByLabelText('Short'))
    fireEvent.click(screen.getAllByRole('button', { name: '+ condition' })[1]!)
    fireEvent.change(screen.getByLabelText('Short right 0'), { target: { value: 'slow' } })

    fireEvent.change(screen.getByLabelText('stop lookback'), { target: { value: '3' } })
    fireEvent.change(screen.getByLabelText('stop side'), { target: { value: 'high' } })
    fireEvent.change(screen.getByLabelText('take profit rr'), { target: { value: '1.5' } })
    fireEvent.click(screen.getByLabelText('Stop at candle extreme'))
    fireEvent.click(screen.getByLabelText('Take profit at R:R'))

    fireEvent.change(screen.getByLabelText('percent'), { target: { value: '2' } })
    fireEvent.change(screen.getByLabelText('timeframe'), { target: { value: 'M15' } })
    expect(screen.getByLabelText('timeframe')).toHaveValue('M15')
  })

  it('shows the schema errors when the document is not valid yet', () => {
    renderWithProviders(<StrategyBuilder />)
    fireEvent.change(screen.getByLabelText('strategy'), { target: { value: 'ma_cross' } })
    fireEvent.change(screen.getByLabelText('name'), { target: { value: '' } })
    expect(screen.getByText(/not valid yet/i)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /run backtest/i })).toBeDisabled()
  })
})
