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
from tradeforge_engine.indicators import (
    ADX,
    ATR,
    COMPOSITE_COMPONENTS,
    EMA,
    RSI,
    SMA,
    Bollinger,
    ComponentView,
    Highest,
    Lowest,
    build_indicator,
)
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.protocols import CompositeIndicator, Indicator
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


# --------------------------------------------------------------------------- #
# ATR, HIGHEST, LOWEST — the candle-reading indicators                          #
# --------------------------------------------------------------------------- #

# Five bars, chosen so every true range is a whole number and so the gap on bar 4 is the term
# that wins. Worked out on paper, column by column:
#
#   bar  high  low  close   TR                                      why
#   1     10    8     9     10 - 8                            = 2   no previous close
#   2     12    9    11     max(3, |12-9|,  |9-9| )           = 3   the bar's own range
#   3     14   10    13     max(4, |14-11|, |10-11|)          = 4   the bar's own range
#   4     22   21    22     max(1, |22-13|, |21-13|)          = 9   ** the gap, not the range **
#   5     23   21    22     max(2, |23-22|, |21-22|)          = 2   the bar's own range
#
# Bar 4 is the whole reason ATR is not `high - low`: the bar spans 1 point and the market moved
# 9. An implementation that ignored the previous close would report 1 and call a gap day quiet.
_GAPPED = [
    ("10", "8", "9"),
    ("12", "9", "11"),
    ("14", "10", "13"),
    ("22", "21", "22"),
    ("23", "21", "22"),
]


def _candles(rows: list[tuple[str, str, str]]) -> list[Candle]:
    """Bars from (high, low, close), with the open placed where a real market would put it.

    ⚠️ The open is the previous close **clamped into this bar's own range**, and the clamp is
    the point rather than a detail: a gap *is* the market opening away from where it closed, so
    on bar 4 the open is 21 and not 13. `Candle` refuses a body outside its own high and low —
    correctly, since a bar whose open sits below its low is one where a stop would trigger at a
    price that never traded. No indicator here reads the open; the fixture has to be a possible
    market anyway, or it proves things about bars that cannot exist.
    """
    candles: list[Candle] = []
    previous = Decimal(rows[0][2])
    for index, (high, low, close) in enumerate(rows):
        top, bottom = Decimal(high), Decimal(low)
        candles.append(
            Candle(
                time=START + index * HOUR,
                open=min(max(previous, bottom), top),
                high=top,
                low=bottom,
                close=Decimal(close),
            )
        )
        previous = Decimal(close)
    return candles


def test_atr_is_the_wilder_average_of_the_true_ranges() -> None:
    """The golden, and the numbers are exact by construction.

    Seed  = (2 + 3 + 4) / 3          = 3
    Bar 4 = 3 + (9 - 3) / 3          = 5
    Bar 5 = 5 + (2 - 5) / 3          = 4

    ⚠️ **The series was chosen so Wilder and an ordinary EMA disagree.** An EMA of period 3 has
    weight 2/(3+1) = 0.5, so bar 4 would read 3 + 0.5 x (9 - 3) = **6**, not 5. That single
    digit is the difference between an ATR that matches every charting tool and one that is
    quietly ~2x too fast for ever — the most common way this indicator is got wrong.
    """
    values = _values(ATR(period=3), _candles(_GAPPED))

    assert values == [None, None, Decimal(3), Decimal(5), Decimal(4)]


def test_a_gap_down_is_as_wide_as_a_gap_up() -> None:
    """⚠️ The `abs()` in both gap terms, which only a **downward** gap can prove.

    `_GAPPED` gaps up, and there every term of the maximum is already positive — so the `abs`
    is decoration in that fixture and a version without it passes the whole suite.

    Here the market closes at 13 and the next bar collapses, trading 5.50 down to 4.00. The
    true range is `|4 - 13| = 9`: the distance travelled, gap included. Drop the `abs` and the
    maximum is taken over `1.50, -7.50, -9.00`, so the answer is **1.50** — the bar's own
    range, on the day of the largest move of the year.

    What that costs is not an odd number in a report. An ATR-sized stop would be six times too
    tight exactly when the market gapped, and `rising(atr)` and `between(atr, ...)` would read
    a crash as a quiet market — the filters inverted on the one bar they exist for.
    """
    collapse = _candles(
        [
            ("10", "8", "9"),
            ("12", "9", "11"),
            ("14", "10", "13"),
            ("5.50", "4", "4.20"),
        ]
    )

    # TR: 2, 3, 4 as before, then |4 - 13| = 9. Seed (2+3+4)/3 = 3, then 3 + (9-3)/3 = 5.
    assert _values(ATR(period=3), collapse) == [None, None, Decimal(3), Decimal(5)]


