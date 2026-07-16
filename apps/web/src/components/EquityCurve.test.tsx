import { render } from '@testing-library/react'

import type { EquityPoint } from '../api/types'

// lightweight-charts draws to a real canvas, which jsdom does not provide — so the library is
// mocked and the test asserts the component wires the data through it (creates a chart, adds an
// area series, feeds the mapped points) and tears it down on unmount.
const { createChart, addSeries, setData, fitContent, remove } = vi.hoisted(() => {
  const setData = vi.fn()
  const fitContent = vi.fn()
  const remove = vi.fn()
  const addSeries = vi.fn(() => ({ setData }))
  const createChart = vi.fn(() => ({ addSeries, timeScale: () => ({ fitContent }), remove }))
  return { createChart, addSeries, setData, fitContent, remove }
})

vi.mock('lightweight-charts', () => ({
  createChart,
  AreaSeries: 'AreaSeries',
  ColorType: { Solid: 'solid' },
}))

import { EquityCurve } from './EquityCurve'

const points: EquityPoint[] = [
  { time: '2024-01-01T00:00:00Z', equity: '10000' },
  { time: '2024-01-01T01:00:00Z', equity: '10200' },
]

describe('EquityCurve', () => {
  afterEach(() => {
    vi.clearAllMocks()
  })

  it('creates the chart and feeds it the mapped equity series', () => {
    render(<EquityCurve points={points} />)
    expect(createChart).toHaveBeenCalledTimes(1)
    expect(addSeries).toHaveBeenCalledWith('AreaSeries', expect.objectContaining({ lineWidth: 2 }))
    expect(setData).toHaveBeenCalledWith([
      { time: Date.parse('2024-01-01T00:00:00Z') / 1000, value: 10000 },
      { time: Date.parse('2024-01-01T01:00:00Z') / 1000, value: 10200 },
    ])
    expect(fitContent).toHaveBeenCalled()
  })

  it('removes the chart on unmount', () => {
    const view = render(<EquityCurve points={points} />)
    view.unmount()
    expect(remove).toHaveBeenCalled()
  })
})
