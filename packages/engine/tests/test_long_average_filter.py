"""His long-average direction filter: only buy above it, only sell below it.

Dictated 2026-09-09 for the two setups of this module. The rule that needed a fixture to hold it
is his first answer: **what is compared is the price the order would enter at, not the bar's
close**. The bounce below is built so that one bar separates the two readings — bar 11 closes at
99 under a long average of 99.32 and breaks at 100 above it — because a filter written on the
close would skip exactly the entries that cross the line, and no assertion on an ordinary bar
would ever say so.

⚠️ **Every number here came from a probe of the real setups, not from arithmetic on paper**
(`docs/aulas/PR-207`). The falling stretch is what puts the long average above price; the bounce
is what crosses the fast one. Which bars arm, and what the two averages read on each, are the
probe's answers.

The Ponto Contínuo needs its own scenario rather than the same one: its rule 4 cancels a
qualification on any bar that closes below its average, so a straight downtrend can never qualify
it, and the pullback has to happen *above* the fast average while still under the long one.
"""

import datetime as dt
from decimal import Decimal, localcontext

import pytest

from tradeforge_engine.average_setups import AverageEntryPoint
from tradeforge_engine.bar_setups import is_force_bar, is_hammer
from tradeforge_engine.domain import Candle, Context, Side, Signal, SignalKind
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.swing import (
    LongAverageFilter,
    Mme9BreakoutStrategy,
    PontoContinuoStrategy,
)
from tradeforge_engine.testing import AAPL, HOUR, START, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()
_MIRROR_AXIS = Decimal(200)


def _index_of(candle: Candle) -> int:
    return round((candle.time - START) / HOUR)


def _mirror(candles: list[Candle]) -> list[Candle]:
    """Reflect about a price: the buy scenario becomes its sell twin, extremes swapped."""
    return [
        Candle(
            time=candle.time,
            open=_MIRROR_AXIS - candle.open,
            high=_MIRROR_AXIS - candle.low,
            low=_MIRROR_AXIS - candle.high,
            close=_MIRROR_AXIS - candle.close,
        )
        for candle in candles
    ]


def _drive(
    strategy: Mme9BreakoutStrategy | PontoContinuoStrategy, candles: list[Candle]
) -> dict[int, list[Signal]]:
    """Feed candles one at a time; the signals each bar produced, keyed by bar number."""
    out: dict[int, list[Signal]] = {}
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            context = Context(candle=candle, instrument=AAPL, account=_ACCOUNT)
            out[_index_of(candle)] = list(strategy.on_bar(context))
    return out


def _entries(signals: dict[int, list[Signal]]) -> dict[int, Signal]:
    return {
        index: signal
        for index, bar_signals in signals.items()
        for signal in bar_signals
        if signal.kind is SignalKind.ENTRY
    }


# A market that falls from 110 to 93 and then bounces. The fall is what lifts the long average
# above price; the bounce is what takes price back over the fast one. Bars 9 and 10 break under
# the long average, bar 11 breaks over it — and bar 11 is the bar that separates his rule from
# the obvious one, closing at 99 under a long average of 99.32 while breaking at 100 above it.
_BOUNCE = [
    bar(0, open_="110", close="109", high="110", low="108"),
    bar(1, open_="109", close="107", high="109", low="106"),
    bar(2, open_="107", close="105", high="107", low="104"),
    bar(3, open_="105", close="103", high="105", low="102"),
    bar(4, open_="103", close="101", high="103", low="100"),
    bar(5, open_="101", close="99", high="101", low="98"),
    bar(6, open_="99", close="97", high="99", low="96"),
    bar(7, open_="97", close="95", high="97", low="94"),
    bar(8, open_="95", close="93", high="95", low="92"),
    bar(9, open_="93", close="96", high="97", low="93"),  # crosses the fast average; breaks at 97
    bar(10, open_="96", close="97", high="98", low="95"),
    bar(11, open_="97", close="99", high="100", low="96"),  # close 99 < 99.32 < break 100
    bar(12, open_="99", close="98", high="99", low="97"),  # turn alive, break 99 under the average
    bar(13, open_="98", close="101", high="102", low="97"),
]

_FAST = 3
_LONG = 9
_FIRST_ALLOWED = 11
"""The bar the filter first lets through — read off the probe, not reasoned."""