def test_atr_says_nothing_until_it_has_seen_a_full_period_of_ranges() -> None:
    """A half-formed average is how a strategy sizes a stop off a number that does not exist."""
    assert _values(ATR(period=4), _candles(_GAPPED)) == [
        None,
        None,
        None,
        # (2 + 3 + 4 + 9) / 4 = 4.5
        Decimal("4.5"),
        # 4.5 + (2 - 4.5) / 4 = 3.875
        Decimal("3.875"),
    ]


def test_the_channel_is_the_extreme_of_the_bars_that_closed_before_this_one() -> None:
    """`HIGHEST` reads highs and `LOWEST` reads lows, over the window **ending one bar back**.

    Over the same five bars, with a window of three:

        highs   10  12  14  22  23   ->  HIGHEST(3) = -, -, -, 14, 22
        lows     8   9  10  21  21   ->  LOWEST(3)  = -, -, -,  8,  9

    ⚠️ **Bar 4 is the assertion that matters, and it is 14 — not 22.** Fold the current bar in
    first and the channel reads 22, which is that bar's own high, and then
    `breaks_above(price.high, channel)` can never be true: the level moves up to meet the price
    on the very bar that was supposed to break it. That was measured, not argued — the inclusive
    version produced zero entries over a thirty-bar breakout, with the largest
    `high - channel` over the whole run being exactly zero.

    The second assertion is the sliding: `LOWEST` on bar 5 is 9, not 8, because bar 1 has left
    the window. A running minimum rather than a windowed one still reports 8 for ever, and the
    channel would only ever widen.
    """
    candles = _candles(_GAPPED)

    assert _values(Highest(period=3), candles) == [
        None,
        None,
        None,
        Decimal(14),
        Decimal(22),
    ]
    assert _values(Lowest(period=3), candles) == [
        None,
        None,
        None,
        Decimal(8),
        Decimal(9),
    ]


def test_the_channel_is_a_level_this_bar_can_actually_break() -> None:
    """The property the exclusion exists for, stated as the rule a document would write.

    A bar whose high exceeds every high of the `period` bars before it must read as a break.
    Under an inclusive window this test is unsatisfiable by construction, which is the whole
    finding: the feature had a golden for its arithmetic and none for its purpose.
    """
    breakout = _candles(
        [("10", "8", "9"), ("11", "9", "10"), ("12", "10", "11"), ("20", "12", "19")]
    )
    channel = Highest(period=3)
    values = _values(channel, breakout)

    assert values[3] == Decimal(12)
    assert breakout[3].high > values[3]


def test_the_channel_holds_an_earlier_extreme_the_newer_bars_cannot_beat() -> None:
    """The deque exists for this: the answer is often not the newest bar.

    Highs 30, 20, 21, 22, 23 with a window of three. Bar 4 reads 30 — the oldest bar in its
    window — and bar 5 reads 22, when the 30 has finally left. An implementation that only
    remembered the most recent bar, or that popped the front unconditionally, gets bar 4 wrong
    and looks right on every series that happens to be rising.
    """
    candles = _candles(
        [("30", "1", "5"), ("20", "2", "5"), ("21", "3", "5"), ("22", "4", "5"), ("23", "5", "6")]
    )

    assert _values(Highest(period=3), candles) == [
        None,
        None,
        None,
        Decimal(30),
        Decimal(22),
    ]


def test_one_bar_can_evict_several_candidates_at_once() -> None:
    """⚠️ The `while` in the pop loop, which a single-pop `if` passes every earlier test with.

    Highs 30, 12, 11, 10, 25 over a window of four. When the 25 arrives it has to unseat the 10,
    the 11 **and** the 12 in one go: while any of them stays behind it, it is a candidate that
    can never be the answer again. Pop only the last one and the 12 and 11 stay parked.

    Nothing is visibly wrong while the 30 is still in the window — the front is correct, so
    every assertion about the answer passes. The damage lands on the bar the 30 expires: the
    channel then answers **12** for a window whose true maximum is 25, less than half the real
    level, and the next breakout fires against a rail that is not there.
    """
    highs = ["30", "12", "11", "10", "25", "9", "8"]
    candles = _candles([(high, "1", "5") for high in highs])

    values = _values(Highest(period=4), candles)

    # Bar 5 still sees the 30; by bar 6 it has expired and the 25 has to be the answer.
    assert values[4] == Decimal(30)
    assert values[5] == Decimal(25)
    assert values[6] == Decimal(25)


