import { useState } from 'react'

import {
  useEngageKillSwitch,
  useKillSwitch,
  useLiveSession,
  useLiveSessions,
  useSessionEvents,
  useStopLiveSession,
} from '../api/hooks'
import type { LiveSession, LiveSessionDetail, SessionEvent } from '../api/types'
import { useLiveSessionFeed } from '../api/useLiveSessionFeed'
import type { FeedEvent } from '../api/useLiveSessionFeed'
import { count, signedMoney } from '../format'

function clock(value: string | null): string {
  return value === null ? '—' : new Date(value).toLocaleTimeString()
}

function silence(seconds: number): string {
  if (seconds < 90) return `${count(seconds)}s`
  return `${count(Math.round(seconds / 60))}m`
}

/**
 * What a session is doing, in one badge.
 *
 * ⚠️ **`stale` outranks `status`, and that ordering is the whole point of this component.** A
 * process that died leaves its row saying `running` for ever, because the thing that would update
 * the status is the thing that died. Reading `status` first would paint the deadest sessions
 * green.
 */
function Status(props: { session: LiveSession }): React.JSX.Element {
  const { status, stale, silent_for_seconds: silent } = props.session
  if (status === 'running' && stale) {
    return (
      <span className="rounded bg-amber-500/20 px-2 py-0.5 text-xs text-amber-300">
        silent {silence(silent)}
      </span>
    )
  }
  const tone =
    status === 'running'
      ? 'bg-emerald-500/20 text-emerald-300'
      : status === 'failed'
        ? 'bg-rose-500/20 text-rose-300'
        : 'bg-slate-700 text-slate-300'
  return <span className={`rounded px-2 py-0.5 text-xs ${tone}`}>{status}</span>
}

/**
 * The kill switch, and the sentence that has to sit next to it.
 *
 * ⚠️ **The spec calls this "encerra tudo" and that is not what it does.** `safety.admits` clears
 * exits, cancels and tightening stops *before* it consults the switches, deliberately — a switch
 * that refused an exit would be an operator pulling the handle and staying in the trade. So the
 * label says what happens, and the note says what does not. A button that promises more than the
 * mechanism delivers is worse than no button, because it is believed at the one moment nobody
 * has time to check.
 */
function KillSwitchPanel(): React.JSX.Element {
  const state = useKillSwitch()
  const engage = useEngageKillSwitch()
  const engaged = state.data?.engaged ?? false

  return (
    <section
      className={`rounded-lg border p-4 ${
        engaged ? 'border-rose-600 bg-rose-950/40' : 'border-slate-800 bg-slate-900'
      }`}
    >
      <div className="flex items-start justify-between gap-4">
        <div>
          <h2 className="text-sm font-semibold">Kill switch</h2>
          <p className="mt-1 text-xs text-slate-400">
            Stops the executor <strong>taking on new risk</strong>, for every session at once.{' '}
            <strong className="text-slate-200">It does not close open positions</strong> — they keep
            the stop already sitting at the venue, and exits, cancels and tightening stops still go
            through.
          </p>
        </div>
        <button
          type="button"
          disabled={engaged || engage.isPending}
          onClick={() => {
            engage.mutate()
          }}
          className="shrink-0 rounded bg-rose-600 px-3 py-2 text-sm font-semibold text-white disabled:bg-slate-700 disabled:text-slate-400"
        >
          {engaged ? 'Engaged' : 'Engage kill switch'}
        </button>
      </div>

      {state.isError && (
        <p className="mt-3 text-xs text-amber-300">
          Could not read the kill switch. The executor treats an unreadable Redis as engaged — but
          do not rely on that from here.
        </p>
      )}

      {engaged && (
        <p className="mt-3 text-xs text-rose-200">
          Engaged {state.data?.engaged_at === null ? '(nobody recorded when)' : clock(state.data?.engaged_at ?? null)}
          {' · '}
          {state.data?.layer}. Release it from a shell: <code>redis-cli DEL executor:kill-switch</code>.
          There is no button for that, on purpose.
        </p>
      )}

      {/* ⚠️ Always shown, engaged or not. `engaged: false` here means "the layer this API can
          write is not engaged" and nothing more: the file on the executor's disk and the flag in
          its memory are invisible from this process, and either one stops everything while this
          panel reads clear. */}
      <p className="mt-2 text-[11px] text-slate-500">
        This is one of three layers. The other two live on the executor&apos;s own machine and
        cannot be seen from here.
      </p>
    </section>
  )
}

