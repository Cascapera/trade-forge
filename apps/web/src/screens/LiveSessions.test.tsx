import { fireEvent, screen, within } from '@testing-library/react'

import type { KillSwitch, LiveSession, LiveSessionDetail } from '../api/types'
import { renderWithProviders } from '../test-utils'

vi.mock('../api/hooks', () => ({
  useLiveSessions: vi.fn(),
  useLiveSession: vi.fn(),
  useSessionEvents: vi.fn(),
  useStopLiveSession: vi.fn(),
  useKillSwitch: vi.fn(),
  useEngageKillSwitch: vi.fn(),
}))

vi.mock('../api/useLiveSessionFeed', () => ({ useLiveSessionFeed: vi.fn() }))

import {
  useEngageKillSwitch,
  useKillSwitch,
  useLiveSession,
  useLiveSessions,
  useSessionEvents,
  useStopLiveSession,
} from '../api/hooks'
import { useLiveSessionFeed } from '../api/useLiveSessionFeed'
import { LiveSessions } from './LiveSessions'

const sessions = vi.mocked(useLiveSessions)
const session = vi.mocked(useLiveSession)
const events = vi.mocked(useSessionEvents)
const stop = vi.mocked(useStopLiveSession)
const killSwitch = vi.mocked(useKillSwitch)
const engage = vi.mocked(useEngageKillSwitch)
const feed = vi.mocked(useLiveSessionFeed)

function listed(over: Partial<LiveSession> = {}): LiveSession {
  return {
    id: 'sess-1',
    strategy_id: 's1',
    instrument_id: 'i1',
    symbol: 'EURUSD',
    timeframe: 'H1',
    mode: 'paper',
    status: 'running',
    initial_capital: '10000',
    engine_version: '0.1.0',
    warmup_bars: 120,
    started_at: '2026-09-01T10:00:00Z',
    stopped_at: null,
    heartbeat_at: '2026-09-01T12:00:00Z',
    last_bar_time: '2026-09-01T11:00:00Z',
    error: null,
    stale: false,
    silent_for_seconds: 4,
    ...over,
  }
}

function detailed(over: Partial<LiveSessionDetail> = {}): LiveSessionDetail {
  return {
    ...listed(),
    stop_requested_at: null,
    open_positions: [],
    realised_today: '0',
    trades_closed_today: 0,
    ...over,
  }
}

function switchState(over: Partial<KillSwitch> = {}): KillSwitch {
  return { engaged: false, engaged_at: null, layer: 'redis:executor:kill-switch', ...over }
}

interface Stubs {
  rows?: LiveSession[]
  detail?: LiveSessionDetail
  killer?: KillSwitch
  connected?: boolean
  feedEvents?: ReturnType<typeof useLiveSessionFeed>['events']
  stopping?: boolean
}

function arrange(stubs: Stubs = {}): { mutate: ReturnType<typeof vi.fn>; engaged: ReturnType<typeof vi.fn> } {
  const mutate = vi.fn()
  const engaged = vi.fn()
  const rows = stubs.rows ?? [listed()]
  sessions.mockReturnValue({
    data: { total: rows.length, sessions: rows },
    isPending: false,
  } as unknown as ReturnType<typeof useLiveSessions>)
  // The detail defaults to *the row that is selected*, not to a fresh healthy session. Two shapes
  // of the same session disagreeing is a fixture that cannot happen in production, and a test
  // built on one proves something about a state the app never reaches.
  session.mockReturnValue({
    data: stubs.detail ?? detailed(rows[0]),
    isError: false,
  } as unknown as ReturnType<typeof useLiveSession>)
  events.mockReturnValue({ data: { total: 0, events: [] } } as unknown as ReturnType<
    typeof useSessionEvents
  >)
  stop.mockReturnValue({ mutate, isPending: stubs.stopping ?? false, isError: false } as unknown as ReturnType<
    typeof useStopLiveSession
  >)
  killSwitch.mockReturnValue({
    data: stubs.killer ?? switchState(),
    isError: false,
  } as unknown as ReturnType<typeof useKillSwitch>)
  engage.mockReturnValue({ mutate: engaged, isPending: false } as unknown as ReturnType<
    typeof useEngageKillSwitch
  >)
  feed.mockReturnValue({ connected: stubs.connected ?? true, events: stubs.feedEvents ?? [] })
  return { mutate, engaged }
}

