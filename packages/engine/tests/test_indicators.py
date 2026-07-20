"""Indicators, checked against numbers worked out by hand.

An indicator feeds a comparison that decides a trade, so "close enough" is not a category
that exists here. Each golden is a short series whose SMA and EMA can be computed on paper —
and chosen so the two *disagree*, because a test where every average is equal would pass on
an EMA that had quietly been implemented as an SMA.

The arithmetic runs under the engine's pinned decimal context, the same one `run()` installs,
so the value a test asserts is the value a backtest would see — not a number that depends on
whatever precision the ambient process happens to carry.
"""

from decimal import Decimal, localcontext

import pytest
from hypothesis import given
from hypothesis import strategies as st

from tradeforge_engine.domain import Candle
from tradeforge_engine.errors import EngineError
from tradeforge_engine.indicators import EMA, RSI, SMA, build_indicator
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.protocols import Indicator
from tradeforge_engine.testing import HOUR, START, bar


def _closes(*values: str) -> list[Candle]:
    """Flat candles at each close — all we need to exercise a `source="close"` indicator."""
    candles: list[Candle] = []
    for index, value in enumerate(values):
        price = Decimal(value)
        candles.append(
            Candle(time=START + index * HOUR, open=price, high=price, low=price, close=price)
        )
    return candles


def _values(indicator: Indicator, candles: list[Candle]) -> list[Decimal | None]:
    out: list[Decimal | None] = []
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            indicator.update(candle)
            out.append(indicator.value())
    return out


# The series both goldens share: non-linear, so SMA and EMA cannot coincide by accident.
_GOLDEN = ["12", "24", "15", "42", "30"]


def test_sma_matches_hand_calculation() -> None:
    """SMA(3) of 12, 24, 15, 42, 30.

    None, None while the window fills, then the plain mean of each trailing three:
    (12+24+15)/3 = 17, (24+15+42)/3 = 27, (15+42+30)/3 = 29.
    """
    values = _values(SMA(period=3), _closes(*_GOLDEN))
    assert values == [None, None, Decimal("17"), Decimal("27"), Decimal("29")]


def test_ema_matches_hand_calculation() -> None:
    """EMA(3), alpha = 2/(3+1) = 0.5, seeded with the SMA of the first three bars.

    Seed at bar 3 = (12+24+15)/3 = 17. Then ema = close·0.5 + ema_prev·0.5:
    bar 4 = 42·0.5 + 17·0.5 = 29.5; bar 5 = 30·0.5 + 29.5·0.5 = 29.75.
    Note bar 4: the SMA said 27, the EMA says 29.5 — they are genuinely different indicators.
    """
    values = _values(EMA(period=3), _closes(*_GOLDEN))
    assert values == [None, None, Decimal("17"), Decimal("29.5"), Decimal("29.75")]


# RSI goldens. period=2 keeps the arithmetic on paper; Wilder's 1/period (0.5 here) is still
# distinct from EMA's 2/(period+1) = 0.667, so a bug that reached for the EMA weight would read
# 83.33 at the last bar, not 80 — the golden fails loudly on that swap.
_RSI_GOLDEN = ["100", "103", "102", "102.5"]


def test_rsi_matches_hand_calculation() -> None:
    """RSI(2) of 100, 103, 102, 102.5.

    Changes: +3 (gain), -1 (loss), +0.5 (gain). None while fewer than `period` changes exist.
    Seed at bar 3 (two changes): avg_gain = (3+0)/2 = 1.5, avg_loss = (0+1)/2 = 0.5,
    RS = 1.5/0.5 = 3, RSI = 100 - 100/(1+3) = 75.
    Bar 4, Wilder (alpha = 1/2): avg_gain = 1.5 + (0.5-1.5)/2 = 1.0; avg_loss = 0.5 + (0-0.5)/2 =
    0.25; RS = 1.0/0.25 = 4, RSI = 100 - 100/5 = 80.
    """
    values = _values(RSI(period=2), _closes(*_RSI_GOLDEN))
    assert values == [None, None, Decimal("75"), Decimal("80")]


def test_rsi_pins_to_100_when_there_are_no_losses() -> None:
    """An only-rising window has avg_loss = 0: RS → ∞, and RSI pins to 100 instead of dividing by
    zero. The same branch is what a flat series reads — no move is a gain or a loss."""
    values = _values(RSI(period=3), _closes("1", "2", "3", "4", "5"))
    assert values[-1] == Decimal("100")


