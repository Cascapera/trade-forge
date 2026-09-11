// What the catalogue shows out of what the server sent: the rows a search leaves behind, and a
// sweep said in one line. Pure, and separate from the screen, because both are decisions with a
// wrong answer — and a decision with a wrong answer wants a test that does not have to render.

/** The fields either kind of row carries — a strategy or a catalogue entry. */
export interface Searchable {
  id: string
  name: string
  /** The named setup the document runs, or `null` for one built from indicators. */
  setup: string | null
}

/**
 * The rows a search box leaves behind.
 *
 * ⚠️ **The text matches the name or the setup**, because both are how somebody looks for a
 * strategy here: `9.1` is a name a person typed, and `mme9_turn` is what the engine runs.
 * Matching only the name hides every row whose author named it something else, and this
 * project's own database holds `Structure — CHoCH 56454`, which runs `mme9_breakout`.
 *
 * Empty text matches everything rather than nothing — an empty search box is the absence of a
 * question, not a question no row answers.
 *
 * ⚠️ There was a setup filter beside this, and it went when the screen stopped listing
 * documents and started listing entries. It was a second way to ask what the search box already
 * answers, and a shelf holds tens of entries rather than the hundreds that make a dropdown earn
 * its place. Kept in the history rather than in the signature: a parameter every caller passes
 * empty is a branch that only its own test can reach.
 */
export function filterCatalogue<T extends Searchable>(items: T[], text: string): T[] {
  const needle = text.trim().toLowerCase()
  if (needle === '') return items
  return items.filter(
    (item) =>
      item.name.toLowerCase().includes(needle) ||
      item.setup?.toLowerCase().includes(needle) === true,
  )
}

/**
 * `period=5, 9, 21 · stop_buffer=0.1, 0.2` — a sweep said in one line.
 *
 * The **last segment** of each path, because `setup.params.` is on every one of them and a
 * column of identical prefixes is a column nobody reads. The full path is still what travels to
 * the server; this is a caption, and nothing should be parsed back out of it.
 *
 * An empty grid gives an empty string, which the screen uses as "there is nothing to say here"
 * rather than printing a bare separator.
 */
export function gridSummary(grid: Record<string, unknown[]>): string {
  return Object.entries(grid)
    .map(([path, values]) => `${path.split('.').at(-1) ?? path}=${values.join(', ')}`)
    .join(' · ')
}