// --------------------------------------------------------------------------- //
// The two sentences this whole PR is about                                     //
// --------------------------------------------------------------------------- //

test('the kill switch says it does not close positions', () => {
  arrange()

  renderWithProviders(<LiveSessions />)

  // ⚠️ `specs/fase-3.md` says this button "encerra tudo" and it does not: `safety.admits` clears
  // exits, cancels and tightening stops *before* it consults the switches. Guilherme chose the
  // honest label over building a flatten, and this is where that decision is pinned — a button
  // that promises more than the mechanism delivers is believed at the one moment nobody checks.
  expect(screen.getByText(/does not close open positions/i)).toBeInTheDocument()
})

test('stopping a session says the position stays open', () => {
  arrange()

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/the open position stays open/i)).toBeInTheDocument()
  expect(screen.getByText(/nothing manages it/i)).toBeInTheDocument()
})

test('the panel says a stop or a kill needs a restart, not a resume', () => {
  arrange()

  renderWithProviders(<LiveSessions />)

  // Three refusals in a row retire a zone for the rest of the session, and releasing the switch
  // does not bring it back. Somebody who resumed instead of restarting would be running a
  // strategy quietly missing its setups.
  expect(screen.getByText(/restart the session rather than resuming it/i)).toBeInTheDocument()
})

// --------------------------------------------------------------------------- //
// Alive is not the same question as running                                    //
// --------------------------------------------------------------------------- //

test('a running session that stopped beating is shown as silent, not as running', () => {
  // ⚠️ **The mutant this exists for reads `status` first**, which is the obvious implementation
  // and paints the deadest sessions green. The row says `running` because the process that would
  // have updated it is the process that died — that is the whole reason `stale` is on the wire.
  arrange({ rows: [listed({ stale: true, silent_for_seconds: 240 })] })

  renderWithProviders(<LiveSessions />)

  // Scoped to the table: the same session is legitimately badged twice, here and in the detail
  // header below it, and both have to agree.
  const table = within(screen.getByRole('table'))
  expect(table.getByText(/silent 4m/i)).toBeInTheDocument()
  expect(table.queryByText('running')).not.toBeInTheDocument()
})

test('a beating session is shown as running', () => {
  arrange({ rows: [listed({ stale: false })] })

  renderWithProviders(<LiveSessions />)

  expect(within(screen.getByRole('table')).getByText('running')).toBeInTheDocument()
})

// --------------------------------------------------------------------------- //
// The kill switch                                                              //
// --------------------------------------------------------------------------- //

test('engaging the kill switch asks the API', () => {
  const { engaged } = arrange()

  renderWithProviders(<LiveSessions />)
  fireEvent.click(screen.getByRole('button', { name: /engage kill switch/i }))

  expect(engaged).toHaveBeenCalled()
})

test('an engaged switch offers no way to release it and names the shell command instead', () => {
  arrange({ killer: switchState({ engaged: true, engaged_at: '2026-09-01T12:30:00Z' }) })

  renderWithProviders(<LiveSessions />)

  // ⚠️ The absence is the decision, so the absence is what is asserted. An endpoint that can
  // un-kill is an endpoint a retry can un-kill; releasing is a shell command typed by somebody
  // looking at a running system.
  expect(screen.queryByRole('button', { name: /release|disengage|un-?kill/i })).toBeNull()
  expect(screen.getByText(/redis-cli DEL executor:kill-switch/)).toBeInTheDocument()
})

test('the panel says the two layers it cannot see exist', () => {
  arrange()

  renderWithProviders(<LiveSessions />)

  // `engaged: false` here means "the layer this API can write is not engaged" and nothing more.
  // A screen that read it as "the executor is armed" would be wrong whenever somebody had
  // touched the file on the executor's own disk.
  expect(screen.getByText(/one of three layers/i)).toBeInTheDocument()
})

// --------------------------------------------------------------------------- //
// Stopping                                                                     //
// --------------------------------------------------------------------------- //

