"""The Ponto Contínuo: buy the bar that came back to the average, touched it, and closed above.

Like the MME9 suite next door, most of these drive the real `run()` loop rather than poking the
strategy bar by bar — a setup entered by a *stop* order only exists once the broker and the
anti-lookahead guard are in the picture. The rest are hand-driven, which is the only way to hold a
position still and read the stop levels the conduction asks for, and the only way to see the bars
that emit **nothing** — where every silent bug in this setup would live.

Every number here was measured by feeding these exact candles to the real indicators, structure
detector and strategy, and printing what came out. None of it is arithmetic done in this file. The
average is a 3 rather than the author's 20 so the warmup stays readable, and because a short EMA
moves far enough per bar to put a touch and a close on opposite sides of the line.
"""

from decimal import Decimal, localcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    ZERO,
    Candle,
    Context,
    Fill,
    Money,
    OrderRequest,
    Position,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.loop import ENGINE_CONTEXT, RunResult, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.swing import AverageKind, PontoContinuoStrategy
from tradeforge_engine.testing import AAPL, HOUR, START, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()


# --------------------------------------------------------------------------- #
# Harness                                                                       #
# --------------------------------------------------------------------------- #


def _run(  # noqa: PLR0913 — keyword-only; each names one axis of a backtest
    candles: list[Candle],
    *,
    side: Side = Side.LONG,
    rr: str | None = "5",
    period: int = 3,
    average: AverageKind = "EMA",
    stop_buffer_ticks: int = 0,
) -> RunResult:
    """The setup through the real loop. `rr=5` is the author's own target multiple."""
    strategy = PontoContinuoStrategy(
        side=side, period=period, average=average, stop_buffer_ticks=stop_buffer_ticks
    )
    broker = BacktestBroker(
        instrument=AAPL,
        initial_capital=Decimal(100_000),
        take_profit_rr=Decimal(rr) if rr is not None else None,
    )
    return run(
        candles=candles,
        timeframe=HOUR,
        instrument=AAPL,
        strategy=strategy,
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )


def _drive(  # noqa: PLR0913 — keyword-only; each names one axis of a driven run
    candles: list[Candle],
    *,
    side: Side = Side.LONG,
    period: int = 3,
    average: AverageKind = "EMA",
    stop_buffer_ticks: int = 0,
    breakeven_at_r: Decimal | None = Decimal(2),
    position_on: frozenset[int] = frozenset(),
    held: Position | None = None,
    fills_on: dict[int, list[Fill]] | None = None,
) -> list[list[Signal]]:
    """Feed candles one at a time and collect the signals each bar produced.

    `held` is what the conduction scenarios need: a position with known levels, because what the
    strategy computes is measured *from* them. Note the level never advances between bars here —
    the broker is not in the loop — so a scenario asking "what does this bar improve on" states the
    starting stop it wants rather than inheriting one.
    """
    fills_on = fills_on or {}
    strategy = PontoContinuoStrategy(
        side=side,
        period=period,
        average=average,
        stop_buffer_ticks=stop_buffer_ticks,
        breakeven_at_r=breakeven_at_r,
    )
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            position = None
            if index in position_on:
                position = held or Position(
                    symbol=AAPL.symbol,
                    side=side,
                    volume=Decimal(1),
                    entry_price=candle.open,
                    entry_time=candle.time,
                )
            out.append(
                list(
                    strategy.on_bar(
                        Context(
                            candle=candle,
                            instrument=AAPL,
                            account=_ACCOUNT,
                            position=position,
                            fills=tuple(fills_on.get(index, ())),
                        )
                    )
                )
            )
    return out


def _held(
    *,
    entry: str,
    stop: str | None,
    initial: str | None = None,
    side: Side = Side.LONG,
    at: int = 0,
) -> Position:
    """An open position with known levels. `initial` defaults to `stop`, which is what the ledger
    writes at the fill; they are handed separately because a conducted trade is exactly one where
    they have parted company, and the 2R line goes on being measured from the one that cannot move.
    """
    frozen = initial if initial is not None else stop
    return Position(
        symbol=AAPL.symbol,
        side=side,
        volume=Decimal(1),
        entry_price=Decimal(entry),
        entry_time=START + at * HOUR,
        stop_loss=Decimal(stop) if stop is not None else None,
        initial_stop_loss=Decimal(frozen) if frozen is not None else None,
    )


def _entries(result: RunResult) -> list[Fill]:
    return [f for f in result.fills if f.order.intent is SignalKind.ENTRY]


def _filled(candle: Candle, client_id: str, *, price: str = "114") -> Fill:
    """A fill carrying someone's name — this setup's own, or a stranger's."""
    order = OrderRequest(
        symbol=AAPL.symbol,
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(1),
        decided_at=candle.time,
        client_id=client_id,
    )
    return Fill(order=order, time=candle.time, price=Decimal(price), volume=Decimal(1), costs=ZERO)


def _arms(signals: list[list[Signal]]) -> list[tuple[int, Money | None, Money | None]]:
    """Every bar that armed an order, as `(bar, trigger, protective stop)`."""
    return [
        (index, s.stop_price, s.stop_loss)
        for index, per_bar in enumerate(signals)
        for s in per_bar
        if s.kind is SignalKind.ENTRY
    ]


def _cancels(signals: list[list[Signal]]) -> list[int]:
    return [
        index
        for index, per_bar in enumerate(signals)
        for s in per_bar
        if s.kind is SignalKind.CANCEL
    ]


def _trails(signals: list[list[Signal]]) -> list[list[Money | None]]:
    """The stop levels asked for on each bar. `None` is kept rather than filtered: a `MODIFY_STOP`
    carrying no level is a bug this helper would otherwise report as "no signal at all"."""
    return [
        [s.stop_loss for s in per_bar if s.kind is SignalKind.MODIFY_STOP] for per_bar in signals
    ]


# The warmup every long stream shares: closes 100, 104, 108 seed the EMA3 at 104 on bar 2.
_WARM = [
    bar(0, open_="99", close="100", high="100.5", low="98.5"),
    bar(1, open_="100", close="104", high="104.5", low="100"),
    bar(2, open_="104", close="108", high="108.5", low="104"),
]

