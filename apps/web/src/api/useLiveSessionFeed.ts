// The live-session feed: the first WebSocket this app has ever opened.
//
// Everything else here polls with React Query, which is right for a backtest — a percentage that
// settles in seconds. A live session is different in kind: it is quiet for an hour and then a fill
// happens, and the person watching is watching *because* they want to know the instant it does.

import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'

import { socketUrl } from './client'
import type { SessionFrame } from './types'

/** How long to wait before reopening a socket that dropped. */
const RECONNECT_MS = 3000

/**
 * How many events the ticker keeps.
 *
 * ⚠️ A bound, not a page size. This is a browser tab somebody leaves open for a trading day, and
 * an unbounded array of every fill and refusal is a leak with a nice name. The full history is
 * `GET /live-sessions/{id}/events`, which is paginated precisely so this does not have to be.
 */
const KEEP = 50

export type FeedEvent = Extract<SessionFrame, { type: 'fill' } | { type: 'refusal' }>

export interface Feed {
  /**
   * Whether the socket is open **right now**.
   *
   * ⚠️ Not decoration: the screen turns its polling back on when this is `false`. A panel that
   * quietly stopped updating because a socket dropped would be the worst possible failure here —
   * it looks exactly like a market doing nothing.
   */
  connected: boolean
  /** The most recent events, newest first, capped at `KEEP`. */
  events: FeedEvent[]
}

function parse(raw: string): SessionFrame | null {
  try {
    // The server's own shapes; anything else is a bug on that side and must not crash this one.
    return JSON.parse(raw) as SessionFrame
  } catch {
    return null
  }
}

/**
 * Open a feed for one session and keep React Query honest about it.
 *
 * **The socket notifies; the queries are the truth.** A frame arriving invalidates the session's
 * detail and its event page rather than being merged into them by hand. Merging would put the
 * same facts in two places with two code paths to keep in step — and the one that drifts is
 * always the one nobody looks at, which here would be the number an operator acts on.
 *
 * ⚠️ **It does not reconnect after the session ends.** The server sends a final `state` and closes
 * when a session reaches a terminal status; reconnecting then would hand back the same state, be
 * closed again, and do it for ever, three seconds at a time, on a session that finished hours ago.
 */
export function useLiveSessionFeed(id: string | undefined): Feed {
  const client = useQueryClient()
  const [connected, setConnected] = useState(false)
  // ⚠️ The events are stored **with the session they belong to**, rather than cleared when the id
  // changes. Clearing would mean a `setState` in the effect body — a cascading render, and React's
  // own lint says so — and this way the answer is simply derived: events for another session are
  // not this session's events, so they are never read. One less piece of state to keep in step.
  const [held, setHeld] = useState<{ id: string | undefined; list: FeedEvent[] }>({
    id: undefined,
    list: [],
  })
  // Read by the close handler, which must not re-run the effect when it changes — a state
  // variable here would tear the socket down and rebuild it on every terminal frame.
  const ended = useRef(false)

  useEffect(() => {
    if (id === undefined) return

    ended.current = false
    let socket: WebSocket | null = null
    let timer: ReturnType<typeof setTimeout> | null = null
    let stopped = false

    const open = (): void => {
      socket = new WebSocket(socketUrl(`/ws/live-sessions/${id}`))

      socket.onopen = () => {
        setConnected(true)
      }

      socket.onmessage = (message: MessageEvent<string>) => {
        const frame = parse(message.data)
        if (frame === null) return

        if (frame.type === 'state') {
          // A session that has finished will not send anything else, and the server is about to
          // close: remember that, so the close below is not treated as a dropped connection.
          ended.current = frame.session.status !== 'running'
        } else if (frame.type === 'fill' || frame.type === 'refusal') {
          setHeld((previous) => ({
            id,
            list: [frame, ...(previous.id === id ? previous.list : [])].slice(0, KEEP),
          }))
        }

        // Every frame, including `error`: an unknown session is news the screen has to see.
        void client.invalidateQueries({ queryKey: ['live-session', id] })
        void client.invalidateQueries({ queryKey: ['session-events', id] })
        void client.invalidateQueries({ queryKey: ['live-sessions'] })
      }

      socket.onclose = () => {
        setConnected(false)
        if (stopped || ended.current) return
        timer = setTimeout(open, RECONNECT_MS)
      }
    }

    open()

    return () => {
      stopped = true
      if (timer !== null) clearTimeout(timer)
      // `onclose` is cleared first: unmounting is not a dropped connection, and leaving the
      // handler attached would schedule a reconnect for a screen nobody is looking at any more.
      if (socket !== null) {
        socket.onclose = null
        socket.close()
      }
      setConnected(false)
    }
  }, [id, client])

  return { connected, events: held.id === id ? held.list : [] }
}