# The same pullback shaped for the Ponto Contínuo, which cannot qualify inside a downtrend: bars
# 9 and 10 lift price over the fast average, 11 and 12 correct while still closing above it, and
# 13 touches it and closes back. The break at 98.5 is under a long average of 98.74.
_PULLBACK = [
    *_BOUNCE[:9],
    bar(9, open_="93", close="97", high="97.5", low="93"),
    bar(10, open_="97", close="98", high="98.5", low="96.5"),
    bar(11, open_="98", close="97.5", high="98", low="96"),  # correction 1
    bar(12, open_="97.5", close="97.4", high="97.9", low="95.9"),  # correction 2 -> qualified
    bar(13, open_="97", close="98", high="98.5", low="96.5"),  # touch + close above -> breaks 98.5
    bar(14, open_="98", close="99", high="99.5", low="97.5"),
]

# ⚠️ A second period, and it exists to separate one thing: on the pullback above, bar 13 closes
# at 98 and breaks at 98.5, and an EMA(8) reads 98.317 — *between* them. It is the only period of
# this fixture where his rule and the obvious one disagree, and without it the Ponto Contínuo's
# gate could be written on the close with nothing failing. Found by the guardian, not by design.
_LONG_BETWEEN = 8

# One bar past the pullback, for the order that must survive a blocked bar. Its high of 98.45 is
# under the EMA(8) of 98.4535 it closes on — a margin of a third of a tick, which is deliberate:
# the bar has to be blocked while still touching the fast average and closing above it, so that
# the setup has demonstrably not come apart and only the filter says no.
_HOLDS_THE_ORDER = bar(15, open_="98", close="98.4", high="98.45", low="96")

# And one more that clears it again, so the surviving order can be seen being replaced: a
# withdrawal names an order, and the name this one carries is bar 14's.
_CLEARS_AGAIN = bar(16, open_="98.4", close="99.2", high="99.6", low="97.5")

# Bar 13 again, reshaped into a hammer so the same pullback can be entered by a pattern: a long
# lower shadow from the open and a small upward body. Its order is one tick past the high.
_HAMMER = bar(13, open_="97.8", close="98", high="98.1", low="96.5")
_PULLBACK_HAMMER = [*_PULLBACK[:13], _HAMMER, _PULLBACK[14]]

# A bar of force after that hammer, so the pattern that rests a **limit** can be reached: the
# order lands between the two highs rather than past one of them, and `PatternOrder.stop_price`
# is `None` there. It replaces bar 14 rather than following it, because the pattern reads the
# bar immediately after the hammer.
_FORCE_AFTER_THE_HAMMER = bar(14, open_="98.2", close="99.4", high="99.6", low="98.1")


# --------------------------------------------------------------------------- #
# The filter on its own                                                         #
# --------------------------------------------------------------------------- #


def _warmed(period: int, side: Side = Side.LONG, *, closes: list[str]) -> LongAverageFilter:
    filter_ = LongAverageFilter(period=period, side=side)
    with localcontext(ENGINE_CONTEXT):
        for index, close in enumerate(closes):
            filter_.update(bar(index, open_=close, close=close))
    return filter_


def test_nothing_is_allowed_while_the_long_average_is_warming_up() -> None:
    """A reading of ours, and the one that fails visibly: with no value there is nothing to be
    above, and letting the entry through would trade the first bars of a 200-period filter
    unfiltered — the same run, quietly answering a different question."""
    filter_ = LongAverageFilter(period=3, side=Side.LONG)
    assert filter_.value() is None
    assert not filter_.allows(Decimal(1000))
    assert filter_.series() == ()


def test_the_entry_price_is_what_is_compared_and_not_the_close() -> None:
    """⚠️ His answer, and the one that inverts the obvious reading: *"a barra que fecha a mme9 pra
    cima, se a entrada ocorre acima da média longa já conta neste caso mesmo o fechamento sendo
    abaixo"*. A long average of 100 with a bar closing at 99 and breaking at 101 is a buy."""
    filter_ = _warmed(3, closes=["100", "100", "100"])
    assert filter_.value() == Decimal(100)

    assert filter_.allows(Decimal(101))
    # The close of that very bar, had the filter been written on it, would have refused the trade.
    assert not filter_.allows(Decimal(99))


