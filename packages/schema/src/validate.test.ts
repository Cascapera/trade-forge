import { readdirSync, readFileSync } from 'node:fs'
import { dirname, join } from 'node:path'
import { fileURLToPath } from 'node:url'

import { toFailures, validateStrategy } from './validate.js'

const FIXTURES = join(dirname(fileURLToPath(import.meta.url)), '..', 'fixtures')

function fixtures(group: string): { name: string; document: unknown }[] {
  const directory = join(FIXTURES, group)
  return readdirSync(directory)
    .filter((name) => name.endsWith('.json'))
    .map((name) => ({
      name,
      document: JSON.parse(readFileSync(join(directory, name), 'utf-8')) as unknown,
    }))
}

describe('validateStrategy', () => {
  it.each(fixtures('valid'))('accepts $name', ({ document }) => {
    const result = validateStrategy(document)

    expect(result.valid).toBe(true)
  })

  it.each(fixtures('invalid-schema'))('rejects $name', ({ document }) => {
    const result = validateStrategy(document)

    expect(result.valid).toBe(false)
    if (!result.valid) {
      expect(result.errors.length).toBeGreaterThan(0)
    }
  })

  // This is not a gap in the tests — it *is* the test. These documents are perfectly
  // well-formed and completely unrunnable: a ref to an indicator nobody declared, a
  // 2:1 target with no stop to measure the 1 against. The schema accepts them because
  // a schema describes shape, and none of those faults are shape.
  //
  // Asserting the acceptance pins the boundary in place. The frontend validates for
  // fast feedback; the API decides what may run.
  it.each(fixtures('invalid-semantic'))(
    'accepts $name — schema-valid, yet unrunnable (the Python semantic layer catches this)',
    ({ document }) => {
      const result = validateStrategy(document)

      expect(result.valid).toBe(true)
    },
  )

  it('reports the path of the offending field', () => {
    const result = validateStrategy({ schema_version: '1.0' })

    expect(result.valid).toBe(false)
    if (!result.valid) {
      expect(result.errors.some((error) => error.path === '(root)')).toBe(true)
    }
  })

  it('rejects a document that is not an object at all', () => {
    expect(validateStrategy('a strategy, honest').valid).toBe(false)
  })
})

describe('toFailures', () => {
  it('treats a missing error list as no errors', () => {
    expect(toFailures(null)).toEqual([])
    expect(toFailures(undefined)).toEqual([])
  })

  it('falls back to a generic message when ajv omits one', () => {
    const nameless = { instancePath: '/risk', keyword: 'type', schemaPath: '#/type', params: {} }

    expect(toFailures([nameless])).toEqual([{ path: '/risk', message: 'invalid' }])
  })
})
