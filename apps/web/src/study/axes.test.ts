import { describe, expect, it } from 'vitest'

import { axesFor } from './axes'

describe('axesFor', () => {
  it('offers the parameters the chosen setup actually has', () => {
    // ⚠️ Asserted against the **DSL's own** parameter names, which is the point of deriving
    // them: this list is generated from the JSON Schema, which is generated from the Pydantic
    // models. If someone adds a parameter in Python this test starts failing here — which is
    // the failure you want, rather than a dropdown that quietly stays a version behind.
    const paths = axesFor('mme9_breakout').map((axis) => axis.path)

    // Required first — `side` has no default, so it is the one that must be answered — and the
    // schema's own order after that.
    expect(paths).toEqual([
      'setup.params.side',
      'setup.params.breakeven_at_r',
      'setup.params.period',
      'setup.params.stop_buffer_ticks',
    ])
  })

  it('says what is legal for a bounded number, in its own bounds', () => {
    const [period] = axesFor('mme9_breakout').filter(
      (axis) => axis.path === 'setup.params.period',
    )

    expect(period?.hint).toBe('whole numbers between 1 and 1000, separated by commas')
  })

  it('lists the choices for a parameter that has them', () => {
    const [side] = axesFor('mme9_breakout').filter((axis) => axis.path === 'setup.params.side')

    expect(side?.hint).toMatch(/one or more of long, short/)
    expect(side?.example).toBe('long, short')
  })

  it('says true and false for a flag', () => {
    const [secondary] = axesFor('structure_choch').filter(
      (axis) => axis.path === 'setup.params.allow_secondary',
    )

    expect(secondary?.hint).toBe('true, false — separated by commas')
    expect(secondary?.example).toBe('true, false')
  })

  it('builds its example around the value the author chose', () => {
    // The default is what the strategy already uses, so a first study searches *around* it
    // rather than away from it — and the reader can see at a glance that the middle value is
    // the one they have been running.
    const [period] = axesFor('mme9_breakout').filter(
      (axis) => axis.path === 'setup.params.period',
    )

    expect(period?.example).toBe('4, 9, 14')
  })

  it('never suggests a value the server would refuse', () => {
    // ⚠️ `stop_buffer_ticks` defaults to 0 with a floor of 0, so the naive "default minus a
    // step" is -5 — a request the DSL rejects. An example that fails is worse than no example,
    // because the reader tries it before they trust anything else on the screen.
    const [buffer] = axesFor('mme9_breakout').filter(
      (axis) => axis.path === 'setup.params.stop_buffer_ticks',
    )

    expect(buffer?.example).toBe('0, 1')
    expect(buffer?.example).not.toMatch(/-/)
  })

  it('offers nothing for a strategy built from indicators rather than a setup', () => {
    // Those vary at `indicators.0.params.period`, a shape this list does not describe. Saying
    // nothing leaves the typed field in place; offering the wrong paths would not.
    expect(axesFor(null)).toEqual([])
  })

  it('offers nothing rather than throwing for a setup this build does not know', () => {
    // The API is the authority on what setups exist. A screen that threw here would go blank
    // over a strategy it merely could not describe — a worse outcome than one missing dropdown.
    expect(axesFor('something_the_server_knows_about')).toEqual([])
  })

  it('steps a fractional parameter by a fraction, not by one', () => {
    // ⚠️ The defect the probe caught before any test existed. `stop_buffer` is a *fraction of a
    // region's width*, defaulting to 0.1 — and a step floored at 1 suggested `0.1, 1.1`, an
    // eleven-fold jump offered as a reasonable thing to search. Flooring at 1 is right for a
    // period and absurd for a fraction, and only one of those is obvious from the code.
    const [buffer] = axesFor('structure_choch').filter(
      (axis) => axis.path === 'setup.params.stop_buffer',
    )

    expect(buffer?.example).toBe('0.05, 0.1, 0.15')
  })
})