# The golden. EMA3 per bar from bar 2: 104, 109, 111, 111.75, 112.625, 114.0625, 115.03125,
# 115.015625, 115.7578125, 116.87890625, 117.939453125, 117.9697265625, 117.73486328125,
# 118.867431640625, 117.1837158203125.
#
# bar 3 is a reference; bars 4 and 5 correct (each a lower high *and* a lower low); bar 6 touches
# the average at 111 and closes at 113.5 above it, so it is the setup — while making a *higher*
# high than bar 5, which is the whole point of the latch. The order rests at 114 and bar 7 takes it.
#
# Structure, from the strategy's own detector: bullish BOS on bar 7 (level 115, origin 107), a
# second on bar 15 (level 119.5, origin 116), and a bearish CHoCH on bar 16 (level 116).
_GOLDEN = [
    *_WARM,
    bar(3, open_="110", close="114", high="115", low="110"),  # reference
    bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
    bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
    bar(6, open_="112", close="113.5", high="114", low="111"),  # touch + close above -> arms 114
    bar(7, open_="114", close="115.5", high="116", low="113"),  # fills at 114; BOS #1 -> breakeven
    bar(8, open_="115.5", close="116", high="117", low="115"),
    bar(9, open_="116", close="115", high="116.5", low="114.2"),  # closes under the average
    bar(10, open_="115", close="116.5", high="117.5", low="115"),
    bar(11, open_="117", close="118", high="118.5", low="116.5"),
    bar(12, open_="118", close="119", high="119.5", low="117.5"),
    bar(13, open_="119", close="118", high="119", low="116.5"),
    bar(14, open_="118", close="117.5", high="118.5", low="116"),
    bar(15, open_="117.5", close="120", high="121", low="117"),  # BOS #2, origin 116 -> trails
    bar(16, open_="120", close="115.5", high="120.5", low="115"),  # takes the trailed stop
]

_FILL_BAR = 7
_HELD_IN_GOLDEN = frozenset(range(_FILL_BAR, len(_GOLDEN)))


# --------------------------------------------------------------------------- #
# The warmup: a qualification never outlives the rule that can kill it          #
# --------------------------------------------------------------------------- #


def test_nothing_arms_while_the_average_is_warming_up() -> None:
    assert _arms(_drive(_WARM)) == []


def test_corrections_before_the_average_exists_do_not_qualify_a_pullback() -> None:
    """Bars 1 and 2 are textbook corrections, and they must not count, because they happen where
    the *cancel* rule cannot run: closing below the average needs an average to close below.

    A pullback counted here would survive an arbitrarily long stretch of chart with nothing able to
    invalidate it, and be spent by the first touch after the warmup — bar 4, which touches at 108
    and closes at 113 above an average of 110.4. That entry is what this test forbids: with a
    period of 5 the average appears on bar 4, so counting from bar 1 would already read *qualified*
    by the time it does.
    """
    warmup_corrections = [
        bar(0, open_="110", close="110", high="115", low="109"),
        bar(1, open_="110", close="109", high="114", low="108"),  # correction, no average yet
        bar(2, open_="109", close="108", high="113", low="107"),  # correction, no average yet
        bar(3, open_="108", close="112", high="113", low="107.5"),
        bar(4, open_="112", close="113", high="114", low="108"),  # average appears: 110.4
        bar(5, open_="113", close="113.5", high="114.5", low="109"),
    ]
    assert _arms(_drive(warmup_corrections, period=5)) == []


# --------------------------------------------------------------------------- #
# The golden: entry, conduction, exit                                           #
# --------------------------------------------------------------------------- #


def test_a_long_enters_on_the_touch_that_closed_above_and_is_conducted_out() -> None:
    """The whole setup end to end, through the real loop.

    Bar 6 touches the average at 111 and closes at 113.5 above it, so the order rests at its high
    (114) protected at its low (111) — risk 3. Bar 7 trades through 114 and fills there, which is
    the anti-lookahead rule doing its job: the bar that *decided* cannot be the bar that fills.

    Then structure takes over. Bar 7 confirms the first bullish BOS of the trade, so the stop goes
    to the entry price. Bar 15 confirms a second, whose leg came from 116, so the stop goes there —
    now *above* the entry. Bar 16 trades down to 115 and takes it out at 116.

    The trade therefore ends at its stop **for a profit**: +$666.66, +0.67R, on a stop-out. That is
    not an anomaly to be smoothed over — it is what conducting a trade looks like in the ledger, and
    a reader of `reason == "sl"` who assumes a loss will misread this setup's every winning trade.
    """
    result = _run(_GOLDEN)

    (entry,) = _entries(result)
    assert entry.price == Decimal(114)
    assert entry.time == _GOLDEN[7].time

    (trade,) = result.trades
    assert trade.entry_price == Decimal(114)
    assert trade.exit_price == Decimal(116)
    assert trade.exit_time == _GOLDEN[16].time
    assert trade.reason == "sl"
    assert trade.net_pnl > ZERO
    assert trade.stop_loss == Decimal(111)  # the *initial* stop: what the lot was sized against


def test_the_target_is_not_what_ended_the_golden_trade() -> None:
    """Switching the broker's target off leaves the same trade, which is how we know the conduction
    ended it. At 5R the target sat at 129 and price never saw it."""
    with_target = _run(_GOLDEN, rr="5").trades
    without_target = _run(_GOLDEN, rr=None).trades
    assert [(t.exit_price, t.exit_time, t.reason) for t in with_target] == [
        (t.exit_price, t.exit_time, t.reason) for t in without_target
    ]


def test_the_order_rests_at_the_high_and_is_protected_at_the_low() -> None:
    assert _arms(_drive(_GOLDEN[:7])) == [(6, Decimal(114), Decimal(111))]


