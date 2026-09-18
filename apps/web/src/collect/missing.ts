// What a launch is missing, turned into what to say and what to send.
//
// The plan itself — which years, why whole years, why reaching the disk — is the server's
// (`POST /collections/plan`), and so is queueing the downloads (`collect_missing`). This module
// only reads the plan's answer: one sentence per market, and whether anything would run without
// collecting. No React, so both launch screens say the same thing.

import type { PlannedCollection } from '../api/types'

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
 * True when at least one of the markets being launched has a candle inside the window. A symbol
 * the plan does not mention needs no collection, and is taken to run.
 *
 * ⚠️ **Keyed by symbol alone**, because both launch screens ask the plan about one chart. A plan
 * covering several timeframes would need the pair, and `PlanCollectionRequest.timeframes` is a
 * list, so a third caller must not reuse this as it stands.
 *
 * ⚠️ **"Needs no collection" is not quite "has candles".** A window entirely before the broker's
 * oldest bar, or entirely in the future, is planned as nothing to fetch and still holds no
 * candle. The launch answers that one: the basket leaves the market out and names it, the single
 * backtest is refused.
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