test('a session already asked to stop still reads as running, and cannot be asked twice', () => {
  // ⚠️ Only the session writes `stopped_at`, when it has actually finished. A screen that flipped
  // the status itself would report an outcome it cannot observe — and on a session whose process
  // is already dead it would show `stopped` over a position sitting unmanaged at the venue.
  arrange({ detail: detailed({ stop_requested_at: '2026-09-01T12:31:00Z' }) })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByRole('button', { name: /stopping/i })).toBeDisabled()
  expect(screen.getByText(/Asked at/i)).toBeInTheDocument()
  // The word is inside a `<code>`, so the sentence is broken across elements — matched on the
  // element that carries the claim rather than with a looser regex that would also pass on a
  // screen that had merely mentioned the word somewhere.
  expect(screen.getByText('running', { selector: 'code' })).toBeInTheDocument()
})

test('stopping asks the API for the selected session', () => {
  const { mutate } = arrange()

  renderWithProviders(<LiveSessions />)
  fireEvent.click(screen.getByRole('button', { name: /stop session/i }))

  expect(mutate).toHaveBeenCalledWith('sess-1')
})

// --------------------------------------------------------------------------- //
// The feed                                                                     //
// --------------------------------------------------------------------------- //

test('a dropped socket is shown, not hidden', () => {
  // ⚠️ The screen keeps working when the socket drops — the queries go back to polling — but the
  // operator is entitled to know which of the two they are looking at before they decide how much
  // to trust a quiet screen. A panel silently degraded to polling looks exactly like a quiet market.
  arrange({ connected: false })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/polling/i)).toBeInTheDocument()
})

test('a refusal in the ticker says who refused it', () => {
  arrange({
    connected: true,
    feedEvents: [
      {
        type: 'refusal',
        client_id: 'zone-2',
        at: '2026-09-01T12:32:00Z',
        reason: 'kill switch engaged (redis:executor:kill-switch)',
        by_venue: false,
        retcode: null,
      },
    ],
  })

  renderWithProviders(<LiveSessions />)

  // Ours describe conditions that change on their own; the venue's usually do not. One word for
  // both would tell somebody to wait when they should be fixing something.
  expect(screen.getByText(/kill switch engaged/)).toBeInTheDocument()
  expect(screen.getByText(/\(safeguards\)/)).toBeInTheDocument()
})

// --------------------------------------------------------------------------- //
// What the session is holding, and what it has done today                      //
// --------------------------------------------------------------------------- //

test('an open position is shown with the stop it is carrying', () => {
  // ⚠️ The stop matters more than the entry on this screen: it is the only thing protecting the
  // trade once a stop or a kill has ended the session's management of it, and the two buttons
  // above both say so. A panel that showed the position without it would be hiding the answer to
  // the question those warnings raise.
  arrange({
    detail: detailed({
      open_positions: [
        {
          id: 1,
          direction: 'short',
          entry_time: '2026-09-01T11:00:00Z',
          entry_price: '1.11000',
          volume: '0.20',
          stop_loss: '1.12000',
          take_profit: null,
        },
      ],
    }),
  })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText('short')).toBeInTheDocument()
  expect(screen.getByText(/0\.20 @ 1\.11000/)).toBeInTheDocument()
  expect(screen.getByText(/stop 1\.12000/)).toBeInTheDocument()
})

test('a session holding nothing says so rather than showing an empty list', () => {
  arrange({ detail: detailed({ open_positions: [] }) })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/no open position/i)).toBeInTheDocument()
})

test("today's total is shown beside the number of trades it came from", () => {
  // ⚠️ Zero from no trades and zero from two that cancelled out are different days, and one
  // number cannot tell them apart. Same reason the API returns both.
  arrange({ detail: detailed({ realised_today: '0', trades_closed_today: 2 }) })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/over 2 closed/)).toBeInTheDocument()
})

test('the order log carries the rule that refused', () => {
  arrange()
  events.mockReturnValue({
    data: {
      total: 1,
      events: [
        {
          id: 'a1',
          client_id: 'zone-9',
          status: 'refused',
          reason: '0.11 is above the cap of 0.10',
          requested_at: '2026-09-01T12:20:00Z',
          resolved_at: '2026-09-01T12:20:00Z',
          request: {},
          response: null,
        },
      ],
    },
  } as unknown as ReturnType<typeof useSessionEvents>)

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/above the cap of 0\.10/)).toBeInTheDocument()
})