def test_rsi_pins_to_0_when_there_are_no_gains() -> None:
    """The mirror image: an only-falling window has avg_gain = 0, so RS = 0 and RSI = 0."""
    values = _values(RSI(period=3), _closes("5", "4", "3", "2", "1"))
    assert values[-1] == Decimal("0")


def test_rsi_reads_100_on_a_perfectly_flat_series() -> None:
    """A market that never moves has no gains *and* no losses: avg_loss is 0, so RSI pins to 100
    — this engine's convention, not the 50 some libraries return for 0/0. A flat tape tripping
    `RSI > 70 -> sell` is a real consequence, made visible by a test rather than left to a
    comment. (The only-rising golden above shares the branch; this is the distinct 0/0 case.)"""
    values = _values(RSI(period=3), _closes("100", "100", "100", "100", "100"))
    assert values[-1] == Decimal("100")


def test_rsi_is_none_until_it_has_seen_period_plus_one_closes() -> None:
    """RSI needs `period` changes, and a change needs two closes: `value()` is None through the
    first `period` closes and a number only from close `period + 1` on."""
    assert _values(RSI(period=4), _closes("1", "2", "3", "4")) == [None, None, None, None]
    assert _values(RSI(period=4), _closes("1", "2", "1", "2", "1"))[-1] is not None


def test_rsi_refuses_a_non_positive_period() -> None:
    with pytest.raises(ValueError, match="RSI period must be >= 1"):
        RSI(period=0)


def test_an_indicator_is_none_until_it_has_seen_period_candles() -> None:
    """The warm-up is a fact, not a nuisance: bar 3 of a 20-period average has no value."""
    sma = SMA(period=20)
    values = _values(sma, _closes(*[str(i) for i in range(19)]))
    assert all(value is None for value in values)
    assert len(values) == 19


def test_a_period_of_one_is_the_price_itself() -> None:
    """SMA(1) and EMA(1) both track the source with no lag — the degenerate, checkable case."""
    candles = _closes("10", "20", "30")
    assert _values(SMA(period=1), candles) == [Decimal("10"), Decimal("20"), Decimal("30")]
    assert _values(EMA(period=1), candles) == [Decimal("10"), Decimal("20"), Decimal("30")]


def test_the_source_field_is_honoured() -> None:
    """An SMA on the high reads the high, not the close."""
    candles = [
        bar(0, open_="1.0", close="1.0", high="2.0", low="0.5"),
        bar(1, open_="1.0", close="1.0", high="4.0", low="0.5"),
    ]
    sma = SMA(period=2, source="high")
    values = _values(sma, candles)
    assert values[-1] == Decimal("3.0")  # (2.0 + 4.0) / 2


def test_build_indicator_reads_the_registry() -> None:
    indicator_id, indicator = build_indicator(
        {"id": "sma_fast", "type": "SMA", "params": {"period": 9, "source": "close"}}
    )
    assert indicator_id == "sma_fast"
    assert isinstance(indicator, SMA)


def test_build_indicator_refuses_an_unknown_type() -> None:
    """A strategy naming an indicator the engine cannot compute must fail at compile, not run
    on a default and produce a plausible, wrong backtest."""
    with pytest.raises(EngineError, match="unknown indicator type"):
        build_indicator({"id": "x", "type": "STOCHASTIC", "params": {"period": 14}})


def test_build_indicator_refuses_non_object_params() -> None:
    with pytest.raises(EngineError, match="params must be an object"):
        build_indicator({"id": "x", "type": "SMA", "params": 9})


def test_build_indicator_refuses_a_missing_id() -> None:
    with pytest.raises(EngineError, match="missing a string id"):
        build_indicator({"type": "SMA", "params": {"period": 9}})


def test_a_non_positive_period_is_refused() -> None:
    with pytest.raises(ValueError, match="SMA period must be >= 1"):
        SMA(period=0)
    with pytest.raises(ValueError, match="EMA period must be >= 1"):
        EMA(period=0)


