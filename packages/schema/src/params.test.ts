import { describe, expect, it } from 'vitest'

import { describeParam, offIsASetting, type SchemaNode } from './params.js'

// ⚠️ **Hand-made nodes, and that is the point of this file.** `setups.test.ts` reads the real
// generated schema, which is what proves the DSL's own parameters arrive intact — but every one
// of them has a name, and a reader deciding by name passes every assertion there. Measured: with
// `offIsASetting` rewritten as `param.nullable && param.name !== 'htf_offset'`, all 878 tests in
// the repository stayed green. A parameter called `whatever` is what tells the two apart.
//
// It also reaches the branches the schema has no example of. `requiredWith` on an *enum* is
// plumbing today with no parameter behind it, and a line nothing can reach is a guess about what
// the generator emits rather than a guarantee.

/** No node here uses `$ref`, so following one is a defect in the test rather than a case to pass. */
const resolve = (ref: string): SchemaNode => {
  throw new Error(`this test declares no definitions, and something asked for ${ref}`)
}

function numeric(extra: Partial<SchemaNode> = {}): SchemaNode {
  return { anyOf: [{ type: 'number' }, { type: 'null' }], default: null, ...extra }
}

function names(extra: Partial<SchemaNode> = {}): SchemaNode {
  return { anyOf: [{ type: 'string', enum: ['one', 'two'] }, { type: 'null' }], ...extra }
}

describe('requiredWith, read off a node rather than off a name', () => {
  it('carries the key on a number whose name it has never heard of', () => {
    const param = describeParam('whatever', numeric({ requiredWith: 'companion' }), false, resolve)

    expect(param).toMatchObject({ kind: 'number', nullable: true, requiredWith: 'companion' })
  })

  it('carries the key on a set of names too, which the DSL has no example of yet', () => {
    // The enum branch of `describeParam`. Deleting its spread leaves every other test in the
    // repository green, because no enum in the schema declares the key — so this is the only
    // thing standing between that line and a silent no-op the day one does.
    const param = describeParam('whatever', names({ requiredWith: 'companion' }), false, resolve)

    expect(param).toMatchObject({ kind: 'enum', nullable: true, requiredWith: 'companion' })
  })

  it('leaves the key off entirely when the node does not declare it', () => {
    // Absent, not present-and-undefined: `toHaveProperty` is what tells those apart, and a
    // consumer spreading the param into an object would carry the second one along.
    expect(describeParam('whatever', numeric(), false, resolve)).not.toHaveProperty('requiredWith')
    expect(describeParam('whatever', names(), false, resolve)).not.toHaveProperty('requiredWith')
  })

  it('reads the key from the outer node, where Pydantic hangs it, not from the branch', () => {
    // ⚠️ `float | None` is emitted as an `anyOf`, and the field's own keywords sit *beside* it.
    // A reader looking inside the non-null branch finds nothing and reports nothing — no error,
    // no key, and every form back to treating this `null` as a value somebody may choose.
    const inside: SchemaNode = {
      anyOf: [{ type: 'number', requiredWith: 'companion' }, { type: 'null' }],
    }

    expect(describeParam('whatever', inside, false, resolve)).not.toHaveProperty('requiredWith')
  })
})

describe('offIsASetting', () => {
  it('says no for a nullable that another parameter requires, whatever it is called', () => {
    const param = describeParam('whatever', numeric({ requiredWith: 'companion' }), false, resolve)

    expect(offIsASetting(param)).toBe(false)
  })

  it('says yes for a nullable that nothing requires, whatever it is called', () => {
    // The mirror, and it is what stops the answer from being "no" to everything: a rule that
    // never offers `off` passes the assertion above on its own.
    expect(offIsASetting(describeParam('whatever', numeric(), false, resolve))).toBe(true)
    expect(offIsASetting(describeParam('whatever', names(), false, resolve))).toBe(true)
  })

  it('says no for a parameter that is not nullable at all', () => {
    // Its absence is a forgotten answer rather than a rule switched off, and the two are
    // different sentences on screen.
    const param = describeParam('whatever', { type: 'number', minimum: 1 }, true, resolve)

    expect(offIsASetting(param)).toBe(false)
  })

  it('says no for a flag, which is drawn as its two boxes with no third', () => {
    const param = describeParam('whatever', { type: 'boolean' }, false, resolve)

    expect(offIsASetting(param)).toBe(false)
  })
})
