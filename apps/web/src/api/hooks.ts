// React Query hooks — the app's entire server-state surface. Reads are queries; the two writes
// (create a strategy, enqueue a backtest) are mutations. The interesting one is `useBacktest`:
// a backtest is asynchronous, so the query *polls* until the run reaches a terminal status, then
// stops on its own. That is the whole point of the 202-plus-poll contract the API exposes.

import { skipToken, useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import type { Strategy } from '@tradeforge/schema'

import { api } from './client'
import type {
  Backtest,
  BacktestFilters,
  BacktestStatus,
  BacktestsPage,
  BasketOut,
  CandlesResponse,
  Collection,
  CreateBacktestRequest,
  CreateBasketRequest,
  CreateCollection,
  CreateStudyRequest,
  CreateWalkForwardRequest,
  CreatedBacktest,
  CreatedBasket,
  CreatedStudy,
  CreatedWalkForward,
  EquityPoint,
  Instrument,
  OverlaysResponse,
  Snapshot,
  StrategiesPage,
  StrategyFilters,
  StrategyOut,
  SymbolHistory,
  SymbolSearch,
  StudyOut,
  TradesPage,
  WalkForwardOut,
} from './types'

const POLL_MS = 1000

// A grid is up to five hundred runs on the same worker pool, so it settles on a scale of minutes
// rather than seconds — and every response carries every run in it. Polling a study at the
// single-run cadence would add hundreds of requests to the queue being waited on.
const STUDY_POLL_MS = 3000
const TERMINAL: readonly BacktestStatus[] = ['done', 'failed']

export function isTerminal(status: BacktestStatus | undefined): boolean {
  return status !== undefined && TERMINAL.includes(status)
}

export function useInstruments() {
  return useQuery<Instrument[]>({ queryKey: ['instruments'], queryFn: api.listInstruments })
}

/**
 * The broker's catalogue, searched by prefix.
 *
 * `placeholderData` keeps the previous page on screen while the next one loads. Without it the
 * list empties between keystrokes, and an empty list is the one thing this screen must not show
 * casually — it is also how "nothing matches" and "no catalogue" look.
 *
 * The debounce is the caller's, not this hook's: React Query keys on `q`, so a hook that
 * debounced internally would still be re-keyed on every character and would only be delaying
 * the request it had already decided to make.
 */
export function useSymbolSearch(q: string) {
  return useQuery<SymbolSearch>({
    queryKey: ['symbols', q],
    queryFn: () => api.searchSymbols(q),
    placeholderData: (previous) => previous,
  })
}

/**
 * Ask the host agent to re-photograph the broker's catalogue.
 *
 * ⚠️ Invalidates every symbol search on success, and success here only means *queued*. The
 * agent may be down, in which case the refetched list is the old one — which is the right
 * outcome: the screen keeps working with a stale catalogue rather than emptying itself because
 * a terminal on somebody's desk is closed.
 */
/**
 * What is known about one series. `enabled` only once both halves of the question exist.
 *
 * ⚠️ A 404 here is an answer, not a failure: nobody has probed this series yet. The component
 * reads `error.status` rather than treating every error the same, because "press measure" and
 * "something is broken" are different sentences and only one of them is actionable.
 */
export function useSymbolHistory(symbol: string, timeframe: string | undefined) {
  return useQuery<SymbolHistory>({
    queryKey: ['symbol-history', symbol, timeframe],
    queryFn:
      symbol === '' || timeframe === undefined
        ? skipToken
        : () => api.getSymbolHistory(symbol, timeframe),
    retry: false,
  })
}

/**
 * Ask the host agent to measure a series.
 *
 * ⚠️ Success means *queued*, not measured — a cold H4 took 207 seconds on this broker — so the
 * invalidation below is what eventually shows the answer, once the agent has written it.
 */
export function useProbeSymbol() {
  const client = useQueryClient()
  return useMutation<{ job: string }, Error, { symbol: string; timeframe: string }>({
    mutationFn: ({ symbol, timeframe }) => api.probeSymbol(symbol, timeframe),
    onSuccess: () => client.invalidateQueries({ queryKey: ['symbol-history'] }),
  })
}

/**
 * Launch a collection and get the row to watch it with.
 *
 * ⚠️ The invalidations are three, and each one is a different screen catching up. `collections`
 * is the list under the form; `instruments` is what the backtest screens offer, and a first
 * collection is exactly what puts a symbol there; `symbols` carries the `catalogued` flag that
 * decides whether the combobox marks it "no data".
 */
export function useCreateCollection() {
  const client = useQueryClient()
  return useMutation<Collection[], Error, CreateCollection>({
    mutationFn: (body) => api.createCollection(body),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['collections'] })
      void client.invalidateQueries({ queryKey: ['instruments'] })
      void client.invalidateQueries({ queryKey: ['symbols'] })
    },
  })
}

