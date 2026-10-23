import { render, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { Snapshot } from '../api/types'
import { CURVE_COLORS } from '../backtest/price'
import { TradeSnapshot } from './TradeSnapshot'

/**
 * The picture of one entry, and what it has to tell apart.
 *
 * A filtered run draws two of everything: the setup's average and the long one the filter reads,
 * the zone the order rests in and the region above that released the side at all. Painted alike
 * — which is what this component did until the filters arrived — the picture answers neither of
 * the two questions a reader opens it with.
 */

const BARS: Snapshot['bars'] = [
  { time: '2023-12-31T23:00:00Z', open: '100', high: '102', low: '99', close: '101' },
  { time: '2024-01-01T00:00:00Z', open: '101', high: '103', low: '100', close: '102' },
  { time: '2024-01-01T01:00:00Z', open: '102', high: '104', low: '101', close: '103' },
]

function snapshot(over: Partial<Snapshot> = {}): Snapshot {
  return {
    decided_at: '2023-12-31T23:00:00Z',
    filled_at: '2024-01-01T00:00:00Z',
    bars: BARS,
    regions: [],
    series: [],
    levels: [],
    ...over,
  }
}

function draw(over: Partial<Snapshot> = {}): ReturnType<typeof render> {
  return render(
    <TradeSnapshot
      snapshot={snapshot(over)}
      entryPrice="102"
      stopLoss="99"
      context={{ average: '101' }}
    />,
  )
}

/**
 * ⚠️ **The labels are the ones production emits, and they are not the chart's.** `swing.py` names
 * the setup's own trail `average` in a snapshot and `EMA 9` on the run's chart, while the
 * filter's curve is `long EMA N` in both. So the caption here reads "average", which is the
 * engine's word for "the average this setup is defined by" and not a good one to show a reader —
 * recorded in `specs/backlog.md`, and an engine change rather than a drawing one.
 *
 * Using the chart's labels in this fixture would have hidden that: the picture would look right
 * in the test and read `average` on the screen.
 */
const series = (label: string): Snapshot['series'][number] => ({
  label,
  points: BARS.map((bar, index) => [bar.time, String(100 + index)] as [string, string]),
})

const region = (label: string): Snapshot['regions'][number] => ({
  label,
  top: label === 'htf' ? '104' : '102',
  bottom: label === 'htf' ? '99' : '101',
  from_time: BARS[0]?.time ?? '',
})

function polylines(container: HTMLElement): SVGPolylineElement[] {
  return [...container.querySelectorAll('polyline')]
}

/** The captions the legend wrote, told apart from the price labels that share the tag. */
function captions(container: HTMLElement, among: readonly string[]): string[] {
  return [...container.querySelectorAll('svg text')]
    .map((node) => node.textContent)
    .filter((text) => among.includes(text))
}

describe('TradeSnapshot — the curves', () => {
  it('gives each curve its own hue, so two averages are two lines', () => {
    // ⚠️ Until the long-average filter arrived there was only ever one curve here, so one colour
    // was enough and the component hard-coded it. With the filter on there are two, and painted
    // in one hue the picture shows a line crossing itself.
    const view = draw({ series: [series('average'), series('long EMA 200')] })

    const strokes = polylines(view.container).map((line) => line.getAttribute('stroke'))
    expect(new Set(strokes).size).toBe(2)
    expect(strokes).toEqual([CURVE_COLORS[0], CURVE_COLORS[1]])
  })

  it('takes the hues and the strokes from the same place the price chart does', () => {
    // A reader moves between the run's chart and one trade's picture expecting the MME9 to be
    // the same colour in both. `curveStyles` is the one function that decides.
    //
    // ⚠️ **Asserted on the second curve, never on the first one's colour.** A single curve comes
    // back in `CURVE_COLORS[0]`, which is the very hue the old hard-coded `LEVEL.average` used —
    // so a one-curve fixture passes against the code this replaced and proves nothing.
    const view = draw({ series: [series('average'), series('long EMA 200')] })

    const [first, second] = polylines(view.container)
    expect(first?.getAttribute('stroke')).toBe(CURVE_COLORS[0])
    expect(second?.getAttribute('stroke')).toBe(CURVE_COLORS[1])
  })

  it('tells two readings of one indicator apart by the line, not by the hue', () => {
    // ⚠️ The stroke only varies *within* a family — `average` and `long EMA 200` are two
    // indicators and both come back solid, in different hues. It takes a band to reach the other
    // half of `curveStyles`, and without this the snapshot's dash table is never read at all.
    const view = draw({ series: [series('bb.upper'), series('bb.lower')] })

    const [first, second] = polylines(view.container)
    expect(first?.getAttribute('stroke')).toBe(second?.getAttribute('stroke'))
    expect(first?.getAttribute('stroke-dasharray')).toBeNull()
    expect(second?.getAttribute('stroke-dasharray')).toBe('6 3')
  })

  it('draws no line at all for a curve the palette cannot name', () => {
    // ⚠️ `curveStyles` drops a fourth indicator rather than repeating a hue, and the snapshot has
    // to drop it too: the obvious fallback is `LEVEL.average`, which *is* `CURVE_COLORS[0]` — the
    // recycling the palette refuses, arriving through the back door.
    const view = draw({ series: [series('a'), series('b'), series('c'), series('d')] })

    expect(polylines(view.container)).toHaveLength(3)
    expect(captions(view.container, ['a', 'b', 'c', 'd'])).toEqual(['a', 'b', 'c'])
  })

  it('names both curves when there are two', () => {
    const view = draw({ series: [series('average'), series('long EMA 200')] })

    expect(within(view.container).getByText('average')).toBeInTheDocument()
    expect(within(view.container).getByText('long EMA 200')).toBeInTheDocument()
  })

  it('says nothing when there is only one curve to name', () => {
    // With a single average the picture already says which it is by being the only line, and a
    // caption over the candles would cost more than it explains.
    const view = draw({ series: [series('average')] })

    expect(within(view.container).queryByText('average')).not.toBeInTheDocument()
  })

  it('groups an indicator with itself in the caption, whatever order it arrived in', () => {
    // The same regrouping `toCurves` needed on the run's chart: the caption walks the styles, not
    // the snapshot, so two readings of one indicator are named together.
    const view = draw({
      series: [series('bb.upper'), series('long EMA 200'), series('bb.lower')],
    })

    const labels = ['bb.upper', 'bb.lower', 'long EMA 200']
    expect(captions(view.container, labels)).toEqual(['bb.upper', 'bb.lower', 'long EMA 200'])
  })
})

describe('TradeSnapshot — the regions', () => {
  it('names the region that came from the timeframe above', () => {
    const view = draw({ regions: [region('zone'), region('htf')] })

    expect(within(view.container).getByText('região de cima')).toBeInTheDocument()
  })

  it('leaves the region above unfilled, so the zone inside it stays readable', () => {
    // ⚠️ The two overlap by construction — the small zone sits inside the big band — and two
    // translucent fills over each other make a third shade belonging to neither.
    const both = draw({ regions: [region('zone'), region('htf')] })

    const filled = [...both.container.querySelectorAll('rect')].filter(
      (rect) => rect.getAttribute('fill') !== 'none' && rect.getAttribute('opacity') === '0.09',
    )
    expect(filled).toHaveLength(1)
  })

  it('draws the region above first, so the zone the order rests in sits over it', () => {
    // The document order is the paint order in SVG, and the band that released the entry is
    // background to the zone that took it — never the other way round.
    const view = draw({ regions: [region('zone'), region('htf')] })

    const groups = [...view.container.querySelectorAll('svg > g')]
    const drawn = groups.filter((group) => group.querySelector('rect') !== null)
    expect(drawn[0]?.querySelector('text')?.textContent).toBe('região de cima')
  })

  it('draws the region above with a heavier outline than the zone inside it', () => {
    // Not the encoding — the word is — but not free either: unasserted, a refactor could take it
    // away and the two bands would differ only by a caption the eye finds last.
    const view = draw({ regions: [region('zone'), region('htf')] })

    const outlines = [...view.container.querySelectorAll('rect')].filter(
      (rect) => rect.getAttribute('fill') === 'none',
    )
    expect(outlines.map((rect) => rect.getAttribute('stroke-width'))).toEqual(['1.75', '1'])
  })

  it('marks the region above when it began before the window', () => {
    // ⚠️ The usual case, not the exception: a four-hour region almost always starts before a
    // window of a few base bars. The arrow rides the caption it already has rather than adding a
    // second line that would appear on nearly every filtered trade.
    const early = { ...region('htf'), from_time: '2020-01-01T00:00:00Z' }
    const view = draw({ regions: [early] })

    expect(within(view.container).getByText(/região de cima ←/)).toBeInTheDocument()
  })

  it('leaves the arrow off a region above that starts on screen', () => {
    const view = draw({ regions: [region('htf')] })

    expect(within(view.container).getByText('região de cima')).toBeInTheDocument()
    expect(within(view.container).queryByText(/←/)).not.toBeInTheDocument()
  })

  it('says nothing about a region above when the run had no filter', () => {
    const view = draw({ regions: [region('zone')] })

    expect(within(view.container).queryByText('região de cima')).not.toBeInTheDocument()
  })
})
