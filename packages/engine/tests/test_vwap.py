"""Anchored VWAP, checked against volume-weighted averages worked out by hand.

The series is chosen so the three sources disagree — a low-priced heavy bar pulls the `low` line
well under the `high` line — because a band whose three lines coincided would pass on a VWAP that
had quietly ignored its source. Volume is what makes it a *weighted* average, so the goldens use
uneven volumes: an equal-weight bug would compute a different number and fail here.
"""

from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.domain import Candle, Money
from tradeforge_engine.errors import EngineError
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.testing import HOUR, START
from tradeforge_engine.vwap import AnchoredVWAP


def _candle(  # noqa: PLR0913 — keyword-only; each names one field of the OHLCV bar under test
    index: int,
    *,
    high: str,
    low: str,
    close: str,
    open_: str | None = None,
    tick: int = 0,
    real: int = 0,
) -> Candle:
    return Candle(
        time=START + index * HOUR,
        open=Decimal(close if open_ is None else open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        tick_volume=tick,
        real_volume=real,
    )


def _run(vwap: AnchoredVWAP, candles: list[Candle]) -> list[Money | None]:
    out: list[Money | None] = []
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            vwap.update(candle)
            out.append(vwap.value())
    return out


# bar 0: hlc3 10, high 12, low 8, volume 100.  bar 1: hlc3 12, high 14, low 10, volume 300.
_GOLDEN = [
    _candle(0, high="12", low="8", close="10", tick=100),
    _candle(1, high="14", low="10", close="12", tick=300),
]


def test_hlc3_is_the_volume_weighted_typical_price() -> None:
    """bar 0: (12+8+10)/3 = 10, weight 100 -> 10.
    bar 1: typical 12, weight 300 -> (10*100 + 12*300) / 400 = 4600/400 = 11.5."""
    assert _run(AnchoredVWAP(source="hlc3"), _GOLDEN) == [Decimal("10"), Decimal("11.5")]


def test_high_and_low_sources_form_the_band() -> None:
    """The same anchor and volumes, priced on the highs and on the lows, bound a zone.
    high: 12, then (12*100 + 14*300)/400 = 13.5.  low: 8, then (8*100 + 10*300)/400 = 9.5."""
    assert _run(AnchoredVWAP(source="high"), _GOLDEN) == [Decimal("12"), Decimal("13.5")]
    assert _run(AnchoredVWAP(source="low"), _GOLDEN) == [Decimal("8"), Decimal("9.5")]


def test_auto_volume_prefers_real_and_falls_back_to_ticks() -> None:
    """`auto` uses exchange volume where it exists. Two bars with equal real volume but wildly
    different ticks: reading real gives a clean midpoint, reading ticks would not."""
    bars = [
        _candle(0, high="10", low="10", close="10", tick=999, real=100),
        _candle(1, high="20", low="20", close="20", tick=1, real=100),
    ]
    # real-weighted: (10*100 + 20*100) / 200 = 15. tick-weighted would be ~10.01.
    assert _run(AnchoredVWAP(source="hlc3", volume="auto"), bars)[-1] == Decimal("15")
    assert _run(AnchoredVWAP(source="hlc3", volume="real"), bars)[-1] == Decimal("15")


def test_forcing_tick_volume_ignores_real() -> None:
    bars = [
        _candle(0, high="10", low="10", close="10", tick=100, real=1),
        _candle(1, high="20", low="20", close="20", tick=300, real=1),
    ]
    # tick-weighted: (10*100 + 20*300)/400 = 7000/400 = 17.5.
    assert _run(AnchoredVWAP(source="hlc3", volume="tick"), bars)[-1] == Decimal("17.5")


def test_reset_reanchors_to_the_next_candle() -> None:
    """Re-anchoring is how a setup moves the VWAP onto a fresh swing: after reset the average is
    the second bar alone, as if the first had never been seen."""
    vwap = AnchoredVWAP(source="hlc3")
    with localcontext(ENGINE_CONTEXT):
        vwap.update(_GOLDEN[0])
        vwap.reset()
        vwap.update(_GOLDEN[1])
        assert vwap.value() == Decimal("12")  # bar 1's typical price, anchor forgotten


def test_value_is_none_until_there_is_volume() -> None:
    """A bar with no volume prices nothing — it neither divides by zero nor moves the average."""
    vwap = AnchoredVWAP(source="hlc3")
    with localcontext(ENGINE_CONTEXT):
        vwap.update(_candle(0, high="12", low="8", close="10", tick=0, real=0))
        assert vwap.value() is None
        vwap.update(_candle(1, high="14", low="10", close="12", tick=100))
        assert vwap.value() == Decimal("12")  # the empty bar contributed nothing


def test_unknown_source_and_volume_are_refused() -> None:
    with pytest.raises(EngineError, match="unknown VWAP source"):
        AnchoredVWAP(source="median")
    with pytest.raises(EngineError, match="unknown VWAP volume"):
        AnchoredVWAP(volume="both")


def test_a_series_with_no_volume_never_produces_a_value() -> None:
    """The warm-up never ends if volume never arrives — an average of nothing is nothing, not a
    zero. Five empty bars in a row all read None."""
    empties = [_candle(i, high="12", low="8", close="10", tick=0, real=0) for i in range(5)]
    assert _run(AnchoredVWAP(source="hlc3"), empties) == [None] * 5


def test_reset_clears_the_value_immediately() -> None:
    """Re-anchoring returns the indicator to warm-up: after reset, before any new candle, there is
    nothing to average."""
    vwap = AnchoredVWAP(source="hlc3")
    with localcontext(ENGINE_CONTEXT):
        vwap.update(_GOLDEN[0])
        assert vwap.value() is not None
        vwap.reset()
        assert vwap.value() is None


def test_a_bar_with_negative_volume_is_discarded() -> None:
    """A corrupt feed can carry a negative volume; the `<= 0` guard drops it, so it cannot flip the
    sign of the weighted sum. Only the valid bar that follows counts."""
    vwap = AnchoredVWAP(source="hlc3")
    with localcontext(ENGINE_CONTEXT):
        vwap.update(_candle(0, high="14", low="10", close="12", tick=-100))
        assert vwap.value() is None
        vwap.update(_candle(1, high="12", low="8", close="10", tick=100))
        assert vwap.value() == Decimal("10")


@given(
    bars=st.lists(
        st.tuples(
            st.integers(min_value=1, max_value=1000),  # price (flat candle -> exact typical)
            st.integers(min_value=1, max_value=10_000),  # volume
        ),
        min_size=1,
        max_size=40,
    )
)
def test_the_average_stays_within_the_prices_it_averaged(bars: list[tuple[int, int]]) -> None:
    """A weighted average can never leave the range of its inputs: the VWAP sits between the
    smallest and largest price seen since the anchor. A sign or accumulation bug escapes that
    range. Flat candles keep the typical price an exact integer, so the only rounding is the final
    division — which of a value strictly inside the range stays inside it."""
    candles = [
        _candle(i, high=str(p), low=str(p), close=str(p), tick=vol)
        for i, (p, vol) in enumerate(bars)
    ]
    with localcontext(ENGINE_CONTEXT):
        vwap = AnchoredVWAP(source="hlc3")
        prices: list[Money] = []
        for candle in candles:
            vwap.update(candle)
            prices.append(candle.close)
            current = vwap.value()
            assert current is not None
            assert min(prices) <= current <= max(prices)
