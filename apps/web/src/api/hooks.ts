// React Query hooks — the app's entire server-state surface. Reads are queries; the two writes
// (create a strategy, enqueue a backtest) are mutations. The interesting one is `useBacktest`:
// a backtest is asynchronous, so the query *polls* until the run reaches a terminal status, then
// stops on its own. That is the whole point of the 202-plus-poll contract the API exposes.

import { skipToken, useMutation, useQuery } from '@tanstack/react-query'
import type { Strategy } from '@tradeforge/schema'

import { api } from './client'
import type {
  Backtest,
  BacktestStatus,
  CreateBacktestRequest,
  CreatedBacktest,
  EquityPoint,
  Instrument,
  StrategyOut,
  TradesPage,
} from './types'

const POLL_MS = 1000
const TERMINAL: readonly BacktestStatus[] = ['done', 'failed']

export function isTerminal(status: BacktestStatus | undefined): boolean {
  return status !== undefined && TERMINAL.includes(status)
}

export function useInstruments() {
  return useQuery<Instrument[]>({ queryKey: ['instruments'], queryFn: api.listInstruments })
}

export function useCreateStrategy() {
  return useMutation<StrategyOut, Error, Strategy>({
    mutationFn: (definition) => api.createStrategy(definition),
  })
}

/**
 * Save a document under a lineage: a new strategy, or the next version of one already saved.
 *
 * `POST` always writes version 1, and (name, version) is unique, so saving the same name twice can
 * only ever conflict. Iterating — nudge the period, run it again — is therefore not a `POST` at
 * all; it is `PUT` on the previous version, which the API inserts as the next one, linked to its
 * parent. Choosing between them by *name* is what lets the screen keep one button.
 */
export function useSaveStrategy() {
  return useMutation<StrategyOut, Error, { definition: Strategy; parentId: string | null }>({
    mutationFn: ({ definition, parentId }) =>
      parentId === null
        ? api.createStrategy(definition)
        : api.updateStrategy(parentId, definition),
  })
}

export function useCreateBacktest() {
  return useMutation<CreatedBacktest, Error, CreateBacktestRequest>({
    mutationFn: (payload) => api.createBacktest(payload),
  })
}

export function useBacktest(id: string | undefined) {
  // `skipToken` disables the query when there is no id *and* narrows `id` to a string inside the
  // function — the v5 idiom that needs neither a cast nor a non-null assertion.
  return useQuery<Backtest>({
    queryKey: ['backtest', id],
    queryFn: id === undefined ? skipToken : () => api.getBacktest(id),
    // Keep polling while the run is queued or running; stop the moment it is done or failed.
    refetchInterval: (query) => (isTerminal(query.state.data?.status) ? false : POLL_MS),
  })
}

export function useTrades(id: string | undefined, enabled: boolean) {
  return useQuery<TradesPage>({
    queryKey: ['trades', id],
    queryFn: id !== undefined && enabled ? () => api.getTrades(id) : skipToken,
  })
}

export function useEquity(id: string | undefined, enabled: boolean) {
  return useQuery<EquityPoint[]>({
    queryKey: ['equity', id],
    queryFn: id !== undefined && enabled ? () => api.getEquity(id) : skipToken,
  })
}