def test_the_entry_cannot_fill_on_the_bar_that_armed_it() -> None:
    """Bar 6's own high *is* the trigger, so a broker that filled on the deciding bar would report
    an entry at 06:00. The engine raises `LookaheadError` rather than allow it, and the fill lands
    on bar 7 — which this asserts by time, not by price, because the price is the same either way.
    """
    (entry,) = _entries(_run(_GOLDEN))
    assert entry.time == _GOLDEN[7].time
    assert entry.time > _GOLDEN[6].time


# --------------------------------------------------------------------------- #
# The qualification: two corrections, and the latch                             #
# --------------------------------------------------------------------------- #


def test_one_correction_does_not_qualify_a_pullback() -> None:
    """Bar 5 touches at 111 and closes at 113.5 above an average of 112.25 — a perfectly good
    trigger with only *one* correction behind it (bar 4). The author's rule asks for two."""
    one_correction = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1, and the only one
        bar(5, open_="113", close="113.5", high="115.5", low="111"),  # touch + close above
    ]
    assert _arms(_drive(one_correction)) == []


def test_a_correction_needs_a_lower_high_and_a_lower_low_both() -> None:
    """The conjunction, tested one half at a time — because dropping either half leaves a rule that
    still looks right and still passes every scenario above.

    Each stream below has exactly **one** real correction (bar 5) and one bar that satisfies only
    half the test (bar 4), followed by a complete trigger on bar 6: it touches the average and
    closes beyond it. All four must be silent, because one correction is not two.

    * An **inside bar** — lower high, *higher* low — is not a correction. Price did not go lower;
      the bar simply traded inside the one before it. Count it and a range contraction reads as a
      pullback.
    * An **outside bar** — higher high, lower low — is not one either. Price went further in *both*
      directions, which is the opposite of the orderly step-down the setup is looking for.

    The short streams mirror it: there a correction is a higher high **and** a higher low, so the
    outside bar is the one that fakes the high test and the inside bar fakes the low test. Both
    sides are written out because they are two separate expressions in the source.
    """
    long_inside = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="111"),  # inside bar: not a correction
        bar(5, open_="113", close="112.5", high="113", low="108"),  # the only real correction
        bar(6, open_="112", close="113.5", high="114", low="111"),  # a full trigger, unqualified
    ]
    long_outside = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="116", low="108"),  # outside bar: not a correction
        bar(5, open_="113", close="112.5", high="115", low="107"),  # the only real correction
        bar(6, open_="112", close="113.5", high="114", low="111"),  # a full trigger, unqualified
    ]
    assert _arms(_drive(long_inside)) == []
    assert _arms(_drive(long_outside)) == []

    warm_short = [
        bar(0, open_="121", close="120", high="121.5", low="119.5"),
        bar(1, open_="120", close="116", high="120", low="115.5"),
        bar(2, open_="116", close="112", high="116", low="111.5"),
    ]
    short_outside = [
        *warm_short,
        bar(3, open_="110", close="106", high="110", low="105"),  # reference
        bar(4, open_="106.5", close="107", high="112", low="104"),  # outside bar
        bar(5, open_="107", close="107.5", high="113", low="105"),  # the only real correction
        bar(6, open_="107", close="106.5", high="109", low="106"),  # a full trigger, unqualified
    ]
    short_inside = [
        *warm_short,
        bar(3, open_="110", close="106", high="110", low="105"),  # reference
        bar(4, open_="106.5", close="107", high="109", low="106"),  # inside bar
        bar(5, open_="107", close="107.5", high="110", low="107"),  # the only real correction
        bar(6, open_="107", close="106.5", high="109", low="106"),  # a full trigger, unqualified
    ]
    assert _arms(_drive(short_outside, side=Side.SHORT)) == []
    assert _arms(_drive(short_inside, side=Side.SHORT)) == []


def test_two_corrections_must_be_consecutive() -> None:
    """*Consecutive* is the word in the rule, and this is what enforces it: a bar that is not a
    correction resets the count to zero, it does not merely fail to add to it.

    Bar 4 corrects. Bar 5 is an inside bar — lower high, higher low — so the streak is broken. Bar 6
    corrects again. That is two corrections in the stream and **one** in a row, so bar 7's complete
    trigger must find nothing: it touches 111 against an average of 112.625 and closes at 113.5
    above it, and stays silent.

    Without the reset the two scattered corrections add up and bar 7 buys — a whole trade taken on a
    pullback that never happened. The count is only ever observable when it is already non-zero,
    which is why every other test in this file misses this: their streams are all at zero when it
    matters.
    """
    scattered = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction
        bar(5, open_="112.8", close="112", high="113", low="109"),  # inside bar: breaks the streak
        bar(6, open_="112", close="112", high="112.5", low="108.5"),  # correction, and the first
        bar(7, open_="112", close="113.5", high="114", low="111"),  # a full trigger, unqualified
    ]
    assert _arms(_drive(scattered)) == []


def test_both_comparisons_in_a_correction_are_strict() -> None:
    """Four comparisons, four equalities, and none of them is a correction.

    A double top prints the same high twice; a shelf prints the same low twice. Neither is price
    stepping *down*, so neither continues a pullback — the streak breaks. Each stream below has one
    real correction (bar 4), one bar that ties on exactly one edge (bar 5), and a complete trigger
    on bar 6, so all four must be silent. Relax any single comparison to `<=` (or `>=` on the short)
    and that stream's bar 6 buys.

    This is what the conjunction test above cannot reach: that one asks whether *both* halves are
    required, this one whether either is strict. They fail to different mutations, so they are
    different tests.
    """
    warm_short = [
        bar(0, open_="121", close="120", high="121.5", low="119.5"),
        bar(1, open_="120", close="116", high="120", low="115.5"),
        bar(2, open_="116", close="112", high="116", low="111.5"),
    ]
    long_equal_high = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction
        bar(5, open_="113", close="112.5", high="114", low="107"),  # high ties, low is lower
        bar(6, open_="112", close="113.5", high="114", low="111"),  # a full trigger, unqualified
    ]
    long_equal_low = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction
        bar(5, open_="113", close="112.5", high="113.5", low="108"),  # low ties, high is lower
        bar(6, open_="112", close="113.5", high="114", low="111"),  # a full trigger, unqualified
    ]
    short_equal_high = [
        *warm_short,
        bar(3, open_="110", close="106", high="110", low="105"),  # reference
        bar(4, open_="106.5", close="107", high="112", low="106"),  # correction (mirrored)
        bar(5, open_="107", close="107.5", high="112", low="106.5"),  # high ties, low is higher
        bar(6, open_="108", close="106.5", high="109", low="106"),  # a full trigger, unqualified
    ]
    short_equal_low = [
        *warm_short,
        bar(3, open_="110", close="106", high="110", low="105"),  # reference
        bar(4, open_="106.5", close="107", high="112", low="106"),  # correction (mirrored)
        bar(5, open_="107", close="107.5", high="113", low="106"),  # low ties, high is higher
        bar(6, open_="108", close="106.5", high="109", low="106"),  # a full trigger, unqualified
    ]
    assert _arms(_drive(long_equal_high)) == []
    assert _arms(_drive(long_equal_low)) == []
    assert _arms(_drive(short_equal_high, side=Side.SHORT)) == []
    assert _arms(_drive(short_equal_low, side=Side.SHORT)) == []


