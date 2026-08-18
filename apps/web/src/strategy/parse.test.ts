import { validateStrategy, type Strategy } from '@tradeforge/schema'

import bollinger from '../../../../packages/schema/fixtures/valid/bollinger_breakout_with_adx_filter.json'
import channel from '../../../../packages/schema/fixtures/valid/channel_breakout_with_atr_filter.json'
import emaShort from '../../../../packages/schema/fixtures/valid/ema_short_only.json'
import maCross from '../../../../packages/schema/fixtures/valid/ma_cross_breakout.json'
import nested from '../../../../packages/schema/fixtures/valid/nested_logic.json'
import setupChoch from '../../../../packages/schema/fixtures/valid/setup_structure_choch.json'
import setupContinuation from '../../../../packages/schema/fixtures/valid/setup_structure_continuation_defaults.json'
import setupMme9 from '../../../../packages/schema/fixtures/valid/setup_mme9_breakout.json'
import setupPonto from '../../../../packages/schema/fixtures/valid/setup_ponto_continuo.json'
import { buildStrategy } from './builder'
import { formOf } from './parse'

/**
 * The corpus is the repository's own fixtures, not documents written for this test.
 *
 * ⚠️ That is the whole point. A round-trip proved against documents this test authored proves
 * that the parser agrees with the builder — which they would, having been written together, even
 * if both were wrong about the DSL. These files are the ones the schema package validates and the
 * engine runs, including one (`nested_logic`) written specifically to exercise the expression
 * tree in depth, and they were here before this parser existed.
 */
const CORPUS: readonly { name: string; document: unknown }[] = [
  { name: 'bollinger_breakout_with_adx_filter', document: bollinger },
  { name: 'channel_breakout_with_atr_filter', document: channel },
  { name: 'ema_short_only', document: emaShort },
  { name: 'ma_cross_breakout', document: maCross },
  { name: 'nested_logic', document: nested },
  { name: 'setup_mme9_breakout', document: setupMme9 },
  { name: 'setup_ponto_continuo', document: setupPonto },
  { name: 'setup_structure_choch', document: setupChoch },
  { name: 'setup_structure_continuation_defaults', document: setupContinuation },
]

/** The one fixture whose shape the builder cannot show at all. */
const REFUSED = 'nested_logic'

/**
 * The one fixture that comes back **spelled out** rather than identical, and why.
 *
 * ⚠️ `setup_structure_continuation_defaults` carries no `params` key at all — its entire purpose
 * is the path where the document leaves every parameter to the engine. The form has one empty
 * state per field and it means "not set"; on save, a nullable field that is not set is written as
 * an explicit `null`, because for those parameters empty *is* the setting ("no break-even",
 * "uncapped") and omitting them would mean something else the day a default moves.
 *
 * So the first save writes what the document implied. That is the same strategy, and it is where
 * the exactness stops — which is why the fixed-point test below exists: from the second save on,
 * nothing changes again. Listing this fixture here rather than quietly excluding it is the point;
 * a test that hid it would be hiding the one interesting case in the corpus.
 */
const SPELLED_OUT = 'setup_structure_continuation_defaults'

/**
 * The smallest legal condition document, with one entry rule swapped in.
 *
 * Hand-written rather than taken from the corpus, and only for the shapes the corpus predates.
 * Every one is run through `validateStrategy` in its own test before it is trusted — a document
 * this file invented and this file parses would otherwise prove only that it agrees with itself.
 */
function document(entry: Record<string, unknown>): Record<string, unknown> {
  return {
    schema_version: '1.0',
    name: 'hand written',
    timeframe: 'H1',
    indicators: [
      { id: 'fast', type: 'SMA', params: { period: 9, source: 'close' } },
      { id: 'slow', type: 'SMA', params: { period: 21, source: 'close' } },
    ],
    entry: { ...entry, short: null },
    exit: { stop_loss: null, take_profit: null, conditions: [] },
    risk: { sizing: { type: 'percent_risk', params: { percent: 1 } } },
  }
}

