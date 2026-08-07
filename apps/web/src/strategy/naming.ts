// Naming a run so nobody has to.
//
// The strategy name used to be typed by hand on every launch, and it does more work than a label:
// the builder chooses `POST` versus `PUT` by comparing it, so the name *is* the lineage key. Typing
// it had two costs. Coming back the next day and reaching for the same obvious name — "Ponto
// Contínuo" — collided with the row already saved under it, and the API answered 409 on a name the
// screen had no way to know was taken. And when a name was invented to dodge that, it carried no
// information: "ponto 2" says nothing about when it ran or what it ran.
//
// A generated name fixes both by being unique on the one axis that is always unique — the instant
// it was created.

/**
 * The abbreviation each strategy is named after.
 *
 * Keyed by `StrategyChoice.id`, which for setups is the schema's own type name, so the map is
 * checked against the schema by a test rather than by memory: adding a setup in Python and
 * forgetting an abbreviation here fails CI with the type named, instead of shipping a run called
 * `STRUCT-20260807-151230` that nobody can tell from its sibling.
 */
export const RUN_ABBREV: Record<string, string> = {
  mme9_breakout: 'MME9',
  ponto_continuo: 'PCONT',
  structure_choch: 'SCHOCH',
  structure_continuation: 'SCONT',
  ma_cross: 'MACROSS',
  rsi_oversold: 'RSIOS',
}

/**
 * A last-resort abbreviation for a strategy this file has not been told about.
 *
 * Derived rather than thrown, because a picker that crashes on a setup the backend legitimately
 * added would be worse than an ugly name — the schema is allowed to lead. The test that pins every
 * known type is what keeps this from being reached in practice.
 */
function derive(id: string): string {
  const letters = id.replace(/[^a-z0-9]/gi, '').toUpperCase()
  return letters.slice(0, 8) || 'RUN'
}

function pad(value: number, width = 2): string {
  return String(value).padStart(width, '0')
}

/**
 * The instant, as `AAAAMMDD-HHMMSS`, in the reader's **local** time.
 *
 * Local and not UTC on purpose: this string is a label a human recognises — "the one I ran after
 * lunch" — and an author three hours off UTC reading 18:12 for a run they launched at 15:12 would
 * be reading a lie about their own afternoon. Nothing orders by it: the run log sorts on
 * `created_at`, which is UTC and authoritative. The name only has to be recognisable and unique.
 *
 * Fixed-width fields throughout, so the strings also happen to sort chronologically as text.
 */
export function stamp(at: Date): string {
  const date = `${String(at.getFullYear())}${pad(at.getMonth() + 1)}${pad(at.getDate())}`
  const time = `${pad(at.getHours())}${pad(at.getMinutes())}${pad(at.getSeconds())}`
  return `${date}-${time}`
}

/**
 * The name a run is saved under: `SCHOCH-20260807-151230`.
 *
 * Generated when a strategy is *picked*, not when it is launched, and that timing is the whole
 * design. Nudging a parameter and running again keeps the name, so the save is a `PUT` and the
 * database records it as the next version of the same lineage — which is exactly what versioning
 * is for. Picking the strategy again mints a new stamp and starts a new lineage. So the two
 * behaviours that were previously a manual decision, and usually the wrong one, now follow from
 * what the author actually did.
 *
 * Seconds are the resolution because they are enough: two launches of the same strategy inside one
 * second is not a thing a person does through this screen, and the name stays editable for the
 * case where someone wants to label a run rather than stamp it.
 */
export function runName(choiceId: string, at: Date): string {
  return `${RUN_ABBREV[choiceId] ?? derive(choiceId)}-${stamp(at)}`
}