/**
 * One collection, polled while it runs.
 *
 * ⚠️ Polled at the **study** cadence rather than the single-run one. A collection advances once
 * per calendar year of history and a cold year can take minutes on this broker, so a
 * once-a-second poll would be hundreds of requests against a row that changes five times.
 */
export function useCollection(id: string | undefined) {
  return useQuery<Collection>({
    queryKey: ['collection', id],
    queryFn: id === undefined ? skipToken : () => api.getCollection(id),
    refetchInterval: (query) => (isTerminal(query.state.data?.status) ? false : STUDY_POLL_MS),
  })
}

/** Every collection, newest first, refreshed while any of them is still going. */
export function useCollections() {
  return useQuery<Collection[]>({
    queryKey: ['collections'],
    queryFn: api.listCollections,
    refetchInterval: (query) =>
      (query.state.data ?? []).every((row) => isTerminal(row.status)) ? false : STUDY_POLL_MS,
  })
}

export function useSyncSymbols() {
  const client = useQueryClient()
  return useMutation<{ job: string }>({
    mutationFn: () => api.syncSymbols(),
    onSuccess: () => client.invalidateQueries({ queryKey: ['symbols'] }),
  })
}

/**
 * One saved strategy, by id — the endpoint that had no caller until the builder learned to open
 * one. Immutable for its version, so it never needs refetching once it has arrived.
 */
export function useStrategy(id: string | undefined) {
  return useQuery<StrategyOut>({
    queryKey: ['strategy', id],
    queryFn: id === undefined ? skipToken : () => api.getStrategy(id),
    staleTime: Infinity,
  })
}

export function useCreateStrategy() {
  return useMutation<StrategyOut, Error, Strategy>({
    mutationFn: (definition) => api.createStrategy(definition),
  })
}

/**
 * Every strategy lineage, for a picker to choose from.
 *
 * One row per lineage rather than per version, and a grid's own points are left out by the
 * server — a hundred-point study writes a hundred strategies nobody picks by hand.
 */
export function useStrategies(filters: StrategyFilters = {}) {
  return useQuery<StrategiesPage>({
    queryKey: ['strategies', filters],
    queryFn: () => api.listStrategies(filters),
  })
}

/**
 * Save a document under a lineage: a new strategy, or the next version of one already saved.
 *
 * `POST` always writes version 1, and (name, version) is unique, so saving the same name twice can
 * only ever conflict. Iterating — nudge the period, run it again — is therefore not a `POST` at
 * all; it is `PUT` on the previous version, which the API inserts as the next one, linked to its
 * parent. Choosing between them by *name* is what lets the screen keep one button.
 *
 * ⚠️ **The name is looked up on the server, and that is the fix for the 409.** This used to
 * decide from the id the screen had created in *this browser session*, so a strategy saved from
 * any other tab, or before a reload, was invisible to it — and saving under that name was a
 * `POST` that collided. What a name means is a fact about the database, never about what a tab
 * remembers.
 *
 * `include_generated` on the lookup, because a name is taken in the database whether or not a
 * picker chooses to show it. Hidden is not free, and conflating the two would put the 409 back
 * for exactly the names a study had generated.
 */