function SessionRow(props: {
  session: LiveSession
  selected: boolean
  onSelect: () => void
}): React.JSX.Element {
  const { session } = props
  return (
    <tr
      onClick={props.onSelect}
      className={`cursor-pointer border-t border-slate-800 ${
        props.selected ? 'bg-slate-800' : 'hover:bg-slate-900'
      }`}
    >
      <td className="px-3 py-2 text-sm">{session.symbol}</td>
      <td className="px-3 py-2 text-sm text-slate-400">{session.timeframe}</td>
      <td className="px-3 py-2 text-sm text-slate-400">{session.mode}</td>
      <td className="px-3 py-2">
        <Status session={session} />
      </td>
      <td className="px-3 py-2 text-sm text-slate-400">{clock(session.last_bar_time)}</td>
    </tr>
  )
}

/**
 * The stop button and the sentence that has to sit next to it.
 *
 * ⚠️ **`status` stays `running` after a successful stop, and that is not a lag to paper over.**
 * Only the session writes `stopped_at`, when it has actually finished; a screen that flipped the
 * status itself would be reporting an outcome it cannot observe — and on a session whose process
 * is already dead it would show `stopped` over a position sitting unmanaged at the venue.
 */
function StopButton(props: { session: LiveSessionDetail }): React.JSX.Element {
  const stop = useStopLiveSession()
  const { session } = props
  const asked = session.stop_requested_at !== null
  const running = session.status === 'running'

  return (
    <div className="rounded border border-slate-800 bg-slate-900 p-3">
      <div className="flex items-start justify-between gap-4">
        <p className="text-xs text-slate-400">
          Finishes the bar it is on and ends the session.{' '}
          <strong className="text-slate-200">The open position stays open</strong> — after this,
          nothing manages it: no trailing stop, no exit condition, only the level already at the
          venue.
        </p>
        <button
          type="button"
          disabled={!running || asked || stop.isPending}
          onClick={() => {
            stop.mutate(session.id)
          }}
          className="shrink-0 rounded bg-amber-600 px-3 py-2 text-sm font-semibold text-white disabled:bg-slate-700 disabled:text-slate-400"
        >
          {asked ? 'Stopping…' : 'Stop session'}
        </button>
      </div>
      {asked && running && (
        <p className="mt-2 text-xs text-amber-200">
          Asked at {clock(session.stop_requested_at)}. It stops when it next comes up for air,
          which on a quiet market is up to a few seconds — the row still says <code>running</code>{' '}
          until the session itself writes that it finished.
        </p>
      )}
      {stop.isError && (
        <p className="mt-2 text-xs text-rose-300">
          The request was not recorded, so nothing will act on it. The session is still running.
        </p>
      )}
      {/* ⚠️ Not a footnote. Every arming attempt refused while a session is stopped or killed
          counts towards `MAX_ARMING_ATTEMPTS`, and three in a row retire that zone for the rest of
          the session — releasing the switch does not bring it back. */}
      <p className="mt-2 text-[11px] text-slate-500">
        After a stop or a kill, restart the session rather than resuming it: zones turned away
        three times running are retired for good.
      </p>
    </div>
  )
}

function Positions(props: { session: LiveSessionDetail }): React.JSX.Element {
  const { open_positions: positions } = props.session
  if (positions.length === 0) {
    return <p className="text-sm text-slate-500">No open position.</p>
  }
  return (
    <ul className="space-y-1">
      {positions.map((position) => (
        <li key={position.id} className="text-sm">
          <span className="font-semibold">{position.direction}</span> {position.volume} @{' '}
          {position.entry_price}
          {position.stop_loss !== null && (
            <span className="text-slate-400"> · stop {position.stop_loss}</span>
          )}
        </li>
      ))}
    </ul>
  )
}