describe('reading a saved document back into the form', () => {
  it.each(CORPUS.filter((one) => one.name !== REFUSED && one.name !== SPELLED_OUT))(
    'round-trips $name with nothing added and nothing lost',
    ({ document }) => {
      const result = formOf(document as Strategy)
      if (!result.ok) throw new Error(`refused: ${result.unsupported.join('; ')}`)

      // ⚠️ Exact equality against the file, not against a normalised copy of it. That is only
      // possible because the form holds the optional scalars as text where empty means absent —
      // a fixture that omits `max_open_positions` gets one back that omits it, and one that
      // writes `1` gets `1`. Normalising first would hide precisely the class of bug this test
      // exists for: a re-save that quietly rewrites the document's shape.
      expect(buildStrategy(result.form)).toEqual(document)
    },
  )

  it('refuses the one document whose shape the builder cannot show, naming every reason', () => {
    const result = formOf(nested as unknown as Strategy)
    if (result.ok) throw new Error('nested_logic should not be representable yet')

    // A group inside a group, and a `not` — both in `entry.long`, and both named with the path
    // the reader has to go and look at.
    expect(result.unsupported).toEqual([
      'entry.long.all[1]: a group inside a group, which the builder shows one level of',
      'entry.long.all[2]: a `not`, which the builder has no control for yet',
    ])
  })

  it('names a `not` as a `not`, wherever in the tree it sits', () => {
    // ⚠️ Found by a mutant: deleting the top-level `not` guard changed nothing, because the only
    // `not` in the corpus is nested and was being reported as "a group inside a group" — true
    // about the wrong thing, and a reader sent looking for a nested group would not find one.
    const top = document({
      long: { not: { op: 'gt', left: { ref: 'fast' }, right: { value: 1 } } },
    })
    expect(validateStrategy(top).valid).toBe(true)
    const result = formOf(top as unknown as Strategy)
    expect(result.ok).toBe(false)
    if (!result.ok) {
      expect(result.unsupported).toEqual([
        'entry.long: a `not`, which the builder has no control for yet',
      ])
    }
  })

  it('reports every unsupported shape in one pass, not just the first', () => {
    // ⚠️ The alternative — stopping at the first — turns a document with three problems into
    // three attempts to find that out, and each attempt looks like a different bug.
    const result = formOf(nested as unknown as Strategy)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.unsupported.length).toBeGreaterThan(1)
  })

  it('never returns a form it could not fill completely', () => {
    // The property behind the type: `ok` and `unsupported` cannot both be meaningful, so a caller
    // has no way to treat a partial reading as a complete one.
    for (const { document } of CORPUS) {
      const result = formOf(document as Strategy)
      expect(result.ok || result.unsupported.length > 0).toBe(true)
    }
  })

  it('rebuilds a document that is still valid against the schema', () => {
    // Equality above says "the same document". This says "a legal one" — and the two are
    // different claims, because a bug that dropped a required field would break the second while
    // an equality check against an equally-broken expectation would not notice.
    for (const { name, document } of CORPUS.filter((one) => one.name !== REFUSED)) {
      const result = formOf(document as Strategy)
      if (!result.ok) throw new Error(`${name} refused`)
      const check = validateStrategy(buildStrategy(result.form))
      expect(check.valid, `${name}: ${check.valid ? '' : JSON.stringify(check.errors)}`).toBe(true)
    }
  })
})

