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