export function useSaveStrategy() {
  return useMutation<StrategyOut, Error, { definition: Strategy }>({
    mutationFn: async ({ definition }) => {
      const taken = await api.listStrategies({
        name: definition.name,
        include_generated: true,
      })
      const existing = taken.items[0]
      return existing === undefined
        ? api.createStrategy(definition)
        : api.updateStrategy(existing.id, definition)
    },
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

export function useCreateStudy() {
  return useMutation<CreatedStudy, Error, CreateStudyRequest>({
    mutationFn: (payload) => api.createStudy(payload),
  })
}

/**
 * Has every point of this grid landed? — the study's version of `isTerminal`.
 *
 * A study has no status of its own, only N runs finishing at their own pace, so the poll stops
 * when the **last** one does. Separate from a basket's `isSettled` because the two read different
 * bodies: one function over a union would be a seam where a future field on one shape silently
 * applies to the other.
 */
export function isStudySettled(study: StudyOut | undefined): boolean {
  return study?.runs.every((run) => isTerminal(run.status)) ?? false
}

/**
 * One study, polled until every point has landed.
 *
 * ⚠️ **The interval is longer than a basket's, and the reason is arithmetic.** A basket is a
 * handful of runs; a grid is up to five hundred, drained by the same worker pool one after
 * another. Polling a five-minute study at a basket's cadence would add hundreds of requests to
 * the very queue being waited on, and each response carries every run in the study.
 */
export function useCreateWalkForward() {
  return useMutation<CreatedWalkForward, Error, CreateWalkForwardRequest>({
    mutationFn: (payload) => api.createWalkForward(payload),
  })
}

/**
 * One walk-forward, polled until the orchestrating job stops.
 *
 * ⚠️ **This one has a status of its own, unlike a study or a basket**, and the poll reads it
 * rather than inspecting the runs. That is not a shortcut — it is the only correct source. A
 * walk-forward's runs do not all exist yet while it is working: a fold's out-of-sample run is
 * created *after* its training grid has finished and a winner has been picked, so "every run I
 * can see has landed" is true several times over the course of one experiment, and each time it
 * would stop the poll on a screen that is still filling in.
 *
 * The interval matches a study's, and for the same arithmetic: the folds run one grid after
 * another inside a single worker slot, so this settles on a scale of minutes.
 */
export function useWalkForward(id: string | undefined) {
  return useQuery<WalkForwardOut>({
    queryKey: ['walkforward', id],
    queryFn: id === undefined ? skipToken : () => api.getWalkForward(id),
    refetchInterval: (query) => (isTerminal(query.state.data?.status) ? false : STUDY_POLL_MS),
  })
}

export function useStudy(id: string | undefined) {
  return useQuery<StudyOut>({
    queryKey: ['study', id],
    // `skipToken` rather than `enabled` plus a cast: it narrows the type for real, which is the
    // idiom the rest of this file already uses.
    queryFn: id === undefined ? skipToken : () => api.getStudy(id),
    refetchInterval: (query) => (isStudySettled(query.state.data) ? false : STUDY_POLL_MS),
  })
}

/**
 * What has already been measured about each chosen symbol, fetched in parallel.
 *
 * `useQueries` for the same reason `useEquityCurves` uses it: the number of chosen symbols
 * changes as the user picks, and hooks cannot be called conditionally. Each answer gets its own
 * cache entry keyed by (symbol, timeframe), so re-picking a symbol is free.
 *
 * ⚠️ **Read-only, and a missing entry is not an error.** A symbol nobody has probed answers 404
 * and lands here as absent — which is a fact the screen has to show ("nobody has measured this
 * one") rather than a failure. `retry: false` keeps a 404 from being asked four times.
 */
export function useSymbolHistories(symbols: readonly string[], timeframe: string) {
  return useQueries({
    queries: symbols.map((symbol) => ({
      queryKey: ['symbol-history', symbol, timeframe],
      queryFn: () => api.getSymbolHistory(symbol, timeframe),
      retry: false,
    })),
    combine: (results) => ({
      // A Map rather than an array: the caller asks "what do we know about EURUSD", never
      // "what is the third answer", and an index-keyed answer would silently shift when a
      // symbol is removed from the middle of the list.
      known: new Map(
        results.flatMap((result, index) => {
          const symbol = symbols[index]
          return result.data !== undefined && symbol !== undefined
            ? ([[symbol, result.data]] as [string, SymbolHistory][])
            : []
        }),
      ),
      /** Symbols whose measurement has come back one way or the other. */
      settled: results.filter((result) => !result.isPending).length,
    }),
  })
}
