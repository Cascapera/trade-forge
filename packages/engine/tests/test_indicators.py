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
from tradeforge_engine.indicators import ATR, EMA, RSI, SMA, Highest, Lowest, build_indicator
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

    with pytest.raises(KeyError):
        build_indicator({"id": "atr", "type": "ATR", "params": {"source": "close"}})
