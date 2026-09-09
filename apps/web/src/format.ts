// Pure display formatters. Money and ratios arrive as exact-decimal *strings*; these parse them
// to a Number only at the very edge, to render — the value that decides anything already did so
// on the backend. Kept out of the components so they can be tested directly.

export function money(value: string): string {
  return Number(value).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
}

export function signedMoney(value: string): string {
  const n = Number(value)
  return `${n >= 0 ? '+' : '−'}${money(Math.abs(n).toString())}`
}

/** A whole count, grouped. The locale is pinned for the same reason `money` pins it: a
 * rendered string that changes with the machine's locale is a test that passes on one CI
 * runner and fails on the next. */
export function count(value: number): string {
  return value.toLocaleString('en-US')
}

export function percent(fraction: string | null, digits = 1): string {
  if (fraction === null) return '—'
  return `${(Number(fraction) * 100).toFixed(digits)}%`
}

export function ratio(value: string | null, digits = 2): string {
  if (value === null) return '—'
  return Number(value).toFixed(digits)
}

/** Positive → good, negative → bad, zero → neutral. Drives the one status colour on the P&L. */
export function sign(value: string): 'up' | 'down' | 'flat' {
  const n = Number(value)
  if (n > 0) return 'up'
  if (n < 0) return 'down'
  return 'flat'
}

/**
 * An ISO 8601 duration as something a person reads: `2d 3h`, `45m`, `0s`.
 *
 * ⚠️ **`Y` is not decoration and dropping it silently loses a year at a time.** Pydantic
 * serialises a `timedelta` into the largest units it can, so 400 days comes over the wire as
 * `P1Y35DT2H` — a parser that read only `D` would render that as `35d 2h` and be wrong by a
 * factor of eleven, with nothing on screen looking odd. Measured against the serialiser: a year
 * there is exactly 365 days (`P2Y270D` is 1000 days), so reading one back as 365 is exact rather
 * than an approximation of a calendar.
 *
 * Two units, never three: `2d 3h 14m` is a precision nobody acts on, and the third unit pushes
 * the number people came for off the end of a tile.
 *
 * ⚠️ Anything this cannot parse is an em dash, never `0s`. A zero duration is a real answer — a
 * trade that opened and closed on one bar — and a string nobody could read is not.
 */
const ISO_DURATION =
  /^P(?:(\d+(?:\.\d+)?)Y)?(?:(\d+(?:\.\d+)?)D)?(?:T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?)?$/

const DAY_SECONDS = 86_400

export function duration(iso: string | null): string {
  if (iso === null) return '—'
  const found = ISO_DURATION.exec(iso)
  if (found === null) return '—'
  // ⚠️ Cast because `RegExpExecArray` is typed as an array of `string`, and an optional group
  // that did not match is `undefined` at runtime. Believing the type here turns a missing group
  // into `Number(undefined)`, which is `NaN`, which renders as `NaNd`.
  const groups = found.slice(1) as (string | undefined)[]
  // `P` alone satisfies the pattern with every group empty, and it carries no number at all.
  if (groups.every((part) => part === undefined)) return '—'
  const [years, days, hours, minutes, seconds] = groups.map((part) =>
    part === undefined ? 0 : Number(part),
  ) as [number, number, number, number, number]

  const total =
    years * 365 * DAY_SECONDS + days * DAY_SECONDS + hours * 3600 + minutes * 60 + seconds
  const whole = Math.floor(total)
  const parts: [number, string][] = [
    [Math.floor(whole / DAY_SECONDS), 'd'],
    [Math.floor((whole % DAY_SECONDS) / 3600), 'h'],
    [Math.floor((whole % 3600) / 60), 'm'],
    [whole % 60, 's'],
  ]

  const first = parts.findIndex(([value]) => value > 0)
  // Every unit is zero: the duration is zero, which is a measurement and not an absence.
  if (first === -1) return '0s'
  return parts
    .slice(first, first + 2)
    // The second unit only when it says something. The first is `> 0` by construction — it is
    // where `findIndex` stopped — so it needs no exception of its own.
    .filter(([value]) => value > 0)
    .map(([value, unit]) => `${String(value)}${unit}`)
    .join(' ')
}