def test_a_flat_market_reads_its_own_level_and_keeps_one_candidate() -> None:
    """Ties, which are what a flat market is made of.

    Four identical highs with a window of two: the channel is that value on every bar it has
    one. That much is true whichever way the tie is broken — measured, not assumed, because the
    first version of this test claimed a strict `>` would crash here and it does not: the ties
    are simply kept, and the front of the deque is still a correct answer.

    So the second assertion is the one that pins the choice, and it is about **memory rather
    than about the value**: `>=` pops the value it ties with, because a newer bar with the same
    extreme outlives the older one and the older can never be needed again. Without it a flat
    stretch parks one candidate per bar of the window — bounded, so not a leak, but `period`
    entries where one would do, on exactly the market that produces the longest runs of ties.
    """
    candles = _candles([("7", "7", "7")] * 4)

    assert _values(Highest(period=2), candles) == [None, None, Decimal(7), Decimal(7)]
    assert _values(Lowest(period=2), candles) == [None, None, Decimal(7), Decimal(7)]

    channel = Highest(period=2)
    for candle in candles:
        channel.update(candle)
    assert len(channel._candidates) == 1


def test_the_new_indicators_are_reachable_by_the_names_a_document_uses() -> None:
    """The registry is the contract between a stored document and this module."""
    for kind in ("ATR", "HIGHEST", "LOWEST"):
        spec = {"id": f"x_{kind}", "type": kind, "params": {"period": 3}}
        name, indicator = build_indicator(spec)
        assert name == f"x_{kind}"
        # ⚠️ Narrowed, not cast. `build_indicator` may now hand back a composite, and the claim
        # this test makes about these three is precisely that it does not: they answer with one
        # number, so a document referring to `x_ATR.upper` has nothing to reach.
        assert isinstance(indicator, Indicator)
        assert indicator.value() is None


def test_the_candle_readers_take_a_period_and_nothing_else() -> None:
    """⚠️ Silently ignoring a `source` would be worse than refusing it — but the refusal that
    matters is not this one.

    An earlier version of this test called `ATR(period=3, source="close")` and asserted the
    `TypeError`. That asserts CPython's handling of an unexpected keyword, not a rule of ours,
    and static analysis flags the call as the mistake it is written to be. The rule lives where
    a *document* is read: `PeriodParams` forbids extra keys, and
    `fixtures/invalid-schema/atr_with_a_price_source.json` is that refusal as a test.

    What is worth pinning here is the builder's side of the same contract — the params it reads
    off the document. It reads a period; the ATR that comes back is the same one either way,
    which is what makes an ignored `source` invisible without the schema.
    """
    _, indicator = build_indicator({"id": "atr", "type": "ATR", "params": {"period": 3}})
    assert isinstance(indicator, ATR)

    # ⚠️ Was `pytest.raises(KeyError)`, which pinned a traceback as the contract. The backlog
    # carried it from PR-201 as work for whichever PR next touched this registry. A `KeyError:
    # 'period'` says nothing about which indicator in which document is short a parameter, and
    # `build_indicator` beside it already refuses an unknown type and a missing id with a sentence.
    with pytest.raises(EngineError, match=r"params\.period is required"):
        build_indicator({"id": "atr", "type": "ATR", "params": {"source": "close"}})


def test_a_builder_refuses_a_malformed_params_block_with_a_sentence() -> None:
    """The engine takes the DSL as a plain mapping and does not import the schema (by design), so
    these are the messages a caller with no validator gets — for every indicator in the registry,
    not only the one whose builder someone happened to look at.
    """
    with pytest.raises(EngineError, match="params must be an object"):
        build_indicator({"id": "sma_fast", "type": "SMA"})  # no params key at all
    with pytest.raises(EngineError, match=r"params\.period is required"):
        build_indicator({"id": "bb", "type": "BOLLINGER", "params": {"deviations": 2}})
    with pytest.raises(EngineError, match="must be a whole number"):
        build_indicator({"id": "adx", "type": "ADX", "params": {"period": "fourteen"}})
    # ⚠️ Two refusals, not one, and the coverage report is what separated them: a string reaches
    # `int()` and fails there, while a list or a bool never gets that far. `True` is the one worth
    # spelling out — it is an `int` subclass, so a period of `true` would otherwise be the number 1.
    with pytest.raises(EngineError, match="must be a whole number"):
        build_indicator({"id": "adx", "type": "ADX", "params": {"period": True}})
    with pytest.raises(EngineError, match="must be a whole number"):
        build_indicator({"id": "adx", "type": "ADX", "params": {"period": [14]}})
    with pytest.raises(EngineError, match=r"params\.deviations must be a number"):
        build_indicator(
            {"id": "bb", "type": "BOLLINGER", "params": {"period": 4, "deviations": "x"}}
        )


