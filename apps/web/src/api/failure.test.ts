import { ApiError } from './client'
import { apiFailure } from './failure'

const FALLBACK = 'The request was refused.'

describe('apiFailure', () => {
  it("shows the server's sentence, not the status it arrived with", () => {
    // ⚠️ `ApiError.message` is built from the status alone — "API error 409" — which is the one
    // thing a reader cannot act on.
    const refused = new ApiError(409, 'a strategy with this name and version already exists')

    expect(apiFailure(refused, FALLBACK)).toBe(
      'a strategy with this name and version already exists',
    )
  })

  it("reads the DSL's meaning check, whose reason is a string and not a list", () => {
    // ⚠️ This is the shape that was being dropped, and the one a strategy meets most often: a
    // `SemanticValidationError` joins its reasons into one line, so `errors` is a **string**
    // where the shape check sends a list. Reading only lists left the reader with "strategy is
    // well-formed but cannot run" and no field named — the refusal that was actually met.
    const refused = new ApiError(422, {
      message: 'strategy is well-formed but cannot run',
      errors:
        "setup.params.htf_offset: a higher timeframe needs the broker's clock: give htf_offset, " +
        "the hours its server runs ahead of UTC (the collector's --server-offset)",
    })

    expect(apiFailure(refused, FALLBACK)).toBe(
      'strategy is well-formed but cannot run: setup.params.htf_offset: a higher timeframe ' +
        "needs the broker's clock: give htf_offset, the hours its server runs ahead of UTC " +
        "(the collector's --server-offset)",
    )
  })

  it("reads the shape check's structured list, which is the other shape a refusal takes", () => {
    const refused = new ApiError(422, {
      message: 'strategy failed schema validation',
      errors: [
        {
          loc: ['setup', 'mme9_breakout', 'params', 'breakeven_at_r'],
          msg: 'Input should be greater than 0',
        },
      ],
    })

    expect(apiFailure(refused, FALLBACK)).toBe(
      'strategy failed schema validation: breakeven_at_r input should be greater than 0',
    )
  })

  it('says the message alone when the reason half is empty', () => {
    // An empty `errors` is not a reason, and appending it would leave a colon with nothing after
    // it — a sentence that looks truncated and reads like a bug in the screen.
    const refused = new ApiError(422, { message: 'strategy is well-formed but cannot run', errors: '' })

    expect(apiFailure(refused, FALLBACK)).toBe('strategy is well-formed but cannot run')
  })

  it('keeps a pydantic failure whose path is not a list, dropping only the field name', () => {
    // The field is read off `loc`, which is the part that can be missing; `msg` is the part that
    // always says something. Losing the whole sentence to save the prefix would be the wrong
    // half to keep.
    const refused = new ApiError(422, {
      message: 'strategy failed schema validation',
      errors: [{ msg: 'Input should be greater than 0' }],
    })

    expect(apiFailure(refused, FALLBACK)).toBe(
      'strategy failed schema validation: Input should be greater than 0',
    )
  })

  it('falls back to the sentence when the body carries no message of its own', () => {
    // A `detail` that is an object but not a refusal body — nothing here names a field or a
    // reason, so the screen has to supply both.
    expect(apiFailure(new ApiError(400, { loc: ['body'] }), FALLBACK)).toBe(
      'The request was refused. (API error 400)',
    )
  })

  it('keeps the status when the body says nothing readable, but behind the sentence', () => {
    // A 500 from a proxy has no `detail`. The status is then genuinely all that is known, so it
    // is kept — in brackets, rather than standing alone as the whole explanation.
    expect(apiFailure(new ApiError(502, null), FALLBACK)).toBe(
      'The request was refused. (API error 502)',
    )
  })

  it('keeps the message of an error that is not the API refusing', () => {
    // The network dying is not a validation problem, and its own message is the only thing that
    // knows what happened. Swallowing it would send a reader to fix a form that was fine.
    expect(apiFailure(new Error('Failed to fetch'), FALLBACK)).toBe('Failed to fetch')
  })

  it('falls back for a shape it does not recognise', () => {
    expect(apiFailure({ weird: true }, FALLBACK)).toBe(FALLBACK)
  })
})