def test_the_sell_side_is_the_mirror() -> None:
    filter_ = _warmed(3, Side.SHORT, closes=["100", "100", "100"])
    assert filter_.allows(Decimal(99))
    assert not filter_.allows(Decimal(101))


@pytest.mark.parametrize("side", [Side.LONG, Side.SHORT])
def test_an_entry_exactly_on_the_average_is_refused_on_both_sides(side: Side) -> None:
    """Strictly beyond, the reading the rest of this module already uses for "above the average".
    A reading of ours rather than his rule, and recorded in the backlog as one."""
    assert not _warmed(3, side, closes=["100", "100", "100"]).allows(Decimal(100))


def test_a_period_below_one_is_refused() -> None:
    with pytest.raises(ValueError, match="long average period must be >= 1"):
        LongAverageFilter(period=0, side=Side.LONG)


def test_the_filter_carries_its_own_curve_and_label() -> None:
    filter_ = _warmed(3, closes=["100", "101", "102", "103"])
    assert filter_.label == "long EMA 3"
    [series] = filter_.series()
    assert series.label == "long EMA 3"
    # Two points from four bars: the curve begins where the indicator did, and the warming-up
    # bars are skipped rather than recorded as holes (`_AverageTrail`). The seed is the mean of
    # the first three closes, and the fourth bar is the first the exponential rule touches.
    assert [str(point.value) for point in series.points] == ["101", "102.0"]


# --------------------------------------------------------------------------- #
# The MME9 under the filter                                                     #
# --------------------------------------------------------------------------- #


def test_without_the_filter_the_bounce_arms_from_the_bar_that_crosses() -> None:
    """The baseline every test below is read against: five arms, the first on the crossing bar,
    each one chasing the newest bar of the turn."""
    entries = _entries(_drive(Mme9BreakoutStrategy(period=_FAST), _BOUNCE))

    assert sorted(entries) == [9, 10, 11, 12, 13]
    assert str(entries[9].stop_price) == "97"


def test_the_filter_holds_the_turn_back_until_the_break_clears_the_long_average() -> None:
    """Bars 9 and 10 break at 97 and 98 under a long average of 100.0 and 99.40, so nothing is
    placed. Bar 11 breaks at 100 over 99.32 and is the first entry of the run."""
    strategy = Mme9BreakoutStrategy(period=_FAST, long_average_period=_LONG)
    signals = _drive(strategy, _BOUNCE)
    entries = _entries(signals)

    assert signals[9] == []
    assert signals[10] == []
    assert sorted(entries) == [_FIRST_ALLOWED, 13]
    assert str(entries[_FIRST_ALLOWED].stop_price) == "100"


def test_the_bar_that_arms_first_closes_below_the_long_average_it_broke_above() -> None:
    """⚠️ The fixture's whole point, and the reason it is this bounce and not another. Bar 11
    closes at 99 with the long average at 99.32 — a filter reading the close would refuse it —
    and breaks at 100, which is the price his rule looks at."""
    strategy = Mme9BreakoutStrategy(period=_FAST, long_average_period=_LONG)
    _drive(strategy, _BOUNCE[: _FIRST_ALLOWED + 1])

    long_average = strategy._long
    assert long_average is not None
    value = long_average.value()
    assert value is not None
    assert str(value)[:6] == "99.320"
    assert _BOUNCE[_FIRST_ALLOWED].close < value < Decimal(100)


def test_a_blocked_bar_leaves_the_resting_order_exactly_where_it_was() -> None:
    """*"Ela fica, só retira se o setup desconfigurar."* Bar 12 is still on the setup's side, so
    the turn stands, and its break at 99 is under the long average — the bar sends nothing at all.
    That the order was still resting is what bar 13's cancel proves: a withdrawal names an order,
    and the name it carries is bar 11's."""
    signals = _drive(Mme9BreakoutStrategy(period=_FAST, long_average_period=_LONG), _BOUNCE)

    assert signals[12] == []
    [cancelled, replaced] = signals[13]
    assert cancelled.kind is SignalKind.CANCEL
    assert cancelled.client_id == _entries(signals)[_FIRST_ALLOWED].client_id
    assert replaced.kind is SignalKind.ENTRY


