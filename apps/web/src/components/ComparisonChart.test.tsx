import { act, render, screen } from '@testing-library/react'

import type { ComparisonSeries } from '../backtest/compare'

// lightweight-charts draws to a real canvas, which jsdom does not provide. The library is mocked
// and the test asserts what this component is responsible for: one line per run, each in its own
// colour, direct labels only while they still fit, and a clean teardown.
const { createChart, addSeries, setData, createPriceLine, subscribe, unsubscribe, remove, handles } =
  vi.hoisted(() => {
    const setData = vi.fn()
    const createPriceLine = vi.fn()
    // Every call returns a *distinct* handle, not one shared object. The component looks each
    // series up in the crosshair payload's own map, keyed by handle identity — one shared object
    // would collapse six lines into one and the hover test below would pass for the wrong reason.
    // Each handle carries the options it was created with, so the assertions read them off the
    // handle rather than indexing into `mock.calls` — same facts, without the tuple gymnastics.
    const handles: { setData: unknown; createPriceLine: unknown; options: Record<string, unknown> }[] =
      []
    const addSeries = vi.fn((kind: string, options: Record<string, unknown>) => {
      const handle = { setData, createPriceLine, kind, options }
      handles.push(handle)
      return handle
    })
    const subscribe = vi.fn()
    const unsubscribe = vi.fn()
    const remove = vi.fn()
    const createChart = vi.fn(() => ({
      addSeries,
      timeScale: () => ({ fitContent: vi.fn() }),
      subscribeCrosshairMove: subscribe,
      unsubscribeCrosshairMove: unsubscribe,
      remove,
    }))
    return {
      createChart,
      addSeries,
      setData,
      createPriceLine,
      subscribe,
      unsubscribe,
      remove,
      handles,
    }
  })

vi.mock('lightweight-charts', () => ({
  createChart,
  LineSeries: 'LineSeries',
  ColorType: { Solid: 'solid' },
  LineStyle: { Dashed: 2 },
}))

import { ComparisonChart } from './ComparisonChart'

function series(over: Partial<ComparisonSeries>): ComparisonSeries {
  return {
    id: 'a',
    label: 'AAPL H1 · Ponto Contínuo v1',
    color: '#3987e5',
    points: [
      { time: 1_722_517_200, value: 0 },
      { time: 1_722_520_800, value: 12.5 },
    ],
    ...over,
  }
}

afterEach(() => {
  vi.clearAllMocks()
  handles.length = 0
})

/** Drive the crosshair the way lightweight-charts would, and let React settle. */
function moveCrosshairTo(reading: Record<number, number> | null): void {
  const handler = subscribe.mock.calls[0]?.[0] as (param: unknown) => void
  act(() => {
    handler(
      reading === null
        ? { time: undefined, seriesData: new Map() }
        : {
            time: 1_722_520_800,
            seriesData: new Map(
              Object.entries(reading).map(([index, value]) => [handles[Number(index)], { value }]),
            ),
          },
    )
  })
}

