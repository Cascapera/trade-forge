// A run's R by year, read for display (01/10). The server keeps R by year of entry, then year of
// exit (`r_by_years`, ADR-0030); a screen shows a row per year of entry and says when part of it
// left in a later year — that part is what a cut ending in the entry year leaves out.

/** One year of entry: the R its trades made wherever they left, and where they left. */
export interface EntryYear {
  year: number
  /** Summed over every year of exit, as a decimal string for the formatters. */
  r: string
  /** Per year of exit, earliest first. One entry, the year itself, for most years. */
  exits: { year: number; r: string }[]
  /** Some of its trades left in a later year. */
  leftLater: boolean
}

/**
 * A sum of decimal strings, back as a string. ⚠️ Rounded to 1e-8 and cleared of `-0`: summed as
 * floats, `0.1 + 0.2 - 0.3` is `5.5e-17`, which the formatters would print as `+0.00 R` — or, below
 * zero, `-0.00 R`, a loss that is not there. Display only; nothing decides on it.
 */
export function sumR(values: readonly string[]): string {
  const total = values.reduce((sum, value) => sum + Number(value), 0)
  const rounded = Math.round(total * 1e8) / 1e8
  return String(rounded === 0 ? 0 : rounded)
}

/** `r_by_years` as rows, earliest year of entry first. */
export function entryYears(rByYears: Record<string, Record<string, string>>): EntryYear[] {
  return Object.entries(rByYears)
    .map(([entered, byExit]) => {
      const year = Number(entered)
      const exits = Object.entries(byExit)
        .map(([left, r]) => ({ year: Number(left), r }))
        .sort((a, b) => a.year - b.year)
      return {
        year,
        r: sumR(exits.map((exit) => exit.r)),
        exits,
        leftLater: exits.some((exit) => exit.year !== year),
      }
    })
    .sort((a, b) => a.year - b.year)
}

/**
 * The calendar years a window `[dateFrom, dateTo)` touches, in UTC — the years a run of it can
 * have an entry in. ⚠️ `dateTo` is exclusive: a window to 1 January 2025 ends in 2024, and offering
 * 2025 would offer a year the run never saw.
 */
export function windowYears(dateFrom: string, dateTo: string): number[] {
  const first = new Date(dateFrom).getUTCFullYear()
  const last = new Date(new Date(dateTo).getTime() - 1).getUTCFullYear()
  const years: number[] = []
  for (let year = first; year <= Math.max(first, last); year += 1) years.push(year)
  return years
}
