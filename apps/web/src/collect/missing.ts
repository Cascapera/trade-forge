// What a launch is missing, turned into what to say and what to send.
//
// The plan itself — which years, why whole years, why reaching the disk — is the server's
// (`POST /collections/plan`), and so is queueing the downloads (`collect_missing`). This module
// only reads the plan's answer: one sentence per market, and whether anything would run without
// collecting. No React, so both launch screens say the same thing.

import type { PlannedCollection } from '../api/types'
import { roughly } from '../format'

function year(iso: string): string {
  return iso.slice(0, 4)
}

function years(from: string, to: string): string {
  return year(from) === year(to) ? year(from) : `${year(from)}–${year(to)}`
}

/**
 * `EURUSD H1 — on disk 2021-01-04 to 2025-12-31; would fetch 2019–2020, 2026`, or, for a symbol
 * the broker does not list, `AAPL H1 — never collected; not listed by the broker, cannot be
 * collected`.
 */
export function missingLine(market: PlannedCollection): string {
  const held = market.covers === null ? 'never collected' : `on disk ${market.covers}`
  if (market.at_broker === false) {
    return `${market.symbol} ${market.timeframe} — ${held}; not listed by the broker, cannot be collected`
  }
  const fetched = market.windows.map((w) => years(w.date_from, w.date_to)).join(', ')
  const wait = market.time === null ? '' : `, ${roughly(market.time.seconds)}`
  return `${market.symbol} ${market.timeframe} — ${held}; would fetch ${fetched}${wait}`
}

/**
 * How long "Collect and run" would spend downloading, one pair after another — the host agent
 * takes one at a time. Only the pairs it would actually fetch count (`at_broker !== false`).
 *
 * `null` when any of those has no estimate: a sum missing a term is a smaller number than the
 * truth, and "about 5 min" said over a download nobody could measure would be the one number on
 * the screen that is wrong without looking it.
 */
export function downloadTime(plan: readonly PlannedCollection[]): number | null {
  let total = 0
  for (const market of plan) {
    if (market.at_broker === false) continue
    if (market.time === null) return null
    total += market.time.seconds
  }
  return total
}

/**
 * Would "Collect and run" fetch anything at all?
 *
 * ⚠️ `null` counts as collectable: an unsynced symbol list is a question nobody has asked, and
 * the launch still collects it (`to_collect` on the server). Only `false` is a no.
 */
export function anythingToCollect(plan: readonly PlannedCollection[]): boolean {
  return plan.some((market) => market.at_broker !== false)
}

/** One market on one chart — what a run reads, and what the plan answers about. */
export interface Pair {
  symbol: string
  timeframe: string
}

/** Every market on every chart: the pairs a launch over these axes reads. */
export function pairsOf(symbols: readonly string[], timeframes: readonly string[]): Pair[] {
  return timeframes.flatMap((timeframe) => symbols.map((symbol) => ({ symbol, timeframe })))
}

/**
 * Would anything run right now, if the person declined to collect?
 *
 * True when at least one of the pairs being launched has a candle inside the window. A pair the
 * plan does not mention needs no collection, and is taken to run.
 *
 * ⚠️ **Keyed by the pair, not the symbol.** The sweep asks the plan about several charts at
 * once, and a market can hold M15 and not H4: keyed by symbol, its empty H4 would read as the
 * whole market being empty, or its full M15 as the whole market being full, depending on which
 * entry the plan happened to list.
 *
 * ⚠️ **"Needs no collection" is not quite "has candles".** A window entirely before the broker's
 * oldest bar, or entirely in the future, is planned as nothing to fetch and still holds no
 * candle. The launch answers that one: the basket and the sweep leave the pair out and name it,
 * the single backtest is refused.
 */
export function anythingToRun(
  pairs: readonly Pair[],
  plan: readonly PlannedCollection[],
): boolean {
  const key = (pair: Pair): string => `${pair.symbol}|${pair.timeframe}`
  const empty = new Set(plan.filter((market) => !market.in_window).map(key))
  return pairs.some((pair) => !empty.has(key(pair)))
}