def test_the_triggering_bar_need_not_be_a_correction_itself() -> None:
    """Bar 6 of the golden makes a *higher* high than bar 5 (114 against 113.5), so it is not a
    correction — and it is still the setup. His answer, and the geometry `MarketStructure` already
    reads the same way: two corrections *arm* the pullback, and the arming stands.

    Requiring the trigger to continue the run would discard exactly the bars worth trading: a bar
    that takes the average back usually prints above the one before it.
    """
    assert _GOLDEN[6].high > _GOLDEN[5].high  # the bar that arms is not a correction
    assert _arms(_drive(_GOLDEN[:7])) == [(6, Decimal(114), Decimal(111))]


def test_a_plain_bar_may_sit_between_the_correction_and_the_touch() -> None:
    """Bar 6 here is neither a correction (higher high) nor a trigger (its low of 113 never reaches
    the average at 112.875). The qualification survives it, and bar 7 — also not a correction —
    touches at 112.5 and closes at 114.5 above 113.6875, arming 115.5/112.50."""
    gap = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
        bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
        bar(6, open_="113", close="114", high="115", low="113"),  # neither: higher high, no touch
        bar(7, open_="114", close="114.5", high="115.5", low="112.5"),  # the touch, a bar later
    ]
    assert _arms(_drive(gap)) == [(7, Decimal("115.5"), Decimal("112.50"))]


# --------------------------------------------------------------------------- #
# The trigger: touch *and* close beyond, with the tie belonging to neither      #
# --------------------------------------------------------------------------- #


def test_a_qualified_bar_that_closes_above_without_touching_arms_nothing() -> None:
    """Bar 6 is qualified and closes at 125.5 above the average at 124.125, but its low of 124.5
    never reaches it. The setup is price coming *back* to the average; a bar that stayed clear of
    it did not come back."""
    never_touched = [
        bar(0, open_="99", close="100", high="100.5", low="98.5"),
        bar(1, open_="100", close="110", high="110.5", low="100"),
        bar(2, open_="110", close="120", high="120.5", low="110"),  # EMA3 seeds at 110
        bar(3, open_="120", close="126", high="127", low="119"),  # reference
        bar(4, open_="126", close="125", high="126", low="118"),  # correction 1
        bar(5, open_="125", close="124", high="125.5", low="117"),  # correction 2 -> qualified
        bar(6, open_="125", close="125.5", high="126", low="124.5"),  # low stays above 124.125
    ]
    assert _arms(_drive(never_touched)) == []


def test_the_touch_may_pierce_the_average() -> None:
    """Bar 6 of the golden reaches 111 against an average of 112.625 — a full point and a half
    *through* it — and that is a touch. His rule says so explicitly: the touch may transpass. What
    matters is where the bar closed, not how far under it traded first."""
    assert _GOLDEN[6].low < Decimal("112.625")
    assert _arms(_drive(_GOLDEN[:7])) == [(6, Decimal(114), Decimal(111))]


def test_reaching_the_average_to_the_cent_is_a_touch() -> None:
    """The other boundary, and the opposite of the closing one: *touching* is inclusive. A bar whose
    low lands exactly on the average did come back to it, so it triggers.

    The long's bar 6 bottoms at 113 against an average of 113 exactly; the short's tops at 107.125
    against 107.125. Both sides are stated because they are two separate comparisons in the source,
    and a `<=` narrowed to `<` on either one would drop the setups that touch the line cleanly —
    silently, and only on the bars where the method was most precisely satisfied.
    """
    long_stream = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
        bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
        bar(6, open_="113.5", close="114.25", high="115", low="113"),  # low == average == 113
    ]
    assert _arms(_drive(long_stream)) == [(6, Decimal(115), Decimal(113))]

    short_stream = [
        bar(0, open_="121", close="120", high="121.5", low="119.5"),
        bar(1, open_="120", close="116", high="120", low="115.5"),
        bar(2, open_="116", close="112", high="116", low="111.5"),
        bar(3, open_="110", close="106", high="110", low="105"),  # reference
        bar(4, open_="106.5", close="107", high="112", low="106"),  # correction 1 (mirrored)
        bar(5, open_="107", close="107.5", high="113", low="106.5"),  # correction 2 -> qualified
        bar(6, open_="107", close="106", high="107.125", low="105.5"),  # high == average
    ]
    assert _arms(_drive(short_stream, side=Side.SHORT)) == [
        (6, Decimal("105.5"), Decimal("107.125"))
    ]