describe('the shapes the corpus does not happen to contain', () => {
  // ⚠️ The corpus has no `between` and no `rising`/`falling` — every fixture predates them. Half
  // the parser was therefore unexercised, which a mutant proved: reading an absent `bars` as the
  // schema's `1` broke nothing. These documents are hand-written, so each one is checked against
  // the same validator the API runs before it is trusted as a document at all.

  it('round-trips a band, including a bound that is a name rather than a number', () => {
    const doc = document({
      long: {
        op: 'between',
        value: { ref: 'fast' },
        low: { value: 30 },
        high: { ref: 'slow' },
      },
    })
    expect(validateStrategy(doc).valid).toBe(true)
    const result = formOf(doc as unknown as Strategy)
    if (!result.ok) throw new Error(result.unsupported.join('; '))
    expect(buildStrategy(result.form)).toEqual(doc)
  })

  it('round-trips a trend with a window, and one that left the window to the engine', () => {
    const withBars = document({ long: { op: 'rising', of: { ref: 'fast' }, bars: 3 } })
    const without = document({ long: { op: 'falling', of: { ref: 'fast' } } })
    expect(validateStrategy(withBars).valid).toBe(true)
    expect(validateStrategy(without).valid).toBe(true)

    for (const doc of [withBars, without]) {
      const result = formOf(doc as unknown as Strategy)
      if (!result.ok) throw new Error(result.unsupported.join('; '))
      // ⚠️ The second document is the one that matters: an absent `bars` has to come back absent.
      // Reading it as the schema's `1` produces the same *behaviour* today and freezes today's
      // default into the author's document, which is a different strategy the day it moves.
      expect(buildStrategy(result.form)).toEqual(doc)
    }
  })

  it('refuses a literal where the builder can only show a name', () => {
    // `5 > fast` is well-formed DSL and the builder has never offered it. Quietly flipping it to
    // `fast < 5` would be a different rule the day the operator is not symmetric — and
    // `crosses_above` is not.
    const doc = document({ long: { op: 'gt', left: { value: 5 }, right: { ref: 'fast' } } })
    expect(validateStrategy(doc).valid).toBe(true)
    const result = formOf(doc as unknown as Strategy)
    expect(result.ok).toBe(false)
    if (!result.ok) expect(result.unsupported[0]).toMatch(/a literal \(5\)/)
  })
})

describe('the one document that comes back spelled out', () => {
  const once = () => {
    const first = formOf(setupContinuation as unknown as Strategy)
    if (!first.ok) throw new Error('refused')
    return buildStrategy(first.form)
  }

  it('writes what the document left to the engine, and nothing else', () => {
    // ⚠️ Pinned by value, not waved at. If this list ever grows, a save has started adding
    // something new to the author's document and somebody has to decide whether it should.
    const rebuilt = once() as unknown as { setup: { params: Record<string, unknown> } }
    expect(rebuilt.setup.params).toEqual({
      allow_secondary: false,
      breakeven_at_r: null,
      max_bos: null,
    })
  })

  it('is a fixed point from the second save on', () => {
    // The property that actually protects the author: opening and saving may spell a document
    // out once, and must never keep changing it. Without this, a slow drift — each save adding
    // one more key — would look exactly like the first save being harmless.
    const first = once()
    const second = formOf(first)
    if (!second.ok) throw new Error('refused on the second pass')
    expect(buildStrategy(second.form)).toEqual(first)
  })
})

describe('what the form keeps that it has no control for', () => {
  it('keeps a description it did not write', () => {
    const result = formOf(maCross as unknown as Strategy)
    if (!result.ok) throw new Error('refused')
    expect(result.form.description).toBe(maCross.description)
  })

  it('keeps a risk cap that was written, and leaves absent one that was not', () => {
    // ⚠️ Two fixtures, because one alone cannot separate "read it" from "always writes the
    // default". `ema_short_only` sets both caps to values that are *not* the schema's defaults;
    // `bollinger_breakout_with_adx_filter` omits them entirely.
    const written = formOf(emaShort as unknown as Strategy)
    const omitted = formOf(bollinger as unknown as Strategy)
    if (!written.ok || !omitted.ok) throw new Error('refused')

    expect(written.form.maxOpenPositions).toBe('2')
    expect(written.form.maxDailyLossPercent).toBe('2')
    expect(omitted.form.maxOpenPositions).toBe('')
    expect(omitted.form.maxDailyLossPercent).toBe('')
  })

  it('leaves a parameter the document never chose empty, rather than pre-filling its default', () => {
    // ⚠️ `setup_structure_continuation_defaults` carries no `params` at all — its whole purpose is
    // the path where the document leaves everything to the engine. A form that helpfully filled
    // in the schema's numbers would turn "unset" into "chosen" the moment anybody re-saved, and
    // the document would stop asking the question it was written to ask.
    const result = formOf(setupContinuation as unknown as Strategy)
    if (!result.ok) throw new Error('refused')
    expect(Object.values(result.form.setup.values).every((value) => value === '' || value === false))
      .toBe(true)
  })
})