def test_a_builder_refuses_a_period_that_is_a_number_but_not_a_whole_one() -> None:
    """⚠️ Two escapes a `try: int(value) except ValueError` leaves open, both found by mutation.

    **An infinity is not a `ValueError`.** `int(float("inf"))` raises `OverflowError`, so an
    infinite period used to leave this module as a raw traceback — the exact thing `_whole_number`
    was written to stop. `nan` was only caught by luck, because `int(nan)` happens to raise
    `ValueError` instead.

    **And `int(2.7)` is 2.** A fractional period used to be silently truncated by a function whose
    own refusal says "must be a whole number": the document would run a 2-period average while its
    author read 2.7 off the screen, and every number in the backtest would be of a strategy nobody
    described. Reachable only by a mapping that did not come through the schema — which is precisely
    the caller this function exists for, since it has no validator to consult.
    """
    for period in (float("inf"), float("-inf"), float("nan"), 2.7, "2.7"):
        with pytest.raises(EngineError, match="must be a whole number"):
            build_indicator({"id": "adx", "type": "ADX", "params": {"period": period}})

    # A float that *is* a whole number stays acceptable: JSON has one number type, so a document
    # written by a browser can perfectly well carry `14.0`.
    _, indicator = build_indicator({"id": "adx", "type": "ADX", "params": {"period": 14.0}})
    assert isinstance(indicator, CompositeIndicator)


def test_the_refusal_names_the_indicator_that_caused_it() -> None:
    """ "Which one" is the first question a document with twenty indicators raises."""
    for indicator_id in ("sma_fast", "canal_alto", "forca"):
        with pytest.raises(EngineError, match=indicator_id):
            build_indicator({"id": indicator_id, "type": "SMA", "params": {}})


# --------------------------------------------------------------------------- #
# Bollinger — an average with a volatility envelope, and three readings         #
# --------------------------------------------------------------------------- #


def _number(value: Decimal | None) -> Decimal:
    """Narrow a component to a number, failing the test rather than the type checker."""
    assert value is not None
    return value


def _components(
    indicator: CompositeIndicator, candles: list[Candle]
) -> list[dict[str, Decimal | None]]:
    out: list[dict[str, Decimal | None]] = []
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            indicator.update(candle)
            out.append(dict(indicator.components()))
    return out


def test_bollinger_golden_over_a_window_whose_deviation_is_a_whole_number() -> None:
    """Bollinger(4) with 2 deviations over closes 3, 3, 5, 5, 9.

    The series is chosen so the first answer is exact on paper. At bar 3 the window is
    (3, 3, 5, 5): mean 4, and the squared deviations are 1, 1, 1, 1 — variance 1, so the
    deviation is exactly 1 and the bands land on 6 / 4 / 2.

    ⚠️ **That exactness is what separates population from sample.** Dividing the sum of squared
    deviations by `n - 1` instead of `n` gives a variance of 4/3 and a deviation of
    1.154700538..., putting the upper band at 6.309 rather than 6. A golden over a series whose
    variance happened to be large would barely move between the two; this one refuses both
    spellings of the formula from a single integer.

    At bar 4 the window is (3, 5, 5, 9): mean 5.5 and variance 4.75, whose root does not
    terminate. The middle band is still asserted exactly, and the half-width is asserted against
    `Decimal("4.75").sqrt()` — the standard library's correctly-rounded root, not this module's
    arithmetic — so what the assertion pins is the *variance* the window produced.
    """
    candles = _closes("3", "3", "5", "5", "9")
    values = _components(Bollinger(period=4, source="close", deviations=Decimal(2)), candles)

    assert values[0] == values[1] == values[2] == {"upper": None, "middle": None, "lower": None}
    assert values[3] == {
        "upper": Decimal("6"),
        "middle": Decimal("4"),
        "lower": Decimal("2"),
    }

    with localcontext(ENGINE_CONTEXT):
        half_width = Decimal(2) * Decimal("4.75").sqrt()
    assert values[4]["middle"] == Decimal("5.5")
    assert values[4]["upper"] == Decimal("5.5") + half_width
    assert values[4]["lower"] == Decimal("5.5") - half_width


def test_bollinger_bands_are_symmetric_about_the_middle_and_scale_with_the_multiplier() -> None:
    """The envelope is the same distance either side, and doubling the multiplier doubles it.

    Asserted as a relationship rather than as a second set of literals: the two runs share a
    window, so a bug in the deviation itself cancels out of the comparison and what is left is
    the multiplier's own arithmetic.
    """
    candles = _closes("3", "3", "5", "5", "9")
    one = _components(Bollinger(period=4, deviations=Decimal(1)), candles)[4]
    two = _components(Bollinger(period=4, deviations=Decimal(2)), candles)[4]

    for values in (one, two):
        assert _number(values["upper"]) - _number(values["middle"]) == _number(
            values["middle"]
        ) - _number(values["lower"])

    wide = _number(two["upper"]) - _number(two["middle"])
    narrow = _number(one["upper"]) - _number(one["middle"])
    assert wide == narrow * 2