test('a kill switch that could not be read says so instead of reading clear', () => {
  // ⚠️ `engaged: false` and "I could not ask" are different statements, and only one of them is
  // safe to render as a calm panel. The API answers 503 rather than guessing; the screen must not
  // undo that by falling back to its default.
  arrange()
  killSwitch.mockReturnValue({ data: undefined, isError: true } as unknown as ReturnType<
    typeof useKillSwitch
  >)

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/could not read the kill switch/i)).toBeInTheDocument()
})

test('a list with nothing in it says no session was ever started', () => {
  arrange({ rows: [] })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/no session has ever been started/i)).toBeInTheDocument()
})

// --------------------------------------------------------------------------- //
// The states a session can be in, and the ones it can fail in                   //
// --------------------------------------------------------------------------- //

test.each([
  ['stopped' as const, 'stopped'],
  ['failed' as const, 'failed'],
])('a %s session is badged as such', (status, shown) => {
  arrange({ rows: [listed({ status, stopped_at: '2026-09-01T12:00:00Z' })] })

  renderWithProviders(<LiveSessions />)

  expect(within(screen.getByRole('table')).getByText(shown)).toBeInTheDocument()
})

test('a short silence is counted in seconds, a long one in minutes', () => {
  // ⚠️ `stale` is what decides; this is only how the number reads. Rendering 240s as "240s" is not
  // wrong, it is unreadable — and the number on this badge is the one somebody squints at to
  // decide whether to go and look at the machine.
  arrange({ rows: [listed({ stale: true, silent_for_seconds: 65 })] })

  renderWithProviders(<LiveSessions />)

  expect(within(screen.getByRole('table')).getByText(/silent 65s/i)).toBeInTheDocument()
})

test('a session with no bar yet shows a dash rather than an invented time', () => {
  arrange({ rows: [listed({ last_bar_time: null })] })

  renderWithProviders(<LiveSessions />)

  expect(within(screen.getByRole('table')).getByText('—')).toBeInTheDocument()
})

test('a stop that could not be recorded says the session is still running', () => {
  // ⚠️ The request was not written, so nothing will act on it. A screen that showed "stopping…"
  // would have an operator waiting on a message nobody sent.
  arrange()
  stop.mockReturnValue({ mutate: vi.fn(), isPending: false, isError: true } as unknown as ReturnType<
    typeof useStopLiveSession
  >)

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/request was not recorded/i)).toBeInTheDocument()
})

test('a detail that could not be read says the session itself is unaffected', () => {
  // The detail 503s rather than guessing at the stop state. The screen repeats that honestly:
  // what is missing is the panel's knowledge, not the session.
  arrange()
  session.mockReturnValue({ data: undefined, isError: true } as unknown as ReturnType<
    typeof useLiveSession
  >)

  renderWithProviders(<LiveSessions />)

  expect(screen.getByText(/session itself is unaffected/i)).toBeInTheDocument()
})

test('with nothing running, the newest session is opened anyway', () => {
  // ⚠️ A panel that opened on nothing when every session has ended would make the commonest
  // after-hours question — "what did it do today?" — take a click nobody knows to make.
  arrange({ rows: [listed({ id: 'sess-old', status: 'stopped' })] })

  renderWithProviders(<LiveSessions />)

  expect(screen.getByRole('button', { name: /stop session/i })).toBeDisabled()
})

test('clicking a row opens that session instead of the default one', () => {
  // ⚠️ Never tested until the coverage gate pointed at the click handler, and it is a feature
  // rather than a line: with more than one session running, the screen opens the first *running*
  // one, and picking another is the only way to look at the rest. A panel that always showed the
  // same session would be unusable on precisely the day it matters — the day two are live.
  const other = listed({ id: 'sess-2', symbol: 'GBPUSD' })
  arrange({ rows: [listed(), other] })

  renderWithProviders(<LiveSessions />)
  fireEvent.click(screen.getByText('GBPUSD'))

  // `useLiveSession` is asked about the row that was clicked, not the one that was open.
  expect(session).toHaveBeenLastCalledWith('sess-2', true)
})