describe('ComparisonChart', () => {
  it('invites a pick instead of drawing an empty chart', () => {
    render(<ComparisonChart series={[]} />)
    expect(screen.getByText(/pick a run/i)).toBeInTheDocument()
    expect(createChart).not.toHaveBeenCalled()
  })

  it('draws one line per run, each in its own colour', () => {
    render(
      <ComparisonChart
        series={[series({}), series({ id: 'b', label: 'EURUSD M15 · x v1', color: '#d95926' })]}
      />,
    )

    expect(createChart).toHaveBeenCalledTimes(1)
    expect(addSeries).toHaveBeenCalledTimes(2)
    expect(addSeries).toHaveBeenNthCalledWith(
      1,
      'LineSeries',
      expect.objectContaining({ color: '#3987e5', lineWidth: 2 }),
    )
    expect(addSeries).toHaveBeenNthCalledWith(
      2,
      'LineSeries',
      expect.objectContaining({ color: '#d95926' }),
    )
    expect(setData).toHaveBeenCalledWith([
      { time: 1_722_517_200, value: 0 },
      { time: 1_722_520_800, value: 12.5 },
    ])
  })

  it('marks break-even once, not once per line', () => {
    render(<ComparisonChart series={[series({}), series({ id: 'b' })]} />)
    expect(createPriceLine).toHaveBeenCalledTimes(1)
    expect(createPriceLine).toHaveBeenCalledWith(expect.objectContaining({ price: 0 }))
  })

  it('direct-labels the lines while four still fit', () => {
    render(
      <ComparisonChart
        series={[series({ id: 'a' }), series({ id: 'b' }), series({ id: 'c' }), series({ id: 'd' })]}
      />,
    )
    for (const handle of handles) {
      expect(handle.options).toMatchObject({ lastValueVisible: true })
    }
  })

  it('drops the direct labels once they would collide', () => {
    // The fifth line puts five badges on one price scale. The legend carries identity from here.
    render(
      <ComparisonChart
        series={['a', 'b', 'c', 'd', 'e'].map((id) => series({ id }))}
      />,
    )
    for (const handle of handles) {
      expect(handle.options).toMatchObject({ lastValueVisible: false })
    }
  })

  it('legends every line by name, with its total return', () => {
    // Identity is never colour alone — a name in ink beside the swatch. The number with no
    // pointer on the chart is the line's last value, which is what the run finished at.
    render(<ComparisonChart series={[series({})]} />)

    expect(screen.getByText('AAPL H1 · Ponto Contínuo v1')).toBeInTheDocument()
    expect(screen.getByText('+12.5%')).toBeInTheDocument()
  })

  it('shows a loss in the legend with a minus, not a bare number', () => {
    render(<ComparisonChart series={[series({ points: [{ time: 1, value: -10.2 }] })]} />)
    expect(screen.getByText('−10.2%')).toBeInTheDocument()
  })

  it('reads every line at the pointer, not just the one under it', () => {
    // The whole reason the legend doubles as the tooltip: one pointer position answers "where was
    // each of these runs on that date", which is the comparison being made. A per-line tooltip
    // would answer it one line at a time.
    render(
      <ComparisonChart
        series={[
          series({ id: 'a', label: 'AAPL H1' }),
          series({ id: 'b', label: 'EURUSD M15', color: '#d95926' }),
        ]}
      />,
    )

    moveCrosshairTo({ 0: 4.25, 1: -3.5 })

    expect(screen.getByText('+4.3%')).toBeInTheDocument()
    expect(screen.getByText('−3.5%')).toBeInTheDocument()
  })

  it('falls back to each run’s final return when the pointer leaves', () => {
    render(<ComparisonChart series={[series({})]} />)

    moveCrosshairTo({ 0: 4.25 })
    expect(screen.getByText('+4.3%')).toBeInTheDocument()

    moveCrosshairTo(null)
    expect(screen.getByText('+12.5%')).toBeInTheDocument()
  })

  it('dashes a line the pointer is past the end of', () => {
    // Two runs over different windows is the normal case here, so at most dates one line has a
    // value and the other has none. A dash says "this run was not running yet"; carrying the
    // stale last value would read as a flat stretch that never happened.
    render(
      <ComparisonChart
        series={[
          series({ id: 'a', label: 'AAPL H1' }),
          series({ id: 'b', label: 'EURUSD M15', color: '#d95926', points: [] }),
        ]}
      />,
    )

    moveCrosshairTo({ 0: 4.25 })

    expect(screen.getByText('+4.3%')).toBeInTheDocument()
    expect(screen.getByText('—')).toBeInTheDocument()
  })

  it('unsubscribes and removes the chart on unmount', () => {
    const view = render(<ComparisonChart series={[series({})]} />)
    view.unmount()
    expect(unsubscribe).toHaveBeenCalled()
    expect(remove).toHaveBeenCalled()
    expect(subscribe).toHaveBeenCalled()
  })
})
