// What to put on the screen when the API refuses — one place, because every screen that writes
// meets the same three shapes and none of them is `ApiError.message`.
//
// ⚠️ `ApiError.message` is assembled from the status alone ("API error 422"), which is the one
// thing a reader cannot act on. The reason travels in `detail`, and the server's reasons are
// written to be acted on: *"the higher timeframe must be coarser than M15 and a whole number of
// its bars; M5 is not"*. Showing the status instead throws that away.

import { ApiError } from './client'

/**
 * The sentence the server sent, or `fallback` when it did not send one.
 *
 * Three shapes arrive, because three different things refuse:
 *
 * 1. **A sentence.** A `detail` that is a string — a 409 on a taken name, an unknown symbol.
 * 2. **`{message, errors}` with a list.** Pydantic refused the document's *shape*; `errors` is
 *    its structured failure list, one entry per field.
 * 3. **`{message, errors}` with a string.** The DSL's *meaning* check refused — `htf` without
 *    `htf_offset`, an `htf` finer than the document's own timeframe. `SemanticValidationError`
 *    joins its reasons into one line and the API forwards that line verbatim.
 *
 * ⚠️ **Shape 3 is the one that was being dropped**, and it is the shape a strategy meets most
 * often: the reader saw "strategy is well-formed but cannot run" with the part naming the field
 * cut off. Reading only the list is the easy mistake — the two shapes share a key and differ
 * only in the type behind it.
 */
export function apiFailure(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    const { detail } = error
    if (typeof detail === 'string') return detail

    if (detail !== null && typeof detail === 'object' && 'message' in detail) {
      const body = detail as { message?: unknown; errors?: unknown }
      const message = typeof body.message === 'string' ? body.message : fallback
      const because = reasonOf(body.errors)
      return because === null ? message : `${message}: ${because}`
    }

    // An `ApiError` whose `detail` says nothing readable. The status is then genuinely all that
    // is known, so it is kept — but in brackets, behind a sentence that says what failed, rather
    // than standing alone as the whole explanation.
    return `${fallback} (${error.message})`
  }

  // Not an `ApiError` at all — the network died, or the response was not JSON. Its own message
  // is the only thing that knows what happened ("Failed to fetch"), and swallowing it for a
  // house sentence would leave a reader retrying a form that was never the problem.
  if (error instanceof Error && error.message !== '') return error.message

  return fallback
}

/**
 * The `errors` half of a refusal body as one clause, whichever of its two types it is.
 *
 * A string is already the sentence and is passed through. A list is Pydantic's, and only its
 * **first** entry is read: a grid of fifty points that names one illegal value produces one
 * failure repeated fifty times, and listing them all would bury the sentence under its echoes.
 */
function reasonOf(errors: unknown): string | null {
  if (typeof errors === 'string') return errors === '' ? null : errors
  const failure: unknown = Array.isArray(errors) ? errors[0] : undefined
  if (failure === null || typeof failure !== 'object') return null
  const { loc, msg } = failure as { loc?: unknown; msg?: unknown }
  if (typeof msg !== 'string') return null
  // The last segment is the field; the ones before it are the union branch pydantic took, which
  // names an internal model and would only puzzle a reader.
  const field: unknown = Array.isArray(loc) ? loc.at(-1) : undefined
  return typeof field === 'string' ? `${field} ${msg.toLowerCase()}` : msg
}