def test_the_sell_side_of_the_mme9_mirrors_every_branch() -> None:
    """The reflection about 200 on a short instance. The numbers differ from the buy side's —
    entry 100 against a long average of 100.68, and this time the *close* sits above the long
    average while the entry sits below it — so a sign error cannot pass by symmetry."""
    mirrored = _mirror(_BOUNCE)
    signals = _drive(
        Mme9BreakoutStrategy(period=_FAST, side=Side.SHORT, long_average_period=_LONG), mirrored
    )
    entries = _entries(signals)

    assert sorted(entries) == [_FIRST_ALLOWED, 13]
    assert entries[_FIRST_ALLOWED].side is Side.SHORT
    assert str(entries[_FIRST_ALLOWED].stop_price) == "100"
    assert signals[12] == []


def test_the_entry_carries_the_long_average_as_a_number_and_as_a_curve() -> None:
    """Both, and neither derivable from the other: the scalar is what a later "does this only work
    far from the long average?" aggregates, the curve is what says whether price was running away
    from it or curling back."""
    strategy = Mme9BreakoutStrategy(period=_FAST, long_average_period=_LONG)
    entry = _entries(_drive(strategy, _BOUNCE))[_FIRST_ALLOWED]

    assert entry.context is not None
    assert set(entry.context) == {"average", "long_average"}
    long_average = entry.context["long_average"]
    assert long_average is not None
    assert str(long_average)[:6] == "99.320"
    assert [series.label for series in entry.series] == ["average", "long EMA 9"]


def test_the_entry_carries_no_long_average_when_the_filter_is_off() -> None:
    """A key that is absent says something a key that is present and empty does not."""
    entry = _entries(_drive(Mme9BreakoutStrategy(period=_FAST), _BOUNCE))[9]

    assert entry.context is not None
    assert set(entry.context) == {"average"}
    assert [series.label for series in entry.series] == ["average"]


def test_the_chart_is_offered_both_averages_only_when_the_filter_is_on() -> None:
    """A chart drawing only the MME9 would show entries being skipped with nothing on it to say
    why — which is the one question a reader of a filtered run has."""
    assert list(Mme9BreakoutStrategy(period=_FAST).overlays()) == ["EMA 3"]
    assert list(Mme9BreakoutStrategy(period=_FAST, long_average_period=_LONG).overlays()) == [
        "EMA 3",
        "long EMA 9",
    ]


def test_a_filter_of_the_setup_s_own_period_is_still_two_curves() -> None:
    """⚠️ The name is what keeps them apart. Filtering a nine-period MME9 with a nine-period
    average is a legal thing to ask for and a reasonable first experiment; under a shared label
    the second curve would replace the first in a mapping and a chart would draw one where two
    were meant — and the two being identical there is what would keep it from looking wrong."""
    assert list(Mme9BreakoutStrategy(period=9, long_average_period=9).overlays()) == [
        "EMA 9",
        "long EMA 9",
    ]


# --------------------------------------------------------------------------- #
# The Ponto Contínuo under the filter                                           #
# --------------------------------------------------------------------------- #


def test_the_ponto_continuo_arms_the_touch_without_the_filter() -> None:
    entries = _entries(_drive(PontoContinuoStrategy(period=_FAST), _PULLBACK))

    assert sorted(entries) == [13, 14]
    assert str(entries[13].stop_price) == "98.5"


def test_the_filter_blocks_the_touch_and_leaves_the_qualification_standing() -> None:
    """⚠️ The half that is easy to get wrong. Bar 13's break at 98.5 is under a long average of
    98.74, so nothing is placed — and the two corrections behind it are **not** spent: bar 14
    breaks at 99.5 over the average and arms, with no cancel before it because nothing had been
    placed to cancel. A filter that consumed the qualification would look identical on this bar
    and wrong on the next."""
    strategy = PontoContinuoStrategy(period=_FAST, long_average_period=_LONG)
    signals = _drive(strategy, _PULLBACK)

    assert signals[13] == []
    assert list(_entries(signals)) == [14]
    assert [signal.kind for signal in signals[14]] == [SignalKind.ENTRY]
    assert str(_entries(signals)[14].stop_price) == "99.5"