def test_bollinger_on_a_market_that_never_moves_collapses_to_the_average() -> None:
    """A flat window has no deviation, so the three bands are one line — and nothing raises.

    ⚠️ This is the case that would crash rather than mislead. `Decimal.sqrt()` raises
    `InvalidOperation` on a negative, and the variance is computed as a difference of two nearly
    equal terms. On an exactly flat window they cancel to exactly zero, so this test passes
    without the clamp; what it pins is the *reading* — three equal bands rather than a raise or a
    stray last-place width on the quietest market there is.
    """
    values = _components(Bollinger(period=3, deviations=Decimal(2)), _closes("5", "5", "5", "5"))

    assert values[-1] == {
        "upper": Decimal("5"),
        "middle": Decimal("5"),
        "lower": Decimal("5"),
    }


def test_bollinger_survives_a_window_whose_variance_rounds_below_zero() -> None:
    """⚠️ The clamp, reached rather than argued about — and a mutation run is what forced this test.

    `variance = E[x2] - E[x]2` subtracts two nearly equal terms, and `Decimal.sqrt()` raises
    `InvalidOperation` on a negative. Removing the clamp survived every other test here, because a
    genuinely *flat* window cancels to exactly zero: `n·x / n` is exact, so there is nothing left to
    round the wrong way.

    What reaches it is a **nearly** flat window at a high price scale. Measured: from a base of 1e6
    upward, with the window's spread orders of magnitude below one tick, the cancellation eats the
    whole value and lands a last place below zero. The three prices below give a raw variance of
    exactly `-1E-15`, and without the clamp this call raises from inside an indicator — a backtest
    that dies on the quietest data it will ever see.

    The reading is zero, and that is the honest answer rather than a papered-over one: the true
    variance here is zero to every digit that survived the subtraction.
    """
    values = _components(
        Bollinger(period=3, deviations=Decimal(2)),
        _closes("1000000.0000000000", "1000000.0000000000", "1000000.0000000002"),
    )

    # The mean of the three, at the engine's 28 digits. All three bands are it: a deviation of
    # zero, which is the clamp having done its work.
    average = Decimal("1000000.000000000066666666667")
    assert values[-1] == {"middle": average, "upper": average, "lower": average}


def test_bollinger_reads_the_source_it_was_given() -> None:
    """Bands on the high are bands on the high — the same window arithmetic, a different series."""
    candles = [
        bar(0, open_="1", close="1", high="10", low="1"),
        bar(1, open_="1", close="1", high="10", low="1"),
    ]
    on_high = _components(Bollinger(period=2, source="high", deviations=Decimal(2)), candles)
    on_close = _components(Bollinger(period=2, source="close", deviations=Decimal(2)), candles)

    assert on_high[-1]["middle"] == Decimal("10")
    assert on_close[-1]["middle"] == Decimal("1")


def test_bollinger_refuses_a_period_or_a_multiplier_that_cannot_mean_anything() -> None:
    with pytest.raises(ValueError, match="period must be >= 1"):
        Bollinger(period=0, deviations=Decimal(2))
    with pytest.raises(ValueError, match="deviations must be > 0"):
        Bollinger(period=3, deviations=Decimal(0))


# --------------------------------------------------------------------------- #
# ADX — two smoothings deep, and the rules a constant series cannot separate    #
# --------------------------------------------------------------------------- #

# The golden series, built by solving for movements whose every division terminates and then
# searching for candles that produce them. `open` is the previous close throughout.
#
#  bar  high  low  close   up  down   +DM  -DM   TR    +DI     -DI     ADX
#   0    100   92   100     -     -     -    -    -     -       -       -
#   1    104  100   104     4   -8      4    0    4     -       -       -
#   2    108  104   104     4   -4      4    0    4   100       0       -
#   3    109  103   103     1    1      0    0    6    40       0     100   <- outside, EQUAL
#   4    108   97    97    -1    6      0    6   11    12.5    37.5     75
#   5    105   97    97    -3    0      0    0    8     6.25   18.75    62.5
#   6    109   97    97     4    0      4    0   12    22.5     7.5     56.25
_ADX_GOLDEN = [
    bar(0, open_="100", close="100", high="100", low="92"),
    bar(1, open_="100", close="104", high="104", low="100"),
    bar(2, open_="104", close="104", high="108", low="104"),
    bar(3, open_="104", close="103", high="109", low="103"),
    bar(4, open_="103", close="97", high="108", low="97"),
    bar(5, open_="97", close="97", high="105", low="97"),
    bar(6, open_="97", close="97", high="109", low="97"),
]


