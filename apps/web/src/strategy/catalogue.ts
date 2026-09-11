// What the catalogue shows out of what the server sent: the setups actually on the shelf, and the
// rows a search leaves behind. Pure, and separate from the screen, because both are decisions with
// a wrong answer — and a decision with a wrong answer wants a test that does not have to render.

import type { StrategyListItem } from '../api/types'

/**
 * Which named setups the catalogue actually holds, sorted.
 *
 * ⚠️ Derived from the rows rather than from the schema's list of every setup that exists. The two
 * are different questions: the schema knows every setup the engine can run, and a filter built
 * from that offers options that empty the table — a dropdown mostly made of setups nothing has
 * been saved under. A filter should describe the shelf, not the catalogue of what could go on it.
 *
 * A document built from indicators and conditions has no named setup and contributes nothing
 * here; the table still shows it, under `DSL`.
 */
export function setupsIn(items: StrategyListItem[]): string[] {
  const seen = new Set<string>()
  for (const item of items) if (item.setup !== null) seen.add(item.setup)
  return [...seen].sort()
}

/**
 * The rows a search box and a setup filter leave behind.
 *
 * ⚠️ **The text matches the name or the setup**, because both are how somebody looks for a
 * strategy here: `9.1` is a name a person typed, and `mme9_turn` is what the engine runs.
 * Matching only the name hides every row whose author named it something else, and this
 * project's own database holds `Structure — CHoCH 56454`, which runs `mme9_breakout`.
 *
 * The two filters are an `AND`: a setup chosen and text typed means both, which is what a reader
 * narrowing a list expects. Empty text matches everything rather than nothing — an empty search
 * box is the absence of a question, not a question no row answers.
 */
export function filterCatalogue(
  items: StrategyListItem[],
  text: string,
  setup: string,
): StrategyListItem[] {
  const needle = text.trim().toLowerCase()
  return items.filter((item) => {
    if (setup !== '' && item.setup !== setup) return false
    if (needle === '') return true
    return (
      item.name.toLowerCase().includes(needle) ||
      item.setup?.toLowerCase().includes(needle) === true
    )
  })
}