def test_a_bar_closing_exactly_on_the_average_is_neither_trigger_nor_cancel() -> None:
    """The one tie in this setup, and it belongs to neither rule: *above* triggers and *below*
    cancels, both stated strictly, so a close landing on the line does nothing at all.

    Bar 6 touches at 110 and closes at 111.75, which is the average to the cent — an EMA whose
    alpha is 1/2 does not move when the close equals it. Two mutants die here. Read the trigger as
    "did not close below" and bar 6 arms; read the cancel as "did not close above" and bar 6 burns
    the qualification, so bar 7 finds nothing to trade. What must happen is bar 6 in silence and
    bar 7 arming, which is only true if the two comparisons are independent rather than each
    other's negation.

    Note this is deliberately *not* how the MME9 breakout reads the same tie: there anything short
    of a close above ends the turn, because there the average's side is the entire signal.
    """
    on_the_line = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
        bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
        bar(6, open_="112", close="111.75", high="113", low="110"),  # closes ON 111.75
        bar(7, open_="112", close="113.5", high="114", low="111"),  # the real trigger
    ]
    signals = _drive(on_the_line)
    assert _arms(signals) == [(7, Decimal(114), Decimal(111))]
    assert _cancels(signals) == []


# --------------------------------------------------------------------------- #
# The cancel: a close below takes the order and the qualification with it       #
# --------------------------------------------------------------------------- #


def test_a_close_below_the_average_withdraws_the_resting_order() -> None:
    """Bar 6 arms; bar 7 closes at 110 under an average of 112.375, so the order comes back. Only
    the strategy can know the setup is gone, which is why `Broker.cancel` exists — and the cancel
    is applied the same bar it is decided, before the next bar can fill it."""
    cancelled = [
        *_GOLDEN[:7],
        bar(7, open_="113", close="110", high="113.5", low="109.5"),  # closes below -> withdraw
    ]
    signals = _drive(cancelled)
    assert _arms(signals) == [(6, Decimal(114), Decimal(111))]
    assert _cancels(signals) == [7]


def test_a_close_below_the_average_also_burns_the_qualification() -> None:
    """His answer, and the test that pins it: the cancel is the end of the *setup*, not just of the
    order.

    Bar 6 closes at 110 under the average at 110.875. Bar 7 then touches at 109.5 and closes at 113
    above 111.9375 — a complete trigger — and it must be met with **silence**, because the two
    corrections that qualified the pullback died with bar 6. Bars 8 and 9 correct again, and only
    then does bar 10 arm at 114/111.

    Cancel the order alone and bar 7 buys, which is the reading this rejects: it would enter after
    price had already given up the average, on the strength of a pullback that preceded the giving
    up. The bars are the same either way; only the trade differs.
    """
    burned = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
        bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
        bar(6, open_="112", close="110", high="112.5", low="109"),  # closes below -> burns it
        bar(7, open_="110", close="113", high="113.5", low="109.5"),  # a full trigger: silence
        bar(8, open_="113", close="112.5", high="113", low="109"),  # fresh correction 1
        bar(9, open_="112.5", close="112.5", high="112.8", low="108.5"),  # fresh correction 2
        bar(10, open_="112", close="113.5", high="114", low="111"),  # touch + close above -> arms
    ]
    assert _arms(_drive(burned)) == [(10, Decimal(114), Decimal(111))]


def test_a_cancel_makes_the_next_entry_wait_for_two_fresh_corrections() -> None:
    """ "Two fresh corrections" counted from **zero**, which is a stricter claim than the test above
    and needs its own stream.

    That one only shows the *latch* being burned, because the bar right after its cancel is not a
    correction — so the count happens to be reset by the ordinary streak rule, and a cancel that
    cleared the latch alone would pass it. Here bar 7 **is** a correction, so the two resets are
    told apart: the count must be at 1 after it, not at 3.

    Bars 4 and 5 correct. Bar 6 closes at 110 under the average at 110.875 and ends the setup.
    Bar 7 corrects — the first of the new pullback. Bar 8 is then a complete trigger, touching 110
    against 112.21875 and closing at 113.5 above it, met with silence: one correction is not two.
    """
    cancelled_then_one = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),  # reference
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
        bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
        bar(6, open_="112", close="110", high="112.5", low="109"),  # closes below -> ends the setup
        bar(7, open_="110", close="111", high="112", low="108"),  # the *first* fresh correction
        bar(8, open_="111", close="113.5", high="114", low="110"),  # a full trigger, unqualified
    ]
    assert _arms(_drive(cancelled_then_one)) == []


# --------------------------------------------------------------------------- #
# One trade at a time, and the pullback is spent by the fill                    #
# --------------------------------------------------------------------------- #


def test_an_unfilled_order_follows_the_latest_qualifying_bar() -> None:
    """Bars 6, 7 and 8 each touch and close above, and each replaces the order on its own high —
    the swing family's "the last bar always counts". Nothing here is filled: the account is held
    flat by hand, which is the only way to see the third rearm.

    In a real run this can only ever move the trigger *down*. A later bar whose high cleared the
    resting trigger would have filled it on the way through, so the strategy would be looking at a
    position instead of a candidate.
    """
    signals = _drive(_GOLDEN[:9])
    assert _arms(signals) == [
        (6, Decimal(114), Decimal(111)),
        (7, Decimal(116), Decimal(113)),
        (8, Decimal(117), Decimal(115)),
    ]
    # Each rearm takes its predecessor back first: three orders, two withdrawals.
    assert _cancels(signals) == [7, 8]


def test_every_rearm_gets_a_name_of_its_own() -> None:
    """The counter, not the timestamp, is what makes an order's name unique. Below M1 two
    references share a minute, and a repeated name is one the broker already holds — the second
    order would be refused in silence while the strategy believed itself armed."""
    ids = [
        s.client_id
        for per_bar in _drive(_GOLDEN[:9])
        for s in per_bar
        if s.kind is SignalKind.ENTRY
    ]
    assert len(ids) == len(set(ids)) == 3


def test_an_open_position_arms_nothing() -> None:
    """Bar 6 is a complete setup and the account is not flat, so there is nothing to arm. The
    strategy never adds to a position."""
    assert _arms(_drive(_GOLDEN[:7], position_on=frozenset(range(len(_GOLDEN[:7]))))) == []