def test_adx_golden_over_a_series_solved_to_terminate() -> None:
    """ADX(2) over the series in the table above, every value exact.

    Three things this separates that a trending run of similar bars cannot:

    1. **Wilder's weight, not the EMA's.** The `DM` and `TR` series here are not constant, and a
       constant one smooths to the same number under any weight — which is how an ADX built on
       `2/(period+1)` passes a golden and disagrees with every chart. At bar 3 Wilder's `1/2`
       gives a `+DI` of 40; the EMA's `2/3` gives 33.33...
    2. **Both `DM`s cannot be non-zero.** Bar 3 is an *outside* bar — a higher high and a lower
       low — whose two moves are equal at 1. The rule says neither side won, so both are zero
       and `+DI` falls to 40. An implementation crediting both would carry `+DM = -DM = 1`, and
       every value from bar 3 on would differ.
    3. **The comparison is strict.** Reading `up >= down` rather than `up > down` on that same
       bar credits `+DM = 1`, which is the same mutant seen from the other side.

    The `ADX` line itself is asserted from bar 3, which is `2 * period - 1` — see the warm-up
    test for why that index is not `period`.
    """
    values = _components(ADX(period=2), _ADX_GOLDEN)

    assert values[0] == values[1] == {"adx": None, "plus_di": None, "minus_di": None}
    assert values[2] == {"adx": None, "plus_di": Decimal("100"), "minus_di": Decimal("0")}
    assert values[3] == {
        "adx": Decimal("100"),
        "plus_di": Decimal("40"),
        "minus_di": Decimal("0"),
    }
    assert values[4] == {
        "adx": Decimal("75"),
        "plus_di": Decimal("12.5"),
        "minus_di": Decimal("37.5"),
    }
    assert values[5] == {
        "adx": Decimal("62.5"),
        "plus_di": Decimal("6.25"),
        "minus_di": Decimal("18.75"),
    }
    assert values[6] == {
        "adx": Decimal("56.25"),
        "plus_di": Decimal("22.50"),
        "minus_di": Decimal("7.50"),
    }


def test_adx_credits_only_the_larger_move_on_an_outside_bar() -> None:
    """An outside bar with *unequal* moves: the larger one counts and the other is zero.

    The golden's outside bar has equal moves, which pins the both-zero case. This one pins the
    half the golden cannot reach — that when both moves are positive and one is larger, the
    smaller side stays at zero rather than being credited alongside it. A market that made a
    higher high and a lower low did not trend in both directions at once.
    """
    up_wins = [
        bar(0, open_="100", close="100", high="102", low="98"),
        bar(1, open_="100", close="104", high="106", low="97"),  # up 4, down 1 -> +DM only
    ]
    down_wins = [
        bar(0, open_="100", close="100", high="102", low="98"),
        bar(1, open_="100", close="96", high="103", low="94"),  # up 1, down 4 -> -DM only
    ]

    assert _components(ADX(period=1), up_wins)[-1]["minus_di"] == Decimal("0")
    assert _components(ADX(period=1), up_wins)[-1]["plus_di"] != Decimal("0")
    assert _components(ADX(period=1), down_wins)[-1]["plus_di"] == Decimal("0")
    assert _components(ADX(period=1), down_wins)[-1]["minus_di"] != Decimal("0")


def test_adx_warms_up_a_full_period_after_its_own_direction_lines() -> None:
    """`+DI` answers on bar `period`; `ADX` only on bar `2 * period - 1`.

    ⚠️ The gap is the whole reason `components()` returns a value per name instead of `None` for
    the object. `ADX` is a second Wilder smoothing *of* the `DX` the `DI` pair produces, so it
    needs `period` values of something that does not exist until bar `period`. Reporting it as
    soon as the pair exists publishes a single `DX` under the name of an average — the most
    common way an ADX disagrees with a chart over the first few dozen bars.
    """
    period = 3
    values = _components(ADX(period=period), _ADX_GOLDEN)
    first_di = next(index for index, value in enumerate(values) if value["plus_di"] is not None)
    first_adx = next(index for index, value in enumerate(values) if value["adx"] is not None)

    assert first_di == period
    assert first_adx == 2 * period - 1


def test_adx_on_a_market_that_never_moves_reports_no_trend_instead_of_dividing_by_zero() -> None:
    """Every bar identical: no range to apportion and no direction to favour.

    Two divisions by zero sit in this indicator — `DM / TR` when the true range is nothing, and
    the `DX` ratio when both `DI`s are zero — and a flat market reaches both at once. The reading
    is 0, which is also the honest answer: there is no trend here.
    """
    values = _components(ADX(period=2), _closes("5", "5", "5", "5", "5", "5"))

    assert values[-1] == {"adx": Decimal("0"), "plus_di": Decimal("0"), "minus_di": Decimal("0")}


def test_adx_refuses_a_period_that_cannot_mean_anything() -> None:
    with pytest.raises(ValueError, match="period must be >= 1"):
        ADX(period=0)


def test_every_indicator_refuses_a_period_below_one() -> None:
    """The same refusal, once per constructor. `ATR` and the channel pair had no test for theirs —
    found by the coverage report while filling the gaps this PR's own code left, and cheaper to
    close here than to write down as debt."""
    with pytest.raises(ValueError, match="ATR period must be >= 1"):
        ATR(period=0)
    with pytest.raises(ValueError, match="period must be >= 1"):
        Highest(period=0)
    with pytest.raises(ValueError, match="period must be >= 1"):
        Lowest(period=-1)


