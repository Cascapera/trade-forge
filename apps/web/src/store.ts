// UI state that outlives a single screen: which strategy the user just built and saved, so the
// launch screen knows what to run. Server state (the strategy row itself, backtests, results)
// lives in React Query, not here — this holds only the thin thread of the current session.

import { create } from 'zustand'

interface SessionState {
  strategyId: string | null
  strategyName: string | null
  setStrategy: (id: string, name: string) => void
  clear: () => void
}

export const useSession = create<SessionState>((set) => ({
  strategyId: null,
  strategyName: null,
  setStrategy: (id, name) => {
    set({ strategyId: id, strategyName: name })
  },
  clear: () => {
    set({ strategyId: null, strategyName: null })
  },
}))