def test_the_fill_spends_the_pullback() -> None:
    """One qualification, one trade. Bar 6 arms; bar 7 shows the fill, and from there the pullback
    is spent — so when the account is flat again on bar 8, a bar that touches and closes above
    finds nothing to act on until two fresh corrections have run.

    Nothing in his rule says a pullback may be traded twice: entries repeat and re-arm once a trade
    ends, which is a statement about *trades*, not about re-using one setup. Every other setup in
    this engine spends its trigger at the fill for the same reason.
    """
    own = _filled(_GOLDEN[7], "ponto-continuo-20240101T0600-1")
    signals = _drive(_GOLDEN[:9], fills_on={7: [own]})
    # Bar 8 touches and closes above and would otherwise arm — see the rearm test above.
    assert _arms(signals) == [(6, Decimal(114), Decimal(111))]


def test_only_a_fill_carrying_the_armed_name_spends_the_pullback() -> None:
    """A stranger's fill on the same bar is not this setup's trade. The order goes on resting, and
    bar 7 rearms to its own high exactly as it would have."""
    stranger = _filled(_GOLDEN[7], "someone-else-1")
    assert _arms(_drive(_GOLDEN[:8], fills_on={7: [stranger]})) == [
        (6, Decimal(114), Decimal(111)),
        (7, Decimal(116), Decimal(113)),
    ]


def test_an_open_position_is_the_fallback_sign_of_a_fill() -> None:
    """No fill is shown on bar 7, only a position. That is enough: the order is not resting any
    more, so the name is forgotten (no cancel is sent for it) and the pullback is spent."""
    signals = _drive(_GOLDEN[:9], position_on=frozenset({7}))
    assert _arms(signals) == [(6, Decimal(114), Decimal(111))]
    assert _cancels(signals) == []


def test_a_trade_that_opened_and_died_inside_one_bar_still_spends_the_pullback() -> None:
    """The fill that matters most is the one no position survives to report: entered and stopped
    inside a single bar. `Context.fills` is the only channel that carries it (ADR-0015), which is
    why the strategy reads both and not just the position."""
    own = _filled(_GOLDEN[7], "ponto-continuo-20240101T0600-1")
    signals = _drive(_GOLDEN[:9], fills_on={7: [own]})
    assert _arms(signals) == [(6, Decimal(114), Decimal(111))]
    assert _cancels(signals) == []


def test_a_pullback_that_completes_while_the_trade_runs_is_a_real_pullback() -> None:
    """Corrections are geometry, and the chart does not stop making them because we are positioned.

    Bar 6 arms and bar 7 fills, which spends that pullback. Bars 8 and 9 then correct *inside* the
    trade — each a lower high and a lower low, each closing above the average — so by the time bar
    10 finds the account flat there is a live qualification for it to act on, and it arms at
    116.5/114.

    Counting only while flat would swallow every setup that formed inside a trade, and swallow it in
    silence: no number comes out wrong, trades simply stop appearing.
    """
    mid_trade = [
        *_GOLDEN[:8],  # through the fill on bar 7
        bar(8, open_="115.5", close="115.5", high="115.8", low="112"),  # correction 1, in trade
        bar(9, open_="115.4", close="115.4", high="115.6", low="111"),  # correction 2, in trade
        bar(10, open_="115", close="116", high="116.5", low="114"),  # flat again: touch + close up
    ]
    signals = _drive(
        mid_trade,
        position_on=frozenset({7, 8, 9}),
        held=_held(entry="114", stop="111", at=7),
    )
    assert _arms(signals) == [
        (6, Decimal(114), Decimal(111)),
        (10, Decimal("116.5"), Decimal("114.00")),
    ]


# --------------------------------------------------------------------------- #
# The conduction: structure moves the stop, and the average has no say         #
# --------------------------------------------------------------------------- #


def test_the_first_break_of_structure_takes_the_trade_to_breakeven() -> None:
    """Bar 7 confirms the trade's first bullish BOS, and the stop goes to the entry price — not to
    the broken level, and not to the leg origin. Only the *first* break means breakeven."""
    trails = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="111", at=7),
            breakeven_at_r=None,
        )
    )
    assert trails[7] == [Decimal(114)]


def test_a_later_break_trails_the_stop_to_its_leg_origin() -> None:
    """Bar 15 is the trade's second BOS. Its leg came from 116, so that is where the stop goes —
    *above* the entry price of 114, which is how a conducted trade ends up stopping out at a
    profit. Behind the broken level (119.5) the stop would sit inside the move it is riding; behind
    the origin it sits where the trend would be disproven.
    """
    trails = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="111", at=7),
            breakeven_at_r=None,
        )
    )
    assert trails[15] == [Decimal(116)]


def test_a_break_whose_origin_would_loosen_the_stop_is_not_sent() -> None:
    """With the stop already at breakeven, bar 7's own candidate (the entry price, 114) is no
    longer an improvement and nothing is sent — a re-send would restamp `stop_decided_at` on a bar
    that decided nothing. Bar 15's origin of 116 does improve, so that one goes.

    Silence here is the *engine's* rule showing through: a loosening raises (ADR-0018), so the
    strategy's whole job is to not ask.
    """
    trails = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="114", initial="111", at=7),
        )
    )
    assert trails[7] == []
    assert trails[15] == [Decimal(116)]
    assert [t for t in trails if t] == [[Decimal(116)]]


def test_a_change_of_character_against_the_trade_conducts_nothing() -> None:
    """Bar 16 confirms a bearish CHoCH while a long is open. It is not a break in the trade's
    favour, so it names no level — with the 2x1 rule switched off, bar 16 is silent."""
    trails = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="111", at=7),
            breakeven_at_r=None,
        )
    )
    assert trails[16] == []


def test_the_two_by_one_rule_lives_alongside_the_structural_trail() -> None:
    """Both rules are live from the fill and neither arbitrates: the entry risk is 3, so 2R sits at
    120, and bar 16's high of 120.5 reaches it. With structure silent on that bar (its CHoCH is
    against the trade), the level that comes out is the 2x1's — the entry price.

    Switch the multiple off and bar 16 says nothing at all, which is what tells the two rules
    apart: the same bar, the same structure, and a signal that exists only because of the knob.
    """
    with_2x1 = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="111", at=7),
            breakeven_at_r=Decimal(2),
        )
    )
    without = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="111", at=7),
            breakeven_at_r=None,
        )
    )
    assert with_2x1[16] == [Decimal(114)]
    assert without[16] == []


