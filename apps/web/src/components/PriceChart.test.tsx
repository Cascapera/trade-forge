import { cleanup, render, screen } from '@testing-library/react'

import type { Candle, Trade, Zone } from '../api/types'

// lightweight-charts draws to a real canvas, which jsdom does not provide. The library is mocked
// and the test asserts what this component is responsible for: the bars reaching the series, the
// second encoding on the candles, markers going through the plugin rather than a rebuild, and the
// chosen trade moving the view.
const {
  createChart,
  setData,
  setMarkers,
  setVisibleRange,
  fitContent,
  remove,
  options,
  kinds,
  getVisibleRange,
  subscribeRange,
  unsubscribeRange,
  NOON,
} =
  vi.hoisted(() => {
    const setData = vi.fn()
    const setMarkers = vi.fn()
    const setVisibleRange = vi.fn()
    const fitContent = vi.fn()
    const remove = vi.fn()
    const options: Record<string, unknown>[] = []
    const kinds: string[] = []
    // A twelve-hour window across 600px, price 100-110 across 300px — the same shape the
    // geometry tests in `price.test.ts` use, so a rectangle here lands where they say it does.
    const NOON = 1722513600
    const getVisibleRange = vi.fn(() => ({ from: NOON, to: NOON + 12 * 3600 }))
    const timeToCoordinate = vi.fn((time: number) => ((time - NOON) / (12 * 3600)) * 600)
    const priceToCoordinate = vi.fn((price: number) => ((110 - price) / 10) * 300)
    const subscribeRange = vi.fn()
    const unsubscribeRange = vi.fn()
    const addSeries = vi.fn((kind: string, given: Record<string, unknown>) => {
      kinds.push(kind)
      options.push(given)
      return { setData, priceToCoordinate }
    })
    const createChart = vi.fn(() => ({
      addSeries,
      timeScale: () => ({
        fitContent,
        setVisibleRange,
        getVisibleRange,
        timeToCoordinate,
        subscribeVisibleTimeRangeChange: subscribeRange,
        unsubscribeVisibleTimeRangeChange: unsubscribeRange,
      }),
      remove,
    }))
    return {
      createChart,
      setData,
      setMarkers,
      setVisibleRange,
      fitContent,
      remove,
      options,
      kinds,
      getVisibleRange,
      subscribeRange,
      unsubscribeRange,
      NOON,
    }
  })

vi.mock('lightweight-charts', () => ({
  createChart,
  createSeriesMarkers: () => ({ setMarkers }),
  CandlestickSeries: 'CandlestickSeries',
  LineSeries: 'LineSeries',
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
  kinds.length = 0
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

describe('PriceChart — the curves the strategy was reading', () => {
  const ema = {
    label: 'EMA 9',
    points: [
      ['2024-08-01T15:00:00Z', '226.10'],
      ['2024-08-01T16:00:00Z', '226.40'],
    ] as [string, string][],
  }

  it('draws a line per curve, on top of the candles', () => {
    draw({ overlays: [ema] })

    expect(kinds).toEqual(['CandlestickSeries', 'LineSeries'])
  })

  it('names every curve in the legend, because colour alone may not identify it', () => {
    draw({ overlays: [ema] })

    expect(screen.getByText('EMA 9')).toBeInTheDocument()
  })

  it('draws only the candles when the strategy reads no curves', () => {
    // A structure setup. The chart must be an ordinary chart, not an error state.
    draw({ overlays: [] })

    expect(kinds).toEqual(['CandlestickSeries'])
  })

  it('sends the curve its own points rather than padding it to the bars', () => {
    // The curve is two points long against six candles: the warm-up gap survives to the library,
    // which is what lets it be drawn starting where the indicator started.
    draw({ overlays: [ema] })

    expect(setData).toHaveBeenLastCalledWith([
      { time: 1722524400, value: 226.1 },
      { time: 1722528000, value: 226.4 },
    ])
  })
})

describe('PriceChart — the regions the strategy marked', () => {
  const HOUR = 3600

  function zone(over: Partial<Zone> = {}): Zone {
    return {
      kind: 'demand',
      top: '106',
      bottom: '104',
      from_time: new Date((NOON + 2 * HOUR) * 1000).toISOString(),
      confirmed_at: new Date((NOON + 3 * HOUR) * 1000).toISOString(),
      mitigated_at: new Date((NOON + 6 * HOUR) * 1000).toISOString(),
      primary: true,
      ...over,
    }
  }

  /** The rectangles on the overlay layer, in document order. */
  function drawn(container: HTMLElement): SVGRectElement[] {
    return Array.from(container.querySelectorAll('svg rect'))
  }

  it('draws one rectangle per region', () => {
    const view = draw({ zones: [zone(), zone({ primary: false })] })

    expect(drawn(view.container)).toHaveLength(2)
  })

  it('draws no layer at all when the strategy marked none', () => {
    // A swing setup. The chart must be an ordinary chart, not an empty overlay.
    const view = draw({ zones: [] })

    expect(view.container.querySelector('svg')).toBeNull()
  })

  it('fills a region still standing and leaves a taken one as an outline', () => {
    // His rule, on screen: a mitigated region stops competing with the ones still in play, but
    // stays visible — where a region died is what explains a stretch the strategy sat out.
    const live = drawn(draw({ zones: [zone({ mitigated_at: null })] }).container)[0]
    cleanup()
    const taken = drawn(draw({ zones: [zone()] }).container)[0]

    expect(live?.getAttribute('fill')).not.toBe('none')
    expect(taken?.getAttribute('fill')).toBe('none')
  })

  it('dashes a secondary region, so the allow_secondary flag is visible', () => {
    const primary = drawn(draw({ zones: [zone({ primary: true })] }).container)[0]
    cleanup()
    const secondary = drawn(draw({ zones: [zone({ primary: false })] }).container)[0]

    expect(primary?.getAttribute('stroke-dasharray')).toBeNull()
    expect(secondary?.getAttribute('stroke-dasharray')).toBe('4 3')
  })

  it('thickens the edge price has to come back to, which differs by side', () => {
    // The second encoding, and it is not decoration: the up and down hues separate by only
    // ΔE 7.5 under deuteranopia. It is also the price the limit order rests at — a demand
    // region is entered at its top, a supply region at its bottom.
    const demand = draw({ zones: [zone({ kind: 'demand' })] }).container.querySelector('svg line')
    cleanup()
    const supply = draw({ zones: [zone({ kind: 'supply' })] }).container.querySelector('svg line')

    // 106 and 104 on a 100-110 scale over 300px: y = 120 and y = 180.
    expect(demand?.getAttribute('y1')).toBe('120')
    expect(supply?.getAttribute('y1')).toBe('180')
  })

  it('follows the chart as it is panned, and lets go on unmount', () => {
    // The rectangles live in pixel space, so they are meaningless until re-measured. A layer
    // that subscribed and never unsubscribed would keep measuring a chart that is gone.
    const view = draw({ zones: [zone()] })
    expect(subscribeRange).toHaveBeenCalledTimes(1)

    view.unmount()

    expect(unsubscribeRange).toHaveBeenCalledTimes(1)
  })

  it('draws nothing while the chart cannot say what is visible', () => {
    // Before the first layout there is no range, and rectangles placed against a missing one
    // would all land at the origin — a stack of zones in the corner, which reads as data.
    getVisibleRange.mockReturnValueOnce(null as never)

    const view = draw({ zones: [zone()] })

    expect(view.container.querySelector('svg')).toBeNull()
  })
})
