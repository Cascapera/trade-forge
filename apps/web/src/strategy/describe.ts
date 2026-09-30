// A strategy document said in words (29/09, his ask): what a result page puts at the top — which
// strategy ran and every parameter it ran with, in Portuguese, with the ones a sweep varied marked.
//
// Pure and apart from the page, for the reason `catalogue.ts` gives: every sentence here is a
// decision with a wrong answer, and a wrong answer wants a test that does not have to render.
//
// ⚠️ **The document is read, never the name.** A sweep's point is its own strategy row, so its
// document already carries the point's values; the point's coordinates only say *which* of those
// values the sweep varied.

import { SETUP_TYPES, setupSpec } from '@tradeforge/schema'

/** How each setup is called — the names he uses (9.1, 9.2/9.3, 9.4), not the engine's. */
const SETUP_NAMES: Record<string, string> = {
  mme9_breakout: 'MME9 — rompimento',
  mme9_turn: 'MME9 — virada (9.1)',
  mme9_pullback: 'MME9 — pullback (9.2/9.3)',
  mme9_failed_turn: 'MME9 — virada falha (9.4)',
  ponto_continuo: 'Ponto Contínuo',
  structure_choch: 'Estrutura — CHoCH',
  structure_continuation: 'Estrutura — continuação',
}

type Say = (value: unknown) => string

const yesNo =
  (yes: string, no: string): Say =>
  (value) =>
    value === true ? yes : value === false ? no : String(value)

const orOff =
  (off: string, say: Say = String): Say =>
  (value) =>
    value === null ? off : say(value)

const inR: Say = (value) => `${String(value)}R`

const oneOf =
  (words: Record<string, string>): Say =>
  (value) =>
    typeof value === 'string' ? (words[value] ?? value) : String(value)

/** Each parameter's name and how its value is said. A parameter missing here is shown by its
 *  own key and raw value — never hidden: an unknown knob is still a knob the run was set to. */
const PARAMS: Record<string, { label: string; say: Say }> = {
  side: {
    label: 'Lado',
    say: oneOf({ long: 'só compra', short: 'só venda', both: 'compra e venda' }),
  },
  period: { label: 'Período da média', say: String },
  long_average_period: { label: 'Média longa (filtro)', say: orOff('desligada') },
  average: { label: 'Tipo de média', say: oneOf({ EMA: 'exponencial', SMA: 'simples' }) },
  stop_buffer: { label: 'Folga do stop (fração da região)', say: String },
  stop_buffer_ticks: { label: 'Folga do stop (ticks)', say: String },
  breakeven_at_r: { label: 'Breakeven em', say: orOff('desligado', inR) },
  entry_point: {
    label: 'Ponto de entrada',
    say: oneOf({
      edge: 'borda da região',
      midpoint: 'meio da região (50%)',
      return_pass: 'passagem de retorno',
      botinha: 'botinha',
      fffd: 'FFFD',
      martelo: 'martelo',
      martelo_forca: 'martelo de força',
      gift: 'gift',
      barra_ignorada: 'barra ignorada',
      classic: 'clássico',
    }),
  },
  gift_stop: { label: 'Stop do gift', say: oneOf({ gift: 'gift', forca: 'barra de força' }) },
  volume_filter: { label: 'Filtro de volume', say: yesNo('ligado', 'desligado') },
  allow_secondary: { label: 'Regiões secundárias', say: yesNo('aceitas', 'só primárias') },
  max_bos: { label: 'Máximo de BOS', say: orOff('sem limite') },
  corrections: { label: 'Correções', say: String },
  htf: { label: 'Tempo gráfico superior (HTF)', say: orOff('desligado') },
  htf_regions: {
    label: 'Regiões do HTF',
    say: oneOf({
      any: 'qualquer região intocada',
      with_trend: 'só a favor da última quebra',
    }),
  },
  htf_allow_secondary: {
    label: 'Secundárias do HTF',
    say: yesNo('aceitas', 'só primárias'),
  },
}

/** Parameters read only when another one is on: shown only then, as the engine reads them. */
const ONLY_WITH: Record<string, string> = {
  htf_regions: 'htf',
  htf_allow_secondary: 'htf',
}

export interface Described {
  /** Where the value sits in the document — what a sweep's coordinates are keyed by. */
  path: string
  label: string
  value: string
  /** Left to the engine: the document does not state it, so the schema's default runs. */
  byDefault: boolean
  /** A value this run's point of a sweep varied. */
  optimised: boolean
}

export function setupName(type: string | null): string {
  if (type === null) return 'Estratégia por indicadores (DSL)'
  return SETUP_NAMES[type] ?? type
}

function said(key: string, value: unknown): { label: string; value: string } {
  const known = PARAMS[key]
  return known === undefined
    ? { label: key, value: JSON.stringify(value) }
    : { label: known.label, value: known.say(value) }
}

/**
 * Every parameter the run was set to, in words — the setup's, then the target and the risk.
 *
 * `varied` are the point's coordinates (`BacktestPoint.values`), or nothing for a run launched on
 * its own: only their paths are read, to mark what the sweep changed.
 */
export function describeParameters(
  definition: Record<string, unknown>,
  varied: Record<string, unknown> = {},
): Described[] {
  const out: Described[] = []
  const setup = definition.setup as { type?: unknown; params?: Record<string, unknown> } | null
  const type = typeof setup?.type === 'string' ? setup.type : null
  if (type !== null && (SETUP_TYPES as readonly string[]).includes(type)) {
    const given = setup?.params ?? {}
    for (const param of setupSpec(type as (typeof SETUP_TYPES)[number]).params) {
      const on = ONLY_WITH[param.name]
      if (on !== undefined && (given[on] ?? null) === null) continue
      const stated = Object.hasOwn(given, param.name)
      const value = stated ? given[param.name] : param.default
      const path = `setup.params.${param.name}`
      out.push({
        path,
        ...said(param.name, value),
        byDefault: !stated,
        optimised: Object.hasOwn(varied, path),
      })
    }
  }

  const target = (
    definition.exit as { take_profit?: { type?: unknown; params?: { rr?: unknown } } | null } | undefined
  )?.take_profit
  out.push({
    path: 'exit.take_profit.params.rr',
    label: 'Alvo',
    value:
      target === undefined || target === null
        ? 'sem alvo (sai pelo stop ou pelo setup)'
        : target.type === 'risk_multiple' && target.params?.rr !== undefined
          ? inR(target.params.rr)
          : JSON.stringify(target),
    byDefault: false,
    optimised: Object.keys(varied).some((key) => key.startsWith('exit.take_profit')),
  })
  const percent = (
    definition.risk as { sizing?: { params?: { percent?: unknown } } } | undefined
  )?.sizing?.params?.percent
  if (percent !== undefined) {
    out.push({
      path: 'risk.sizing.params.percent',
      label: 'Risco por trade',
      value: typeof percent === 'number' ? `${String(percent)}% do saldo` : JSON.stringify(percent),
      byDefault: false,
      optimised: Object.hasOwn(varied, 'risk.sizing.params.percent'),
    })
  }
  return out
}