def test_a_bar_that_closes_below_the_average_still_conducts_the_trade() -> None:
    """Bar 16 closes at 115.5, under the average at 117.18 — the bar that ends the setup. It still
    owes the trade a tightening, and the ordering inside `on_bar` is what guarantees it: the
    conduction is computed *before* the branch that cancels, so the early return cannot swallow it.

    Get that ordering wrong and the loss is invisible: the trade goes on with a stop that should
    have moved, on exactly the bars where price turned against it.
    """
    trails = _trails(
        _drive(_GOLDEN, position_on=_HELD_IN_GOLDEN, held=_held(entry="114", stop="111", at=7))
    )
    average_at_16 = Decimal("117.1837158203125")
    assert _GOLDEN[16].close < average_at_16
    assert trails[16] == [Decimal(114)]


def test_the_conduction_reads_the_bar_that_filled() -> None:
    """Bar 7 both fills the order and confirms the first BOS, and the stop moves on that same bar.

    The structure family has to sit *out* the bar it filled on, because a limit order fills where
    price came back to and the favourable excursion can be entirely before the fill. A stop order
    is the opposite: it fills going *through* its trigger in the direction of travel, so the bar's
    favourable extreme is necessarily after the fill and reading it credits nothing the trade did
    not live through. This setup therefore needs no such guard, and this test is what would notice
    if one were copied across.
    """
    trails = _trails(
        _drive(
            _GOLDEN,
            position_on=_HELD_IN_GOLDEN,
            held=_held(entry="114", stop="111", at=7),
            breakeven_at_r=None,
        )
    )
    assert trails[_FILL_BAR] == [Decimal(114)]


def test_nothing_is_conducted_while_the_account_is_flat() -> None:
    assert [t for t in _trails(_drive(_GOLDEN)) if t] == []


@pytest.mark.parametrize("average", ["EMA", "SMA"])
def test_an_average_of_one_bar_can_never_be_closed_beyond(average: AverageKind) -> None:
    """A period of 1 is legal and it degenerates: the mean of one close *is* that close, so every
    bar closes exactly on the average and the tie belongs to neither rule. Nothing ever triggers and
    nothing ever cancels.

    It is also the only configuration whose average exists on the very first bar — there is no
    previous candle to measure a correction against — so this is the case that reaches the code path
    where the correction geometry has nothing to compare with. A period of 1 that armed anything
    would mean the tie had quietly become an entry, on every bar of every run.
    """
    signals = _drive(_GOLDEN, period=1, average=average)
    assert _arms(signals) == []
    assert _cancels(signals) == []


# --------------------------------------------------------------------------- #
# The short mirror                                                              #
# --------------------------------------------------------------------------- #


def test_a_short_enters_on_the_break_of_the_low_that_touched_the_average() -> None:
    """Every line of the rule mirrors: a correction is a *higher* high and a *higher* low, the
    touch is the high reaching up to the average, the trigger closes *below* it, and the order
    rests at the bar's low protected at its high.

    Bar 6 reaches 109 against an average of 107.375 and closes at 106.5 below it, so the order
    rests at 106 with its stop at 109 — and bar 6's high of 109 is *lower* than bar 5's 113, so it
    is not a mirrored correction either. The latch holds on this side too.
    """
    short = [
        bar(0, open_="121", close="120", high="121.5", low="119.5"),
        bar(1, open_="120", close="116", high="120", low="115.5"),
        bar(2, open_="116", close="112", high="116", low="111.5"),  # EMA3 seeds at 116
        bar(3, open_="110", close="106", high="110", low="105"),  # reference
        bar(4, open_="106.5", close="107", high="112", low="106"),  # correction 1 (mirrored)
        bar(5, open_="107", close="107.5", high="113", low="106.5"),  # correction 2 -> qualified
        bar(6, open_="108", close="106.5", high="109", low="106"),  # touch + close below
    ]
    assert _arms(_drive(short, side=Side.SHORT)) == [(6, Decimal(106), Decimal(109))]


# --------------------------------------------------------------------------- #
# The average is a knob: exponential or arithmetic, and the period              #
# --------------------------------------------------------------------------- #


def test_which_average_is_chosen_changes_which_bars_qualify() -> None:
    """The same candles, read against the two means, do not produce the same trade — which is what
    proves the choice is wired through rather than decorative.

    The exponential mean is the default and arms on bar 6. The arithmetic one of the same period
    lags further behind price, so it is still *above* bar 5's close: that bar closes below the
    average, which burns the qualification, and nothing is ever armed.
    """
    same = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),
        bar(4, open_="113.5", close="113", high="114", low="108"),
        bar(5, open_="113", close="112.5", high="113.5", low="107"),
        bar(6, open_="112.5", close="113", high="114.5", low="112"),
        bar(7, open_="113", close="114", high="114.5", low="112"),
    ]
    assert _arms(_drive(same, average="EMA")) == [
        (6, Decimal("114.5"), Decimal("112.00")),
        (7, Decimal("114.5"), Decimal("112.00")),
    ]
    assert _arms(_drive(same, average="SMA")) == []


def test_the_arithmetic_average_finds_setups_of_its_own() -> None:
    """The other half of the pair: the SMA is not merely a silent option. On its own candles it
    arms at 111/108 — bar 5 closes at 109.8 above a mean of 109.6, bar 6 touches 108 and closes at
    110.5 above 109.7667."""
    sma_stream = [
        bar(0, open_="99", close="100", high="100.5", low="98.5"),
        bar(1, open_="100", close="102", high="102.5", low="100"),
        bar(2, open_="102", close="104", high="104.5", low="102"),  # SMA3 = 102
        bar(3, open_="104", close="110", high="111", low="104"),  # reference
        bar(4, open_="110", close="109", high="110.5", low="103"),  # correction 1
        bar(5, open_="109", close="109.8", high="110", low="102"),  # correction 2 -> qualified
        bar(6, open_="110", close="110.5", high="111", low="108"),  # touch + close above
    ]
    assert _arms(_drive(sma_stream, average="SMA")) == [(6, Decimal(111), Decimal(108))]


