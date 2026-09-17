// What a launch is missing, turned into what to say and what to send.
//
// The plan itself — which years, why whole years, why reaching the disk — is the server's
// (`POST /collections/plan`). This module only reads its answer: one sentence per market for
// the prompt, and the `POST /collections` bodies that fetch it. No React, so both launch screens
// say the same thing and the grouping is tested without rendering anything.

import type { AssetClass, CreateCollection, Instrument, PlannedCollection } from '../api/types'

/** The most symbols one `POST /collections` accepts (`MAX_COLLECTION_SYMBOLS`). */
const MAX_SYMBOLS_PER_REQUEST = 20

const ASSET_CLASSES: readonly string[] = ['forex', 'stock', 'index', 'future', 'crypto']

function year(iso: string): string {
  return iso.slice(0, 4)
}

function years(from: string, to: string): string {
  return year(from) === year(to) ? year(from) : `${year(from)}–${year(to)}`
}

/** `EURUSD H1 — on disk 2021-01-04 to 2025-12-31; would fetch 2019–2020, 2026`. */
export function missingLine(market: PlannedCollection): string {
  const held = market.covers === null ? 'never collected' : `on disk ${market.covers}`
  const fetched = market.windows.map((w) => years(w.date_from, w.date_to)).join(', ')
  return `${market.symbol} ${market.timeframe} — ${held}; would fetch ${fetched}`
}

/**
 * Would anything run right now, if the person declined to collect?
 *
 * True when at least one of the markets being launched has a candle inside the window. ⚠️ The
 * markets missing from `plan` are the fully covered ones, which run — so a symbol nobody planned
 * for is a symbol that can run.
 */
export function anythingToRun(
  symbols: readonly string[],
  plan: readonly PlannedCollection[],
): boolean {
  const empty = new Set(
    plan.filter((market) => !market.in_window).map((market) => market.symbol),
  )
  return symbols.some((symbol) => !empty.has(symbol))
}

/**
 * The class the catalogue already holds for each symbol.
 *
 * ⚠️ **Sent with every collection this prompt queues.** The collection endpoint decides a class
 * from the broker's tree path and refuses the 24 symbols of this broker filed under Metals, CFDs
 * or Crypto Currency — symbols the catalogue already knows the class of, because a collection
 * put them there. The class sent is that recorded answer, not a guess.
 */
export function catalogueClasses(
  instruments: readonly Instrument[] | undefined,
): ReadonlyMap<string, AssetClass> {
  const classes = new Map<string, AssetClass>()
  for (const instrument of instruments ?? []) {
    if (ASSET_CLASSES.includes(instrument.asset_class)) {
      classes.set(instrument.symbol, instrument.asset_class as AssetClass)
    }
  }
  return classes
}

/** One key per symbol × window in a request — what "already queued" is remembered by. */
export function queuedKeys(body: CreateCollection): string[] {
  return body.rows.flatMap((row) =>
    body.items.map((item) => `${item.symbol}|${row.timeframe}|${row.date_from}|${row.date_to}`),
  )
}

/**
 * The collection requests that fetch everything in `plan` not already in `queued`.
 *
 * ⚠️ **One row per request, always.** A request's rows must name distinct timeframes, and one
 * pair can need two windows at the same timeframe — a row per request makes that refusal
 * impossible rather than something to remember. Markets needing the very same window at the same
 * chart share a request, which is what the screen would have sent by hand.
 *
 * ⚠️ **`queued` is what this screen already sent.** The server does not merge identical
 * requests, so a second press — after a refusal part-way, or before the first collection has
 * finished — would download the same years twice.
 */
export function collectionRequests(
  plan: readonly PlannedCollection[],
  classes: ReadonlyMap<string, AssetClass> = new Map(),
  queued: ReadonlySet<string> = new Set(),
): CreateCollection[] {
  const groups = new Map<string, { row: CreateCollection['rows'][number]; symbols: string[] }>()
  for (const market of plan) {
    for (const window of market.windows) {
      const key = `${market.timeframe}|${window.date_from}|${window.date_to}`
      if (queued.has(`${market.symbol}|${key}`)) continue
      const group = groups.get(key) ?? {
        row: { timeframe: market.timeframe, ...window },
        symbols: [],
      }
      group.symbols.push(market.symbol)
      groups.set(key, group)
    }
  }

  const requests: CreateCollection[] = []
  for (const { row, symbols } of groups.values()) {
    for (let start = 0; start < symbols.length; start += MAX_SYMBOLS_PER_REQUEST) {
      requests.push({
        items: symbols.slice(start, start + MAX_SYMBOLS_PER_REQUEST).map((symbol) => {
          const assetClass = classes.get(symbol)
          return assetClass === undefined ? { symbol } : { symbol, asset_class: assetClass }
        }),
        rows: [row],
      })
    }
  }
  return requests
}
