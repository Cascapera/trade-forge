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
  BasketOut,
  CandlesResponse,
  CreateBacktestRequest,
  CreateBasketRequest,
  CreatedBacktest,
  CreatedBasket,
  EquityPoint,
  Instrument,
  OverlaysResponse,
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
 * The price the run was executed over, fetched only when someone opens the chart.
 *
 * `enabled` carries two conditions from the caller: the run has finished, and the price tab is
 * the one on screen. Both matter — this is the largest payload the results page can ask for
 * (thousands of bars, against a handful of metrics), and fetching it for a reader who never
 * leaves the metrics tab would be the page's whole cost, spent on nothing.
 *
 * `staleTime: Infinity` because the answer genuinely cannot change while the page is open: the
 * window is bounded by the provenance a *finished* run recorded, and a finished run never eats
 * another candle. Re-fetching on every window focus would re-download thousands of bars to
 * receive the same ones back.
 */
/**
 * The curves the strategy was reading, on the same terms as the candles.
 *
 * A query of its own rather than a field on the candles response: a strategy may have no curves
 * at all (the structure setups draw zones, not lines), and folding an optional thing into a
 * required one would make every caller check the same emptiness twice.
 */
export function useOverlays(id: string | undefined, enabled: boolean) {
  return useQuery<OverlaysResponse>({
    queryKey: ['overlays', id],
    queryFn: id !== undefined && enabled ? () => api.getOverlays(id) : skipToken,
    staleTime: Infinity,
  })
}

export function useCandles(id: string | undefined, enabled: boolean) {
  return useQuery<CandlesResponse>({
    queryKey: ['candles', id],
    queryFn: id !== undefined && enabled ? () => api.getCandles(id) : skipToken,
    staleTime: Infinity,
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
export function useCreateBasket() {
  return useMutation<CreatedBasket, Error, CreateBasketRequest>({
    mutationFn: (payload) => api.createBasket(payload),
  })
}

/**
 * Is every run in this basket over? — the basket's own version of `isTerminal`.
 *
 * A basket is not "done" the way a run is; it has no status of its own, only N runs that finish
 * at their own pace. So the poll stops when the **last** one lands, not the first.
 *
 * ⚠️ A basket with no runs counts as settled. It cannot be created — the API refuses fewer than
 * two symbols — but reading it as unsettled would leave `every` returning `true` for an empty
 * list to fight a poll that never stops, and a query that polls forever over a shape that
 * cannot happen is worse than one that quietly agrees there is nothing to wait for.
 */
export function isSettled(basket: BasketOut | undefined): boolean {
  return basket?.runs.every((run) => isTerminal(run.status)) ?? false
}

/**
 * One basket, polled until every run in it has landed.
 *
 * Same 202-plus-poll contract the single run uses, one level up. The interval is deliberately
 * the same: a basket is N runs on the same worker pool, so it settles on the timescale of the
 * slowest of them, and asking more often would only add requests to the thing being waited on.
 */
export function useBasket(id: string | undefined) {
  return useQuery<BasketOut>({
    queryKey: ['basket', id],
    queryFn: id === undefined ? skipToken : () => api.getBasket(id),
    refetchInterval: (query) => (isSettled(query.state.data) ? false : POLL_MS),
  })
}

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
