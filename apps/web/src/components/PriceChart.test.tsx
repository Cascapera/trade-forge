import { cleanup, render, screen } from '@testing-library/react'

import type { Candle, Trade } from '../api/types'

// lightweight-charts draws to a real canvas, which jsdom does not provide. The library is mocked
// and the test asserts what this component is responsible for: the bars reaching the series, the
// second encoding on the candles, markers going through the plugin rather than a rebuild, and the
// chosen trade moving the view.
const { createChart, setData, setMarkers, setVisibleRange, fitContent, remove, options } =
  vi.hoisted(() => {
    const setData = vi.fn()
    const setMarkers = vi.fn()
    const setVisibleRange = vi.fn()
    const fitContent = vi.fn()
    const remove = vi.fn()
    const options: Record<string, unknown>[] = []
    const addSeries = vi.fn((_kind: string, given: Record<string, unknown>) => {
      options.push(given)
      return { setData }
    })
    const createChart = vi.fn(() => ({
      addSeries,
      timeScale: () => ({ fitContent, setVisibleRange }),
      remove,
    }))
    return { createChart, setData, setMarkers, setVisibleRange, fitContent, remove, options }
  })

vi.mock('lightweight-charts', () => ({
  createChart,
  createSeriesMarkers: () => ({ setMarkers }),
  CandlestickSeries: 'CandlestickSeries',
  ColorType: { Solid: 'solid' },
}))

import { PriceChart } from './PriceChart'

/** Hourly bars from 13:00 on 2024-08-01. */
function hourly(count: number): Candle[] {
  return Array.from({ length: count }, (_, i) => ({
    time: `2024-08-01T${String(13 + i).padStart(2, '0')}:00:00Z`,
    open: '226.00',
    high: '227.00',
    low: '225.00',
    close: '226.50',
  }))
}

function trade(over: Partial<Trade>): Trade {
  return {
    id: 1,
    direction: 'long',
    entry_time: '2024-08-01T14:00:00Z',
    entry_price: '226.50',
    exit_time: '2024-08-01T16:00:00Z',
    exit_price: '228.00',
    exit_reason: 'take_profit',
    volume: '10',
    stop_loss: '225.00',
    take_profit: '229.50',
    gross_pnl: '15.00',
    costs: '0',
    net_pnl: '15.00',
    r_multiple: '2.00',
    context: {},
    has_snapshot: false,
    ...over,
  }
}

function draw(over: Partial<React.ComponentProps<typeof PriceChart>> = {}) {
  return render(
    <PriceChart
      candles={hourly(6)}
      trades={[trade({})]}
      selectedTradeId={null}
      symbol="AAPL"
      timeframe="H1"
      {...over}
    />,
  )
}

afterEach(() => {
  // ⚠️ Unmount *before* clearing, explicitly. Testing Library's own cleanup is registered first
  // and so runs last, which means the previous test's teardown would call `remove` after the
  // counters had been reset — and the next test would open with a `remove` it never caused.
  cleanup()
  vi.clearAllMocks()
  options.length = 0
})

describe('PriceChart', () => {
  it('sends every bar to the series', () => {
    draw()

    expect(setData).toHaveBeenCalledTimes(1)
    expect(setData.mock.calls[0]?.[0]).toHaveLength(6)
  })

  it('paints rising candles hollow, which is the encoding colour alone cannot carry', () => {
    // Measured, not chosen: the up and down hues separate by ΔE 7.5 under deuteranopia — inside
    // the band where colour is legal only alongside a second encoding. The body of a rising bar
    // is filled with the surface behind it, so the outline is all that remains.
    draw()

    const given = options[0]
    expect(given?.upColor).not.toBe(given?.borderUpColor)
    expect(given?.downColor).toBe(given?.borderDownColor)
  })

  it('sets the markers through the plugin instead of rebuilding the chart', () => {
    // The reason the plugin exists here. Selecting a trade changes every marker's text, and
    // rebuilding the chart for that would throw away the zoom the reader had set.
    //
    // ⚠️ The bars and the trades are held in the *same* arrays across both renders, which is
    // what React Query hands the screen. Passing freshly built arrays would rebuild the chart
    // legitimately — the effect's dependency really did change — and the test would fail while
    // saying nothing about the selection path it is here to protect. Only `selectedTradeId`
    // moves, so a `selectedTradeId` wired into the chart's own effect is what this catches.
    const bars = hourly(6)
    const trades = [trade({})]
    const view = render(
      <PriceChart
        candles={bars}
        trades={trades}
        selectedTradeId={null}
        symbol="AAPL"
        timeframe="H1"
      />,
    )
    expect(createChart).toHaveBeenCalledTimes(1)

    view.rerender(
      <PriceChart
        candles={bars}
        trades={trades}
        selectedTradeId={1}
        symbol="AAPL"
        timeframe="H1"
      />,
    )

    expect(createChart).toHaveBeenCalledTimes(1)
    expect(setMarkers).toHaveBeenCalledTimes(2)
  })

  it('brings the view to the chosen trade', () => {
    draw({ selectedTradeId: 1 })

    expect(setVisibleRange).toHaveBeenCalledTimes(1)
    const range = setVisibleRange.mock.calls[0]?.[0] as { from: number; to: number }
    expect(range.from).toBeLessThanOrEqual(range.to)
  })

  it('leaves the view alone when nothing is chosen', () => {
    draw({ selectedTradeId: null })

    expect(setVisibleRange).not.toHaveBeenCalled()
    expect(fitContent).toHaveBeenCalledTimes(1)
  })

  it('does not move to a trade that is not in these bars', () => {
    // A null range means the chart stays put. Scrolling to a range assembled out of a missing
    // index would be a worse answer than not moving.
    draw({
      selectedTradeId: 1,
      trades: [trade({ entry_time: '2020-01-01T00:00:00Z', exit_time: '2020-01-01T01:00:00Z' })],
    })

    expect(setVisibleRange).not.toHaveBeenCalled()
  })

  it('says so rather than drawing an empty frame when there are no candles', () => {
    draw({ candles: [] })

    expect(screen.getByText(/no candles to chart/i)).toBeInTheDocument()
    expect(createChart).not.toHaveBeenCalled()
  })

  it('names what it is showing, for a reader who cannot see it', () => {
    draw()

    expect(
      screen.getByRole('img', { name: /AAPL H1 price, 6 candles, with 1 trades marked/i }),
    ).toBeInTheDocument()
  })

  it('tears the chart down when it goes away', () => {
    draw().unmount()

    expect(remove).toHaveBeenCalledTimes(1)
  })
})