# --------------------------------------------------------------------------- #
# The stop buffer, and the bars that arm nothing                                #
# --------------------------------------------------------------------------- #


def test_the_stop_buffer_pushes_the_stop_past_the_reference_low() -> None:
    """Two ticks of a one-cent instrument put the stop at 110.98 rather than on 111. Zero is the
    author's literal rule — the stop *is* the bar's low — and the knob exists because a stop
    sitting on the low is taken out by the noise of the bar that set it."""
    assert _arms(_drive(_GOLDEN[:7], stop_buffer_ticks=2)) == [(6, Decimal(114), Decimal("110.98"))]


def test_the_stop_buffer_mirrors_above_the_high_on_a_short() -> None:
    short = [
        bar(0, open_="121", close="120", high="121.5", low="119.5"),
        bar(1, open_="120", close="116", high="120", low="115.5"),
        bar(2, open_="116", close="112", high="116", low="111.5"),
        bar(3, open_="110", close="106", high="110", low="105"),
        bar(4, open_="106.5", close="107", high="112", low="106"),
        bar(5, open_="107", close="107.5", high="113", low="106.5"),
        bar(6, open_="108", close="106.5", high="109", low="106"),
    ]
    assert _arms(_drive(short, side=Side.SHORT, stop_buffer_ticks=3)) == [
        (6, Decimal(106), Decimal("109.03"))
    ]


def test_a_reference_bar_with_no_range_arms_nothing_and_keeps_the_qualification() -> None:
    """A bar whose high and low are one price is no reference: the trigger would sit on the stop and
    the trade would carry no risk. The pullback is untouched by it — this bar failed to *be* a
    setup, it did not destroy one — so the next bar with a range arms normally."""
    flat_trigger = [
        *_WARM,
        bar(3, open_="110", close="114", high="115", low="110"),
        bar(4, open_="113.5", close="113", high="114", low="108"),  # correction 1
        bar(5, open_="113", close="112.5", high="113.5", low="107"),  # correction 2 -> qualified
        bar(6, open_="112.5", close="112.5", high="112.5", low="112.5"),  # no range
        bar(7, open_="112", close="113.5", high="114", low="111"),  # a real setup
    ]
    signals = _drive(flat_trigger)
    assert _arms(signals) == [(7, Decimal(114), Decimal(111))]
    assert signals[6] == []


def test_a_stop_that_the_buffer_drives_to_zero_or_below_arms_nothing() -> None:
    """A price at or below zero is not a stop. Reachable only at absurd prices with an absurd
    buffer, which is exactly why it is worth a test rather than an assumption."""
    signals = _drive(_GOLDEN[:7], stop_buffer_ticks=20_000)
    assert _arms(signals) == []


# --------------------------------------------------------------------------- #
# Construction                                                                  #
# --------------------------------------------------------------------------- #


def test_the_period_must_be_a_real_average() -> None:
    with pytest.raises(ValueError, match="average period"):
        PontoContinuoStrategy(period=0)


def test_the_stop_buffer_is_a_magnitude() -> None:
    with pytest.raises(ValueError, match="stop buffer"):
        PontoContinuoStrategy(stop_buffer_ticks=-1)


@pytest.mark.parametrize("multiple", ["0", "-1"])
def test_the_breakeven_multiple_must_be_positive(multiple: str) -> None:
    with pytest.raises(ValueError, match="breakeven R multiple"):
        PontoContinuoStrategy(breakeven_at_r=Decimal(multiple))


def test_an_unknown_average_is_refused() -> None:
    """The type says two strings are legal, but the wiring layer that will build this from JSON has
    no types — and a mistyped kind must raise rather than quietly become whichever average a
    fallback branch held."""
    with pytest.raises(ValueError, match="unknown average"):
        PontoContinuoStrategy(average="WMA")  # type: ignore[arg-type]


# --------------------------------------------------------------------------- #
# Determinism and invariants                                                    #
# --------------------------------------------------------------------------- #


@st.composite
def _random_candles(draw: st.DrawFn) -> list[Candle]:
    count = draw(st.integers(min_value=4, max_value=25))
    price = st.decimals(min_value="50", max_value="150", places=1)
    wick = st.decimals(min_value="0", max_value="4", places=1)
    candles: list[Candle] = []
    for index in range(count):
        open_ = draw(price)
        close = draw(price)
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        candles.append(bar(index, open_=str(open_), close=str(close), high=str(high), low=str(low)))
    return candles


def test_the_same_candles_always_produce_the_same_run() -> None:
    first, second = _run(_GOLDEN), _run(_GOLDEN)
    assert [(f.time, f.price, f.volume) for f in first.fills] == [
        (f.time, f.price, f.volume) for f in second.fills
    ]
    assert [(t.entry_price, t.exit_price, t.net_pnl) for t in first.trades] == [
        (t.entry_price, t.exit_price, t.net_pnl) for t in second.trades
    ]


@settings(max_examples=60, deadline=None)
@given(candles=_random_candles(), long=st.booleans())
def test_every_armed_order_carries_positive_risk(candles: list[Candle], long: bool) -> None:
    """Whatever the chart does, the trigger and the protective stop are never the same price and
    never the wrong way round. A zero-risk order divides by zero in the sizing; an inverted one is
    a stop the trade is already through."""
    side = Side.LONG if long else Side.SHORT
    for _, trigger, stop in _arms(_drive(candles, side=side)):
        assert trigger is not None
        assert stop is not None
        assert (trigger > stop) if long else (trigger < stop)


@settings(max_examples=60, deadline=None)
@given(candles=_random_candles(), long=st.booleans())
def test_no_order_is_armed_on_a_bar_that_shows_a_position(
    candles: list[Candle], long: bool
) -> None:
    """The strategy never adds to a trade, on any chart."""
    every_bar = frozenset(range(len(candles)))
    side = Side.LONG if long else Side.SHORT
    assert _arms(_drive(candles, side=side, position_on=every_bar)) == []