# --------------------------------------------------------------------------- #
# The composite seam itself                                                     #
# --------------------------------------------------------------------------- #


def test_a_composite_folds_a_repeated_bar_only_once() -> None:
    """⚠️ The property the overlay path depends on, and the one nothing else in the engine needs.

    A composite is reached through one channel per component, and `Charted.overlays` hands a
    reader one `Indicator` per channel — which the reader then drives. So the same closed bar
    arrives once per component. Without this, a three-band indicator would fold every bar three
    times and report an average over three times the bars anyone asked for: a plausible number,
    from a series nobody has.
    """
    candles = _closes("3", "3", "5", "5")
    once = Bollinger(period=4, deviations=Decimal(2))
    thrice = Bollinger(period=4, deviations=Decimal(2))

    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            once.update(candle)
            thrice.update(candle)
            thrice.update(candle)
            thrice.update(candle)

        assert dict(once.components()) == dict(thrice.components())
        assert once.components()["middle"] == Decimal("4")


def test_the_repeat_guard_holds_for_the_adx_too() -> None:
    """⚠️ Both composites, because the guard is inherited but the early return is per class.

    The Bollinger case above kills the mutant that removes the shared guard, so it looked like
    enough. It is not: `ADX.update` has its own `return`, and the coverage report is what said so —
    a line that never ran. And an ADX is where a triple fold does the most damage, because it is
    two smoothings deep: every bar counted three times advances both the `DM` averages and the `DX`
    average, so the warm-up ends early *and* the level is wrong.
    """
    once = ADX(period=2)
    thrice = ADX(period=2)

    with localcontext(ENGINE_CONTEXT):
        for candle in _ADX_GOLDEN:
            once.update(candle)
            thrice.update(candle)
            thrice.update(candle)
            thrice.update(candle)

        assert dict(once.components()) == dict(thrice.components())
        assert once.components()["adx"] == Decimal("56.25")


def test_a_composite_refuses_a_bar_older_than_the_one_it_last_folded() -> None:
    """Nothing here can produce this today — which is why it raises instead of being ignored.

    Ignoring it would leave the state built from a different set of bars than the caller
    believes, silently. See `guarda-inalcancavel-como-falha` in spirit: the criterion is not
    whether the case is reachable, it is how the code fails when it is.
    """
    candles = _closes("1", "2", "3")
    indicator = ADX(period=2)

    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            indicator.update(candle)
        with pytest.raises(EngineError, match="older than the last one folded"):
            indicator.update(candles[0])


def test_a_component_view_reads_one_component_and_drives_the_whole_composite() -> None:
    """The adapter that lets a composite wear the single-valued shape `Charted` asks for."""
    composite = Bollinger(period=4, deviations=Decimal(2))
    upper = ComponentView(composite=composite, component="upper")
    middle = ComponentView(composite=composite, component="middle")

    with localcontext(ENGINE_CONTEXT):
        for candle in _closes("3", "3", "5", "5"):
            # Both views are driven, as the overlay reader drives every channel it was handed.
            upper.update(candle)
            middle.update(candle)

        assert isinstance(upper, Indicator)
        assert upper.value() == Decimal("6")
        assert middle.value() == Decimal("4")


def test_the_composites_are_reachable_by_the_names_a_document_uses() -> None:
    """The registry is the contract between a stored document and this module.

    ⚠️ And the shape matters as much as the name: these two must arrive as *composites*, because
    the compiler decides whether a declaration becomes one channel or three by asking exactly
    this question. One arriving as a single-valued indicator would make `bb.upper` unresolvable
    while `bb` silently became the only channel.
    """
    built = {
        "BOLLINGER": {"period": 20, "deviations": 2},
        "ADX": {"period": 14},
    }
    for kind, params in built.items():
        name, indicator = build_indicator({"id": "x", "type": kind, "params": params})
        assert name == "x"
        assert isinstance(indicator, CompositeIndicator)
        assert not isinstance(indicator, Indicator)
        assert set(indicator.components()) == set(COMPOSITE_COMPONENTS[kind])


def test_bollinger_defaults_the_multiplier_and_the_source_the_way_the_schema_says() -> None:
    """A document may leave both out; the builder's defaults must match the schema's.

    Read through the registry rather than the constructor, because the registry is what a stored
    document actually travels through — and `deviations` is parsed through `str` there so that
    a JSON `2.5` becomes exactly 2.5 rather than its binary neighbour.
    """
    _, plain = build_indicator({"id": "bb", "type": "BOLLINGER", "params": {"period": 4}})
    _, spelled = build_indicator(
        {
            "id": "bb",
            "type": "BOLLINGER",
            "params": {"period": 4, "deviations": 2, "source": "close"},
        }
    )
    assert isinstance(plain, CompositeIndicator)
    assert isinstance(spelled, CompositeIndicator)

    candles = _closes("3", "3", "5", "5")
    assert _components(plain, candles)[-1] == _components(spelled, candles)[-1]

    _, fractional = build_indicator(
        {"id": "bb", "type": "BOLLINGER", "params": {"period": 4, "deviations": 2.5}}
    )
    assert isinstance(fractional, CompositeIndicator)
    # 2.5 exactly: the half-width is 2.5 times a deviation of 1, so 12.5 above a middle of 4.
    assert _components(fractional, candles)[-1]["upper"] == Decimal("6.5")


