import { fireEvent, render, screen } from '@testing-library/react'
import { useState } from 'react'
import { describe, expect, it } from 'vitest'

import { axesFor, type AxisOption } from '../study/axes'

import { AxisValues } from './AxisValues'

const LABEL = 'Parameter 1 values'

/** The real option for a real parameter — nothing in this file writes a parameter down. */
function option(setup: string, name: string): AxisOption {
  const found = axesFor(setup).find((each) => each.path === `setup.params.${name}`)
  if (found === undefined) throw new Error(`${setup} has no axis ${name}`)
  return found
}

/** Renders the control holding its own line, and reports what the line says. */
function show(chosen: AxisOption | undefined, initial = ''): { line: () => string } {
  let latest = initial
  function Harness(): React.JSX.Element {
    const [raw, setRaw] = useState(initial)
    latest = raw
    return (
      <AxisValues
        option={chosen}
        raw={raw}
        label={LABEL}
        onChange={(next) => {
          setRaw(next)
        }}
      />
    )
  }
  render(<Harness />)
  return { line: () => latest }
}

describe('an axis over a set of names', () => {
  it('offers exactly the values the schema declares, and nothing written here', () => {
    // ⚠️ `side` is `long | short` because the Pydantic model says so. A hand-written pair here
    // would be a second copy of the DSL, and the day a third side exists this test would still
    // pass while the form offered two.
    const side = option('mme9_breakout', 'side')
    show(side)

    expect(side.param.kind).toBe('enum')
    expect(screen.getAllByRole('checkbox').map((box) => box.getAttribute('aria-label'))).toEqual([
      `${LABEL} long`,
      `${LABEL} short`,
    ])
  })

  it('writes the axis in the order the schema declares, not the order they were ticked', () => {
    // The axis order is the order a heatmap lays its cells out. Ticking `short` first and
    // getting `short, long` would draw a chart whose columns are in click order.
    const { line } = show(option('mme9_breakout', 'side'))

    fireEvent.click(screen.getByLabelText(`${LABEL} short`))
    fireEvent.click(screen.getByLabelText(`${LABEL} long`))

    expect(line()).toBe('long, short')
  })

  it('unticks back off the axis', () => {
    const { line } = show(option('mme9_breakout', 'side'), 'long, short')

    fireEvent.click(screen.getByLabelText(`${LABEL} long`))

    expect(line()).toBe('short')
  })

  it('offers a flag as its two values rather than as one box that is either ticked or not', () => {
    // ⚠️ A grid over `allow_secondary` searches **both** settings; a single checkbox would mean
    // "vary it or don't", which is one run either way and not a grid at all.
    show(option('structure_choch', 'allow_secondary'))

    expect(screen.getAllByRole('checkbox').map((box) => box.getAttribute('aria-label'))).toEqual([
      `${LABEL} true`,
      `${LABEL} false`,
    ])
  })
})

describe('an axis over a number', () => {
  it('adds the value in the box and shows it as a removable tag', () => {
    const { line } = show(option('mme9_breakout', 'period'))

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '14' } })
    fireEvent.click(screen.getByLabelText(`${LABEL} add`))

    expect(line()).toBe('14')

    fireEvent.click(screen.getByLabelText(`${LABEL} remove 14`))

    expect(line()).toBe('')
  })

  it('keeps the axis in ascending order however it was filled in', () => {
    const { line } = show(option('mme9_breakout', 'period'), '20')

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '5' } })
    fireEvent.click(screen.getByLabelText(`${LABEL} add`))

    expect(line()).toBe('5, 20')
  })

  it('takes a whole list at once, which is what a ten-value axis really is', () => {
    // ⚠️ Measured against his own study: the fifty-point grid varied `period` over 2..11 — ten
    // values on one axis. Ten presses of an arrow is a form that gets abandoned, so the box
    // parses a pasted list with the very same `parseValues` the request is built with.
    const { line } = show(option('mme9_breakout', 'period'))

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '2,3,4,5,6,7,8,9,10,11' } })
    fireEvent.click(screen.getByLabelText(`${LABEL} add`))

    expect(line()).toBe('2, 3, 4, 5, 6, 7, 8, 9, 10, 11')
  })

  it('will not add a value the axis already has', () => {
    // Two identical points are two identical backtests, which the server refuses — the form is
    // where that is prevented rather than reported.
    show(option('mme9_breakout', 'period'), '9')

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '9' } })

    expect(screen.getByLabelText(`${LABEL} add`)).toBeDisabled()
  })

  it('steps by the parameter own step, so a tenth does not become one and a tenth', () => {
    // The failure this prevents was a real suggestion once: `stop_buffer: 0.1, 1.1`.
    const { line } = show(option('structure_choch', 'stop_buffer'))

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '0.1' } })
    fireEvent.click(screen.getByLabelText(`${LABEL} up`))
    fireEvent.click(screen.getByLabelText(`${LABEL} add`))

    expect(line()).toBe('0.15')
  })

  it('refuses to step onto a bound the API would reject', () => {
    // `breakeven_at_r` is `> 0`. The arrow is disabled rather than clamping to zero, because a
    // clamp answers a press with the one value in range that cannot be launched.
    show(option('mme9_breakout', 'breakeven_at_r'))

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '0.5' } })

    expect(screen.getByLabelText(`${LABEL} down`)).toBeDisabled()
    expect(screen.getByLabelText(`${LABEL} up`)).toBeEnabled()
  })

  it('will not move an arrow while the box holds a list', () => {
    // An arrow moves a value. Stepping here would replace what the reader pasted with one
    // number, which is an edit to a field they can see all of.
    show(option('mme9_breakout', 'period'))

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '5, 9' } })

    expect(screen.getByLabelText(`${LABEL} up`)).toBeDisabled()
    expect(screen.getByLabelText(`${LABEL} down`)).toBeDisabled()
  })

  it('offers the generated example one value at a time', () => {
    // Every suggestion is built around the parameter default and clamped to its bounds, so each
    // one is a value that will run — and each disappears once it is on the axis.
    const { line } = show(option('mme9_breakout', 'period'))

    fireEvent.click(screen.getByLabelText(`${LABEL} suggest 9`))

    expect(line()).toBe('9')
    expect(screen.queryByLabelText(`${LABEL} suggest 9`)).not.toBeInTheDocument()
  })
})

describe('an axis no setup describes', () => {
  it('stays a plain field, because a DSL strategy varies paths this dropdown cannot name', () => {
    // `indicators.0.params.period` belongs to no setup. Refusing to render anything would make
    // those strategies unstudiable; the server is the authority on whether a path leads
    // anywhere.
    const { line } = show(undefined)

    fireEvent.change(screen.getByLabelText(LABEL), { target: { value: '5, 9, 20' } })

    expect(line()).toBe('5, 9, 20')
  })
})
