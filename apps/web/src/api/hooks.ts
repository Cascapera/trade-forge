// React Query hooks — the app's entire server-state surface. Reads are queries; the two writes
// (create a strategy, enqueue a backtest) are mutations. The interesting one is `useBacktest`:
// a backtest is asynchronous, so the query *polls* until the run reaches a terminal status, then
// stops on its own. That is the whole point of the 202-plus-poll contract the API exposes.

import { skipToken, useMutation, useQueries, useQuery } from '@tanstack/react-query'
import type { Strategy } from '@tradeforge/schema'

import { api } from './client'
import type {
  Backtest,
  BacktestFilters,
  BacktestStatus,
  BacktestsPage,
  CreateBacktestRequest,
  CreatedBacktest,
  EquityPoint,
  Instrument,
  Snapshot,
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

/**
 * The run log: every backtest, newest first, under the current filters.
 *
 * The filters are part of the query key, so switching from EURUSD to AAPL is a different cached
 * entry rather than a refetch that blanks the table. `placeholderData` keeps the previous page on
 * screen while the next one loads, which is what stops the layout jumping on every keystroke.
 */
export function useBacktests(filters: BacktestFilters) {
  return useQuery<BacktestsPage>({
    queryKey: ['backtests', filters],
    queryFn: () => api.listBacktests(filters),
    placeholderData: (previous) => previous,
  })
}

/**
 * The equity curves of the selected runs, fetched in parallel — one query per run, not one
 * request for all of them.
 *
 * `useQueries` rather than a loop of `useQuery` because the number of selected runs changes as
 * the user ticks boxes, and hooks cannot be called conditionally. It also gives each curve its
 * own cache entry keyed by run id, which is what makes unticking and re-ticking a run free.
 *
 * `staleTime: Infinity` because a finished run's curve is frozen: the backtest wrote it once and
 * nothing can change it. Re-polling it would be pure waste — the largest of these is 856 kB.
 */
export function useEquityCurves(ids: readonly string[]) {
  return useQueries({
    queries: ids.map((id) => ({
      queryKey: ['equity', id],
      queryFn: () => api.getEquity(id),
      staleTime: Infinity,
    })),
    combine: (results) => ({
      curves: new Map(
        results.flatMap((result, index) => {
          const id = ids[index]
          return result.data !== undefined && id !== undefined
            ? ([[id, result.data]] as [string, EquityPoint[]][])
            : []
        }),
      ),
      isPending: results.some((result) => result.isPending),
      isError: results.some((result) => result.isError),
    }),
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

/**
 * The entry picture for one trade, fetched only once someone opens it.
 *
 * `tradeId === null` means nothing is open, and `skipToken` keeps the query from running at all —
 * which is the whole point. Loading every snapshot with the trades list would assemble megabytes
 * a reader never looks at; the two or three entries that look wrong are the ones that get opened.
 *
 * Cached per trade, so re-opening one costs nothing and closing does not throw the bars away.
 */
export function useTradeSnapshot(backtestId: string | undefined, tradeId: number | null) {
  return useQuery<Snapshot>({
    queryKey: ['snapshot', backtestId, tradeId],
    queryFn:
      backtestId !== undefined && tradeId !== null
        ? () => api.getTradeSnapshot(backtestId, tradeId)
        : skipToken,
    // A recorded window never changes: it was frozen with the trade. Nothing to refetch.
    staleTime: Infinity,
  })
}
