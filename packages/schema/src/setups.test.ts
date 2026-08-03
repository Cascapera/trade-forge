import { describe, expect, expectTypeOf, it } from 'vitest'

import {
  readSetups,
  SETUP_TYPES,
  SETUPS,
  setupSpec,
  type SetupParam,
  type SetupType,
} from './setups.js'

function param(type: SetupType, name: string): SetupParam {
  const found = setupSpec(type).params.find((candidate) => candidate.name === name)
  if (found === undefined) throw new Error(`${type} has no parameter ${name}`)
  return found
}

describe('the setups the DSL can name', () => {
  it('covers every type in the generated union, and no more', () => {
    // The object literal is the drift guard, and it works at *compile* time: `Record<SetupType, …>`
    // fails to typecheck the moment Python grows a fifth setup and the types are regenerated. The
    // runtime comparison then catches the opposite mistake — a key here that the schema dropped.
    const expected: Record<SetupType, true> = {
      mme9_breakout: true,
      ponto_continuo: true,
      structure_choch: true,
      structure_continuation: true,
    }
    expect(new Set(SETUP_TYPES)).toEqual(new Set(Object.keys(expected)))
    expect(SETUPS).toHaveLength(SETUP_TYPES.length)
  })

  it('names an unknown setup as an error rather than returning nothing', () => {
    expect(() => setupSpec('escada' as SetupType)).toThrow(/no setup named escada/)
  })

  it('types SetupType as the literal names, not as string', () => {
    expectTypeOf<SetupType>().toEqualTypeOf<
      'mme9_breakout' | 'ponto_continuo' | 'structure_choch' | 'structure_continuation'
    >()
  })
})

describe('the defaults the form shows', () => {
  // These are the author's own numbers. They are asserted here, in the package that reads them,
  // because a form silently falling back to 0 or to an empty box is indistinguishable from a form
  // showing a real default — and the whole reason the Pydantic model repeats the engine's default
  // is so the builder can show it (ADR-0019).
  it.each([
    ['mme9_breakout', 'period', 9],
    ['ponto_continuo', 'period', 20],
    ['mme9_breakout', 'breakeven_at_r', 2],
    ['ponto_continuo', 'breakeven_at_r', 2],
    ['structure_choch', 'stop_buffer', 0.1],
    ['structure_continuation', 'stop_buffer', 0.1],
  ] as const)('reads %s.%s as %s', (type, name, expected) => {
    expect(param(type, name)).toMatchObject({ default: expected })
  })

  it('reads the average as an enum defaulting to EMA', () => {
    expect(param('ponto_continuo', 'average')).toEqual({
      name: 'average',
      kind: 'enum',
      required: false,
      default: 'EMA',
      options: ['EMA', 'SMA'],
    })
  })

  it('reads allow_secondary as a flag that is off by default', () => {
    expect(param('structure_choch', 'allow_secondary')).toEqual({
      name: 'allow_secondary',
      kind: 'boolean',
      required: false,
      default: false,
    })
  })
})

describe('the parameters a form has to treat specially', () => {
  it('gives side no default, so the form cannot pre-answer it', () => {
    // `side` is required in the schema on purpose: the engine classes default it to LONG, and a
    // pre-selected form field would turn a forgotten choice into a whole long-only backtest read
    // as the setup's result.
    expect(param('mme9_breakout', 'side')).toEqual({
      name: 'side',
      kind: 'enum',
      required: true,
      default: null,
      options: ['long', 'short'],
    })
  })

  it('puts the required parameter first', () => {
    expect(setupSpec('ponto_continuo').params[0]?.name).toBe('side')
  })

  it('has no side at all for the structure family', () => {
    // Which way a structure setup trades follows the structure it reads, so there is nothing to ask.
    for (const type of ['structure_choch', 'structure_continuation'] as const) {
      expect(setupSpec(type).params.map((p) => p.name)).not.toContain('side')
      expect(setupSpec(type).params.every((p) => !p.required)).toBe(true)
    }
  })

  it('marks breakeven_at_r nullable, because null is "off" rather than "unset"', () => {
    expect(param('ponto_continuo', 'breakeven_at_r')).toMatchObject({
      kind: 'number',
      nullable: true,
      default: 2,
    })
  })

  it('marks max_bos nullable with a null default, which means uncapped', () => {
    expect(param('structure_continuation', 'max_bos')).toMatchObject({
      kind: 'integer',
      nullable: true,
      default: null,
      min: 1,
      max: 100,
    })
  })

  it('carries the bounds the schema declares, so the form can refuse out-of-range input', () => {
    expect(param('mme9_breakout', 'period')).toMatchObject({ min: 1, max: 1000, nullable: false })
    expect(param('structure_choch', 'stop_buffer')).toMatchObject({ min: 0, max: 10 })
  })
})