def test_the_touch_that_arms_the_ponto_continuo_may_close_below_the_average_it_broke_above() -> (
    None
):
    """⚠️ His answer 1 on this setup, and the two hosts do not share this path — only the pattern
    reconciliation is shared, so the MME9's twin of this test proves nothing here.

    Bar 13 closes at 98 and breaks at 98.5, with an EMA(8) at 98.317 between them. The break is
    what his rule looks at, so the setup arms; a gate written on the close would refuse exactly
    the entry that crosses the line, and the backtest would come out *worse* with no number
    looking wrong.
    """
    strategy = PontoContinuoStrategy(period=_FAST, long_average_period=_LONG_BETWEEN)
    entries = _entries(_drive(strategy, _PULLBACK))

    assert str(entries[13].stop_price) == "98.5"
    long_average = entries[13].context
    assert long_average is not None
    value = long_average["long_average"]
    assert value is not None
    assert _PULLBACK[13].close < value < Decimal("98.5")


def test_a_blocked_bar_leaves_the_ponto_continuo_s_resting_order_alone() -> None:
    """⚠️ His answer 2 on this setup: *"ela fica, só retira se o setup desconfigurar"*.

    Bar 14 arms at 99.5. Bar 15 touches the fast average and closes above it — the two corrections
    and the qualification are all still standing, so the setup has **not** come apart — and its
    break at 98.45 is under the long average of 98.4535. The bar must send nothing at all. With the
    gate one line lower, past the withdrawal, the filter would take that order back and the trade
    the resumption would have given never happens.
    """
    signals = _drive(
        PontoContinuoStrategy(period=_FAST, long_average_period=_LONG_BETWEEN),
        [*_PULLBACK, _HOLDS_THE_ORDER, _CLEARS_AGAIN],
    )
    armed_on_14 = _entries(signals)[14]
    assert str(armed_on_14.stop_price) == "99.5"

    assert signals[15] == []
    # The proof that it was still resting: bar 16 replaces it, and the cancel it sends names
    # bar 14's order. Had the filter withdrawn it on bar 15, there would be nothing to name.
    [cancelled, replaced] = signals[16]
    assert cancelled.kind is SignalKind.CANCEL
    assert cancelled.client_id == armed_on_14.client_id
    assert replaced.kind is SignalKind.ENTRY


def test_the_filter_gates_a_pattern_entry_by_the_price_the_pattern_would_enter_at() -> None:
    """The shared reconciliation is the second gate, and a hammer is what reaches it. The same
    pullback with bar 13 shaped as one arms at 98.11 — one tick past the hammer's high, its stop
    twenty percent of the bar — and that price is under the same long average of 98.74."""
    assert is_hammer(_HAMMER, side=Side.LONG)

    unfiltered = _entries(
        _drive(
            PontoContinuoStrategy(period=_FAST, entry_point=AverageEntryPoint.MARTELO),
            _PULLBACK_HAMMER,
        )
    )
    assert str(unfiltered[13].stop_price) == "98.11"
    assert str(unfiltered[13].stop_loss) == "96.18"

    filtered = _drive(
        PontoContinuoStrategy(
            period=_FAST, entry_point=AverageEntryPoint.MARTELO, long_average_period=_LONG
        ),
        _PULLBACK_HAMMER,
    )
    assert filtered[13] == []


def test_a_pattern_the_filter_allows_is_placed_unchanged() -> None:
    """The other side of the same gate: with a long average the break clears, the pattern's own
    order arrives exactly as it does with no filter at all. Only a value that must pass proves the
    gate opens — and the period is not the setup's own, or this would prove nothing about routing.
    """
    filtered = _entries(
        _drive(
            PontoContinuoStrategy(
                period=_FAST, entry_point=AverageEntryPoint.MARTELO, long_average_period=4
            ),
            _PULLBACK_HAMMER,
        )
    )
    assert str(filtered[13].stop_price) == "98.11"
    assert str(filtered[13].stop_loss) == "96.18"