# --------------------------------------------------------------------------- #
# Properties of the composites — the class of bug a written scenario misses     #
# --------------------------------------------------------------------------- #


@st.composite
def _market(draw: st.DrawFn) -> list[Candle]:
    """A series of legal candles: `low <= open, close <= high`, open at the previous close.

    Built from a level plus a span rather than from four free numbers, so hypothesis spends its
    budget on shapes that could occur instead of on tuples the domain rejects outright. The length
    is drawn here rather than passed in, because a nested `@given` is quadratic in generation and
    shrinking — hypothesis refuses it, and it is right to.
    """
    candles: list[Candle] = []
    level = Decimal(100)
    for index in range(draw(st.integers(min_value=0, max_value=25))):
        span = draw(st.integers(min_value=0, max_value=8))
        low = level - Decimal(draw(st.integers(min_value=0, max_value=6)))
        high = low + Decimal(span)
        close = low + Decimal(draw(st.integers(min_value=0, max_value=span)))
        open_ = min(max(candles[-1].close if candles else close, low), high)
        candles.append(
            Candle(time=START + index * HOUR, open=open_, high=high, low=low, close=close)
        )
        level = close
    return candles


@given(period=st.integers(min_value=1, max_value=8), candles=_market())
def test_bollinger_bands_never_cross_and_warm_up_together(
    period: int, candles: list[Candle]
) -> None:
    """Three invariants that hold on every market, whatever the numbers.

    `lower <= middle <= upper` is the one worth a property: an ordering slip in the mapping, or a
    sign error on one side, produces bands that look like bands on most series and invert on the
    quiet ones. And the three components are `None` on exactly the same bars — a composite whose
    parts warmed up at different times would make `bb.upper` readable while `bb.middle` was not,
    and a rule comparing the two would silently be comparing against nothing.

    ⚠️ **The envelope is symmetric in arithmetic but not to the last digit, and this property is
    what established that.** It first asserted `upper - middle == middle - lower` exactly, and
    hypothesis produced a counterexample in two draws: half-widths of `0.94280904158206336586779203`
    and `...201`. One unit in the last place, and unavoidable — `middle + spread` and
    `middle - spread` each round independently at 28 significant digits, and no algebraic
    rearrangement removes that (unlike the EMA's increment form, which exists for the same reason
    and *can* be rearranged). It is 1e-26 on a price, so it cannot move a comparison at tick
    resolution; the bound below is what the implementation actually promises.
    """
    for values in _components(Bollinger(period=period, deviations=Decimal(2)), candles):
        present = [name for name, value in values.items() if value is not None]
        assert len(present) in (0, 3)
        if not present:
            continue
        lower = _number(values["lower"])
        middle = _number(values["middle"])
        upper = _number(values["upper"])
        assert lower <= middle <= upper
        above, below = upper - middle, middle - lower
        assert abs(above - below) <= max(abs(middle), Decimal(1)) * Decimal("1e-24")


@given(period=st.integers(min_value=1, max_value=8), candles=_market())
def test_adx_lines_stay_inside_their_own_ceiling(period: int, candles: list[Candle]) -> None:
    """`0 <= ADX <= 100`, both `DI`s non-negative, and — the interesting one — `+DI + -DI <= 100`.

    That last bound is not arbitrary. Only one directional movement can be non-zero on a bar, and
    it can never exceed that bar's true range: if the high rose by `up`, then `high - prev_close` is
    at least `up`, because the previous close cannot sit above the previous high. So `TR >= up`.
    Wilder smooths the `DM`s and the `TR` with the same weight, so the inequality survives the
    smoothing, and the two lines together cannot claim more movement than the market made.

    Which makes this the property that catches dividing by the wrong denominator — a `TR` seeded a
    bar earlier than the `DM`s, say, which is the mistake `ATR`'s own seeding invites. Lines that
    sum past 100 look perfectly ordinary one at a time.
    """
    for values in _components(ADX(period=period), candles):
        adx, plus, minus = values["adx"], values["plus_di"], values["minus_di"]
        if adx is not None:
            assert Decimal(0) <= adx <= Decimal(100)
        if plus is None or minus is None:
            assert plus is None
            assert minus is None
            continue
        assert plus >= Decimal(0)
        assert minus >= Decimal(0)
        assert plus + minus <= Decimal(100)
