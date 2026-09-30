import { describe, expect, it } from 'vitest'

import { describeParameters, setupName } from './describe'

function choch(params: Record<string, unknown>): Record<string, unknown> {
  return {
    schema_version: '1.0',
    name: 'SCHOCH-20260922-222429',
    timeframe: 'M15',
    setup: { type: 'structure_choch', params },
    exit: { take_profit: { type: 'risk_multiple', params: { rr: 3 } } },
    risk: { sizing: { type: 'percent_risk', params: { percent: 1 } } },
  }
}

function row(rows: ReturnType<typeof describeParameters>, label: string) {
  const found = rows.find((one) => one.label === label)
  if (found === undefined) throw new Error(`no row ${label}`)
  return found
}

describe('setupName', () => {
  it('uses his names for the setups, and says what a DSL document is', () => {
    expect(setupName('mme9_turn')).toBe('MME9 — virada (9.1)')
    expect(setupName('structure_choch')).toBe('Estrutura — CHoCH')
    expect(setupName(null)).toBe('Estratégia por indicadores (DSL)')
    expect(setupName('something_new')).toBe('something_new')
  })
})

describe('describeParameters', () => {
  it('says each value in words, and marks what the document left to the default', () => {
    const rows = describeParameters(
      choch({ side: 'long', entry_point: 'midpoint', breakeven_at_r: null, stop_buffer: 0.2 }),
    )

    expect(row(rows, 'Lado')).toMatchObject({ value: 'só compra', byDefault: false })
    expect(row(rows, 'Ponto de entrada').value).toBe('meio da região (50%)')
    expect(row(rows, 'Breakeven em').value).toBe('desligado')
    expect(row(rows, 'Folga do stop (fração da região)').value).toBe('0.2')
    // Not stated: the schema's default runs, and the row says so.
    expect(row(rows, 'Stop do gift')).toMatchObject({ value: 'gift', byDefault: true })
    expect(row(rows, 'Alvo').value).toBe('3R')
    expect(row(rows, 'Risco por trade').value).toBe('1% do saldo')
  })

  it('shows the filter above and its choices only when there is a filter', () => {
    const off = describeParameters(choch({ htf: null }))
    const on = describeParameters(
      choch({ htf: 'H4', htf_regions: 'with_trend', htf_allow_secondary: false }),
    )

    expect(row(off, 'Tempo gráfico superior (HTF)').value).toBe('desligado')
    expect(off.some((one) => one.label === 'Regiões do HTF')).toBe(false)
    expect(row(on, 'Tempo gráfico superior (HTF)').value).toBe('H4')
    expect(row(on, 'Regiões do HTF').value).toBe('só a favor da última quebra')
    expect(row(on, 'Secundárias do HTF').value).toBe('só primárias')
    // The clock is the instrument's since 30/09, not a parameter of the strategy.
    expect(on.some((one) => one.label === 'Relógio do HTF')).toBe(false)
  })

  it('marks what the sweep varied at this point, and nothing else', () => {
    const rows = describeParameters(choch({ side: 'both', stop_buffer: 0.2 }), {
      'setup.params.stop_buffer': 0.2,
      'exit.take_profit.params.rr': 3,
    })

    expect(rows.filter((one) => one.optimised).map((one) => one.label)).toEqual([
      'Folga do stop (fração da região)',
      'Alvo',
    ])
  })

  it('says a run with no target has none', () => {
    const document = { ...choch({}), exit: { take_profit: null } }

    expect(row(describeParameters(document), 'Alvo').value).toBe(
      'sem alvo (sai pelo stop ou pelo setup)',
    )
  })

  it('gives a DSL document the target and the risk, with no setup rows', () => {
    const document = {
      schema_version: '1.0',
      name: 'MACROSS',
      timeframe: 'H1',
      indicators: [],
      exit: { take_profit: { type: 'risk_multiple', params: { rr: 2 } } },
      risk: { sizing: { type: 'percent_risk', params: { percent: 0.5 } } },
    }

    expect(describeParameters(document).map((one) => one.label)).toEqual([
      'Alvo',
      'Risco por trade',
    ])
  })
})