def test_the_filter_reaches_the_mme9_s_pattern_path_too() -> None:
    """⚠️ The two hosts pass the filter into the shared reconciliation on **two different lines**,
    and only one of them was proven. Found by this PR's lesson, after both guardian rounds: a
    mutant forcing `long_average=None` on the MME9's call alone left all 1188 tests green.

    The same pullback, entered by the same hammer, on the MME9 instead: with a four-period filter
    the pattern's order is placed at 98.11, and with an eight-period one — whose average sits
    above that price — the bar sends nothing.
    """

    def arms(long_period: int | None) -> dict[int, Signal]:
        return _entries(
            _drive(
                Mme9BreakoutStrategy(
                    period=_FAST,
                    entry_point=AverageEntryPoint.MARTELO,
                    long_average_period=long_period,
                ),
                _PULLBACK_HAMMER,
            )
        )

    assert str(arms(None)[13].stop_price) == "98.11"
    assert str(arms(4)[13].stop_price) == "98.11"
    assert arms(_LONG_BETWEEN) == {}


def test_the_filter_reads_the_price_a_limit_pattern_rests_at() -> None:
    """The other shape a pattern's order takes, and the one the filter would fail on loudly rather
    than silently: `martelo_forca` rests a **limit**, so `PatternOrder.stop_price` is `None` and
    only `price` answers for both. A gate written against the stop alone raises here instead of
    comparing — which is why this is a test and not a comment.

    The hammer on bar 13 is followed by a bar of force on bar 14, and the limit lands at 98.55
    between their highs. A four-period filter allows it; an eight-period one does not.
    """

    def arms(long_period: int | None) -> dict[int, Signal]:
        return _entries(
            _drive(
                Mme9BreakoutStrategy(
                    period=_FAST,
                    entry_point=AverageEntryPoint.MARTELO_FORCA,
                    long_average_period=long_period,
                ),
                [*_PULLBACK_HAMMER[:14], _FORCE_AFTER_THE_HAMMER],
            )
        )

    assert is_force_bar(_FORCE_AFTER_THE_HAMMER, side=Side.LONG)
    placed = arms(4)[14]
    assert placed.limit_price is not None
    assert str(placed.limit_price) == "98.55"
    assert placed.stop_price is None, "the branch this test exists for: a limit, not a stop"
    assert str(arms(None)[14].limit_price) == "98.55"
    assert arms(_LONG_BETWEEN) == {}


def test_the_ponto_continuo_offers_both_averages_to_the_chart() -> None:
    assert list(PontoContinuoStrategy(period=20, average="SMA").overlays()) == ["SMA 20"]
    assert list(
        PontoContinuoStrategy(period=20, average="SMA", long_average_period=_LONG).overlays()
    ) == ["SMA 20", "long EMA 9"]


def test_the_filter_is_exponential_whatever_average_the_setup_counts_against() -> None:
    """`average` is the mean the corrections are counted against; the filter is a second question
    about direction, and his answer named an exponential one for it. An arithmetic setup with an
    exponential filter is a legal and meaningful combination, so the two must not share a knob."""
    strategy = PontoContinuoStrategy(period=3, average="SMA", long_average_period=3)
    _drive(strategy, _PULLBACK[:14])

    long_average = strategy._long
    assert long_average is not None
    assert long_average.label == "long EMA 3"
    # Bars 11, 12 and 13 close at 97.5, 97.4 and 98. Their arithmetic mean — what the setup's own
    # `SMA` would read — is 97.6333…; the exponential one the filter runs is 97.6625. Different
    # numbers, so a filter that quietly followed `average` would fail here.
    assert str(long_average.value())[:7] == "97.6625"
    assert str(sum(candle.close for candle in _PULLBACK[11:14]) / 3)[:7] == "97.6333"


def test_the_two_setups_default_to_no_filter_at_all() -> None:
    """Every recorded result predates this parameter, so its absence has to change nothing."""
    assert Mme9BreakoutStrategy(period=_FAST)._long is None
    assert PontoContinuoStrategy(period=_FAST)._long is None


def test_the_dates_line_up_with_the_bars_they_name() -> None:
    """A guard on the fixture rather than on the code: the tests above index by bar number, and a
    bar built with the wrong index would move every assertion silently."""
    assert _index_of(_BOUNCE[_FIRST_ALLOWED]) == _FIRST_ALLOWED
    assert _BOUNCE[_FIRST_ALLOWED].time == START + _FIRST_ALLOWED * HOUR
    assert isinstance(_BOUNCE[0].time, dt.datetime)