function Ticker(props: { events: FeedEvent[]; connected: boolean }): React.JSX.Element {
  return (
    <div>
      <h3 className="flex items-center gap-2 text-sm font-semibold">
        Live
        <span className={`text-xs ${props.connected ? 'text-emerald-400' : 'text-amber-400'}`}>
          {/* ⚠️ Shown rather than hidden. When the socket is down the screen falls back to
              polling, so it keeps working — but slower, and an operator deciding how much to
              trust what they see is entitled to know which of the two they are looking at. */}
          {props.connected ? '● live' : '○ polling'}
        </span>
      </h3>
      {props.events.length === 0 ? (
        <p className="mt-1 text-sm text-slate-500">Nothing since this screen opened.</p>
      ) : (
        <ul className="mt-1 space-y-1">
          {props.events.map((event) => (
            <li key={`${event.type}-${event.client_id}-${event.at}`} className="text-sm">
              <span className="text-slate-500">{clock(event.at)}</span>{' '}
              {event.type === 'fill' ? (
                <>
                  <span className="text-emerald-400">filled</span> {event.client_id} @ {event.price}
                </>
              ) : (
                <>
                  <span className="text-rose-400">refused</span> {event.client_id} —{' '}
                  {event.reason}
                  {/* ⚠️ Whose refusal it was, because the two behave oppositely: ours describe
                      conditions that change on their own, the venue's usually do not. */}
                  <span className="text-slate-500"> ({event.by_venue ? 'venue' : 'safeguards'})</span>
                </>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function EventLog(props: { events: SessionEvent[] }): React.JSX.Element {
  if (props.events.length === 0) {
    return <p className="text-sm text-slate-500">No orders yet.</p>
  }
  return (
    <ul className="space-y-1">
      {props.events.map((event) => (
        <li key={event.id} className="text-sm">
          <span className="text-slate-500">{clock(event.requested_at)}</span>{' '}
          <span className="text-slate-300">{event.status}</span> {event.client_id}
          {/* NULL and a string are different statements here: no refusal happened, versus the
              rule that refused. */}
          {event.reason !== null && <span className="text-rose-300"> — {event.reason}</span>}
        </li>
      ))}
    </ul>
  )
}

/**
 * Every live session, and one of them in full.
 *
 * The screen the PR-304 acceptance criterion asks for, and the first thing in this app to open a
 * WebSocket: a fill reaches this page the moment the executor publishes it, rather than at the
 * next poll.
 *
 * ⚠️ **Two things on this page deliberately promise less than their names suggest**, and both are
 * spelled out beside the button rather than in a tooltip: the kill switch does not close
 * positions, and stopping a session does not close its position either. `safety.admits` clears
 * every risk-reducing instruction *before* it looks at the switches, on purpose. A screen that
 * said "encerra tudo" — which is the wording in `specs/fase-3.md` — would be teaching an operator
 * something false at the one moment they cannot afford to check.
 */
export function LiveSessions(): React.JSX.Element {
  const [selected, setSelected] = useState<string | null>(null)
  const sessions = useLiveSessions()
  const rows = sessions.data?.sessions ?? []
  // The first running session, until somebody picks one. A panel that opened on nothing would
  // make the common case — one session, running — take a click to see.
  const current = selected ?? rows.find((row) => row.status === 'running')?.id ?? rows[0]?.id
  const feed = useLiveSessionFeed(current)
  const detail = useLiveSession(current, feed.connected)
  const events = useSessionEvents(current, feed.connected)

  return (
    <div className="space-y-6">
      <KillSwitchPanel />

      <section>
        <h2 className="mb-2 text-sm font-semibold">Sessions</h2>
        {rows.length === 0 ? (
          <p className="text-sm text-slate-500">
            {sessions.isPending ? 'Loading…' : 'No session has ever been started.'}
          </p>
        ) : (
          <table className="w-full">
            <thead>
              <tr className="text-left text-xs text-slate-500">
                <th className="px-3 py-1 font-normal">Symbol</th>
                <th className="px-3 py-1 font-normal">TF</th>
                <th className="px-3 py-1 font-normal">Mode</th>
                <th className="px-3 py-1 font-normal">State</th>
                <th className="px-3 py-1 font-normal">Last bar</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <SessionRow
                  key={row.id}
                  session={row}
                  selected={row.id === current}
                  onSelect={() => {
                    setSelected(row.id)
                  }}
                />
              ))}
            </tbody>
          </table>
        )}
      </section>

      {detail.data !== undefined && (
        <section className="space-y-4">
          <header className="flex items-baseline gap-3">
            <h2 className="text-sm font-semibold">
              {detail.data.symbol} {detail.data.timeframe}
            </h2>
            <Status session={detail.data} />
            <span className="text-xs text-slate-500">
              {/* Zero realised is a real statement; the count beside it is what separates "nothing
                  was closed" from "two trades that cancelled out". */}
              today {signedMoney(detail.data.realised_today)} over{' '}
              {count(detail.data.trades_closed_today)} closed
            </span>
          </header>

          <StopButton session={detail.data} />

          <div className="grid gap-6 md:grid-cols-2">
            <div>
              <h3 className="text-sm font-semibold">Open position</h3>
              <div className="mt-1">
                <Positions session={detail.data} />
              </div>
            </div>
            <Ticker events={feed.events} connected={feed.connected} />
          </div>

          <div>
            <h3 className="text-sm font-semibold">Orders</h3>
            <div className="mt-1">
              <EventLog events={events.data?.events ?? []} />
            </div>
          </div>
        </section>
      )}

      {detail.isError && (
        <p className="text-sm text-amber-300">
          Could not read this session&apos;s stop state, so it is not being shown. The session
          itself is unaffected.
        </p>
      )}
    </div>
  )
}