def test_an_unknown_price_source_is_refused() -> None:
    """The schema validates the source, but the engine still refuses one it cannot read rather
    than resolve `getattr(candle, "volume")` to something plausible."""
    sma = SMA(period=1, source="nonsense")
    with pytest.raises(EngineError, match="unknown price source"):
        sma.update(_closes("1.0")[0])


def test_ema_alpha_is_computed_under_the_engine_context_not_the_ambient_one() -> None:
    """The determinism bug this pins (engine-guardian, PR-104).

    `alpha = 2/(period+1)` is inexact for almost every period (2/3 here). Computed in
    `__init__` — which runs inside `compile_strategy`, *outside* the pinned context `run()`
    installs — it would inherit whatever precision the ambient process happens to carry. Two
    workers compiling the same strategy under different global decimal contexts would then hold
    EMAs with different alphas, and a crossover flipping one bar early rewrites the whole equity
    curve. Exactly the process-global hazard `ENGINE_CONTEXT` exists to remove (loop.py:47).

    The assertion is on `alpha` itself, not on a value sequence: whether the difference
    *propagates* to the output is value-dependent (with few significant digits it usually does
    not), so a golden on the values would pass whether or not the bug is present — the trap this
    project keeps falling into. Alpha is where the non-determinism is *born*, so alpha is what
    the test pins. Built under a tampered prec=50 context, it must equal the default's, because
    it is computed lazily in `update()`, under the engine context, never at construction.
    """
    candles = _closes("10", "20", "30")  # three bars: enough to pass seeding and compute alpha

    default = EMA(period=2)
    _values(default, candles)

    with localcontext() as tampered:
        tampered.prec = 50
        built_under_tampered_context = EMA(period=2)
    _values(built_under_tampered_context, candles)

    assert default._alpha is not None  # it was actually computed, not both left at None
    assert default._alpha == built_under_tampered_context._alpha


@given(
    period=st.integers(min_value=1, max_value=20),
    constant=st.integers(min_value=1, max_value=1000),
    length=st.integers(min_value=1, max_value=40),
)
def test_a_flat_series_averages_to_its_own_level(period: int, constant: int, length: int) -> None:
    """A mean of one repeated value is that value — for both SMA and EMA, once warm.

    The property that catches a seeding bug: an EMA seeded wrong drifts toward its true value
    over the first few bars instead of sitting on it, and a constant series is where that drift
    is visible with nothing else moving.
    """
    candles = _closes(*[str(constant)] * length)
    for indicator in (SMA(period=period), EMA(period=period)):
        values = _values(indicator, candles)
        for index, value in enumerate(values):
            if index + 1 < period:
                assert value is None
            else:
                assert value == Decimal(constant)


@given(period=st.integers(min_value=1, max_value=10), length=st.integers(min_value=0, max_value=15))
def test_value_is_none_exactly_during_warmup(period: int, length: int) -> None:
    """`value()` is None on bars 1..period-1 and a number from bar `period` on. No exceptions,
    no half-formed values — the boundary a strategy relies on to not trade too early."""
    candles = _closes(*[str(i + 1) for i in range(length)])
    for indicator in (SMA(period=period), EMA(period=period)):
        values = _values(indicator, candles)
        for index, value in enumerate(values):
            assert (value is None) == (index + 1 < period)


@given(
    period=st.integers(min_value=1, max_value=10),
    moves=st.lists(st.integers(min_value=-5, max_value=5), min_size=0, max_size=40),
)
def test_rsi_stays_within_0_and_100_and_warms_up_on_schedule(period: int, moves: list[int]) -> None:
    """RSI is bounded to [0, 100] the instant it exists, and it exists from exactly the
    `period`-th close on — one change per bar after the first, `period` changes to seed, so the
    warm-up ends one bar later than an SMA/EMA of the same period (which is why RSI cannot simply
    join the moving-average property tests above).

    The single hand-worked golden cannot reach this whole class: a gain/loss swap in a future
    refactor would keep that golden green yet push some series out of [0, 100] — this is the test
    that would fail. The walk starts at 1000 so flat candles stay strictly positive.
    """
    prices = [1000]
    for move in moves:
        prices.append(prices[-1] + move)
    values = _values(RSI(period=period), _closes(*[str(price) for price in prices]))
    for index, value in enumerate(values):
        if index < period:
            assert value is None
        else:
            assert value is not None
            assert Decimal(0) <= value <= Decimal(100)
