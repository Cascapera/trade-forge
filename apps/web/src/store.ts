// UI state that outlives a single screen: which strategy the user just built and saved, so the
// launch screen knows what to run. Server state (the strategy row itself, backtests, results)
// lives in React Query, not here — this holds only the thin thread of the current session.

import { create } from 'zustand'

interface SessionState {
  strategyId: string | null
  strategyName: string | null
  setStrategy: (id: string, name: string) => void
  /**
   * The basket launched most recently, so the nav can offer a way back to it.
   *
   * Here rather than in React Query because it is not server state: it is *which* basket this
   * person is currently looking at, which the server has no opinion about. There is no
   * `GET /baskets` listing, so without this thread a basket would be reachable only by pasting
   * its URL back — the same thread `strategyId` already provides for the launch screen.
   */
  basketId: string | null
  basketLabel: string | null
  setBasket: (id: string, label: string) => void
  clear: () => void
}

export const useSession = create<SessionState>((set) => ({
  strategyId: null,
  strategyName: null,
  basketId: null,
  basketLabel: null,
  setStrategy: (id, name) => {
    set({ strategyId: id, strategyName: name })
  },
  setBasket: (id, label) => {
    set({ basketId: id, basketLabel: label })
  },
  clear: () => {
    set({ strategyId: null, strategyName: null, basketId: null, basketLabel: null })
  },
}))
