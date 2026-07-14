import Ajv2020, { type ErrorObject, type ValidateFunction } from 'ajv/dist/2020.js'

import schema from './tradeforge_schema/strategy.schema.json' with { type: 'json' }
import type { Strategy } from './generated/strategy.js'

/**
 * The frontend's half of the contract.
 *
 * This validates *shape* — the same JSON Schema the backend generates from its
 * Pydantic models, so the builder can reject a malformed strategy while the user
 * is still typing, without a round-trip.
 *
 * It does NOT validate *meaning*. A strategy referencing an indicator that was
 * never declared, or targeting 2:1 with no stop to measure risk against, passes
 * everything here and is still unrunnable. That check lives in the Python
 * semantic layer, and the API is its authority. Never treat a document that
 * passed in the browser as executable.
 */

// `strict: false`: Pydantic emits an OpenAPI-style `discriminator` alongside `oneOf`.
// Ajv does not know that keyword, and rejecting the schema over an annotation it can
// safely ignore would be pedantry. The `oneOf` does the real work.
const ajv = new Ajv2020({ strict: false, allErrors: true })

const validator: ValidateFunction<Strategy> = ajv.compile<Strategy>(schema)

export interface ValidationFailure {
  path: string
  message: string
}

export type ValidationResult =
  | { valid: true; strategy: Strategy }
  | { valid: false; errors: ValidationFailure[] }

/**
 * Ajv types both `errors` and `message` as optional, so both need a fallback. The
 * mapping is exported so those fallbacks can be tested directly — an untested
 * defensive branch is a guess about what the library does, not a guarantee.
 */
export function toFailures(errors: ErrorObject[] | null | undefined): ValidationFailure[] {
  return (errors ?? []).map((error) => ({
    path: error.instancePath === '' ? '(root)' : error.instancePath,
    message: error.message ?? 'invalid',
  }))
}

/** Validate an unknown document against the strategy schema. */
export function validateStrategy(document: unknown): ValidationResult {
  if (validator(document)) {
    return { valid: true, strategy: document }
  }

  return { valid: false, errors: toFailures(validator.errors) }
}

export type { Strategy }