describe('reading a schema that is not the one we generate', () => {
  // Each of these guards a way the contract could change underneath this file. They are asserted
  // rather than assumed, because a refusal nothing exercises is a guess about what the generator
  // emits — and the failure it would otherwise produce is a form field rendered wrong, silently.

  /** The smallest document that gets as far as reading one parameter. */
  function withParam(node: unknown, extra: Record<string, unknown> = {}): unknown {
    return {
      $defs: {
        Setup: { discriminator: { propertyName: 'type', mapping: { only: '#/$defs/OnlySetup' } } },
        OnlySetup: { properties: { params: { $ref: '#/$defs/OnlyParams' } } },
        OnlyParams: { properties: { knob: node } },
        ...extra,
      },
    }
  }

  it('refuses a union with no discriminator, rather than reporting no setups at all', () => {
    // The empty list is the dangerous answer: a builder with no setups looks like a feature that
    // was never built, not like a contract that broke.
    expect(() => readSetups({ $defs: { Setup: {} } })).toThrow(/lost its discriminator/)
    expect(() => readSetups({})).toThrow(/lost its discriminator/)
  })

  it('names the reference it could not resolve', () => {
    const orphan = {
      $defs: { Setup: { discriminator: { propertyName: 'type', mapping: { a: '#/$defs/Gone' } } } },
    }
    expect(() => readSetups(orphan)).toThrow(/no definition for #\/\$defs\/Gone/)
  })

  it('refuses a setup whose params it cannot find', () => {
    const noParams = {
      $defs: {
        Setup: { discriminator: { propertyName: 'type', mapping: { bare: '#/$defs/BareSetup' } } },
        BareSetup: { properties: { type: { const: 'bare' } } },
      },
    }
    expect(() => readSetups(noParams)).toThrow(/setup bare has no params definition/)
  })

  it('refuses a parameter no form control can hold', () => {
    expect(() => readSetups(withParam({ type: 'array' }))).toThrow(/no form control can hold/)
  })

  it('refuses a parameter whose only type is null', () => {
    expect(() => readSetups(withParam({ anyOf: [{ type: 'null' }] }))).toThrow(/only type is null/)
  })

  it('reads a params model with no properties and no required list as simply having none', () => {
    const empty = {
      $defs: {
        Setup: { discriminator: { propertyName: 'type', mapping: { bare: '#/$defs/BareSetup' } } },
        BareSetup: { properties: { params: { $ref: '#/$defs/Empty' } } },
        Empty: {},
      },
    }
    expect(readSetups(empty)).toEqual([{ type: 'bare', params: [] }])
  })

  it('reads a bare number with no bounds and no default', () => {
    expect(readSetups(withParam({ type: 'number' }))[0]?.params[0]).toEqual({
      name: 'knob',
      kind: 'number',
      required: false,
      default: null,
      nullable: false,
    })
  })

  it('takes an exclusive minimum as the lower bound when there is no inclusive one', () => {
    expect(readSetups(withParam({ type: 'number', exclusiveMinimum: 0 }))[0]?.params[0]).toMatchObject(
      { min: 0 },
    )
  })

  it('treats a non-string default on an enum as no default at all', () => {
    // Better an unanswered field than one pre-filled with a value the select cannot display.
    const node = { $ref: '#/$defs/Kind', default: 7 }
    const withEnum = withParam(node, { Kind: { enum: ['a', 'b'], type: 'string' } })
    expect(readSetups(withEnum)[0]?.params[0]).toMatchObject({ kind: 'enum', default: null })
  })
})
