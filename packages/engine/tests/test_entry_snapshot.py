"""The picture an entry leaves behind: the bars around it, and the levels that justified it.

The snapshot is the one thing in the engine that exists to be *looked at* rather than computed
with. That makes it easy to get quietly wrong — a window off by a bar, a splice with a hole in
it, a chart drawn from bars the strategy had not seen — because no metric moves when it breaks.
So the tests here are about the window's shape, and they are strict about it.

Every number was measured by running the real strategies, broker and loop and printing what
came out. None of it is arithmetic done in this file.

Two halves, and they have different owners:

* the **arming window** — the bars up to and including the decision bar — is attached by the
  loop, which is the component that sees the stream in backtest and in live alike;
* the **extension** — the bars from the decision to the one that filled — is attached by the
  backtest broker, the only side that knows when an order stopped resting.
"""

import datetime as dt
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.backtest_broker import SNAPSHOT_MAX_REST_BARS, BacktestBroker
from tradeforge_engine.domain import (
    Candle,
    EntrySnapshot,
    Side,
    Signal,
    SignalKind,
    SnapshotRegion,
)
from tradeforge_engine.loop import SNAPSHOT_BARS_BEFORE, RunResult, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.testing import EURUSD, HOUR, START, ScriptedStrategy, rising

# --------------------------------------------------------------------------- #
# Harness                                                                       #
# --------------------------------------------------------------------------- #


def _bar(index: int, *, open_: str, high: str, low: str, close: str) -> Candle:
    return Candle(
        time=START + index * HOUR,
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def _flat(index: int) -> Candle:
    """A bar that goes nowhere. Used to make an order wait without reaching its level."""
    return _bar(index, open_="1.10000", high="1.10100", low="1.09990", close="1.10000")


def _market_entry(reference: Candle) -> Signal:
    """An entry at market: it fills at the next bar's open, one bar after the decision."""
    return Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=reference.close,
        stop_loss=reference.close - Decimal("0.00500"),
    )


def _limit_entry(reference: Candle) -> Signal:
    """A buy limit well below the market: it rests until a bar reaches down to it."""
    return Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=reference.close,
        limit_price=Decimal("1.09500"),
        stop_loss=Decimal("1.09000"),
        client_id="zone-1",
    )


def _run(
    candles: list[Candle], script: dict[int, list[Signal]]
) -> tuple[RunResult, BacktestBroker]:
    broker = BacktestBroker(instrument=EURUSD, initial_capital=Decimal(100_000))
    result = run(
        candles=candles,
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=ScriptedStrategy(script),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    )
    return result, broker


def _entered(result: RunResult, broker: BacktestBroker) -> EntrySnapshot:
    """The snapshot on the position the run opened. Fails loudly if nothing entered."""
    positions = broker.positions("EURUSD")
    if positions:
        snapshot = positions[0].snapshot
    else:
        assert result.trades, "the run opened no position and closed no trade"
        snapshot = result.trades[0].snapshot
    assert snapshot is not None, "an entry filled through the loop carries a snapshot"
    return snapshot


def _times(snapshot: EntrySnapshot) -> list[dt.datetime]:
    return [bar.time for bar in snapshot.bars]


def _gaps(snapshot: EntrySnapshot) -> list[tuple[dt.datetime, dt.datetime]]:
    """The pairs of adjacent bars that are not one timeframe apart. Empty means contiguous."""
    times = _times(snapshot)
    return [
        (times[i], times[i + 1]) for i in range(len(times) - 1) if times[i + 1] - times[i] != HOUR
    ]


def _market_run(decision_bar: int) -> tuple[RunResult, BacktestBroker]:
    """Enter at market on `decision_bar` of a rising stream, filling on the bar after it."""
    candles = rising(decision_bar + 3)
    return _run(candles, {decision_bar: [_market_entry(candles[decision_bar])]})


def _resting_run(bars_of_rest: int) -> tuple[RunResult, BacktestBroker]:
    """Arm a buy limit on bar 0, and let price come down to it `bars_of_rest` bars later."""
    waiting = [_flat(index) for index in range(bars_of_rest + 1)]
    reaches = _bar(
        bars_of_rest + 1, open_="1.10000", high="1.10100", low="1.09400", close="1.09800"
    )
    after = _bar(bars_of_rest + 2, open_="1.09800", high="1.09900", low="1.09700", close="1.09850")
    candles = [*waiting, reaches, after]
    return _run(candles, {0: [_limit_entry(candles[0])]})


# --------------------------------------------------------------------------- #
# The type itself                                                               #
# --------------------------------------------------------------------------- #


def test_a_snapshot_needs_at_least_the_decision_bar() -> None:
    """An empty window reaches the screen as an empty chart, which reads as "no context here"
    rather than as the wiring bug it is."""
    with pytest.raises(ValueError, match="at least the decision bar"):
        EntrySnapshot(bars=(), decided_at=START)


def test_snapshot_bars_must_ascend_in_time() -> None:
    """Drawn as-is, a window that goes backwards is a chart folding over itself."""
    first, second = _flat(0), _flat(1)
    with pytest.raises(ValueError, match="ascend in time"):
        EntrySnapshot(bars=(second, first), decided_at=second.time)


def test_a_repeated_bar_is_not_ascending() -> None:
    """Equal timestamps, not just decreasing ones. A splice that re-appends the bar it was
    anchored to would otherwise pass, and draw one candle twice."""
    bar = _flat(0)
    with pytest.raises(ValueError, match="ascend in time"):
        EntrySnapshot(bars=(bar, bar), decided_at=bar.time)


def test_the_decision_bar_has_to_be_inside_the_window() -> None:
    """The check the contiguity rule cannot make. Two ascending halves that do not adjoin are
    still ascending — what proves they adjoin is that the bar they were joined at is present."""
    with pytest.raises(ValueError, match="not in the snapshot window"):
        EntrySnapshot(bars=(_flat(0), _flat(1)), decided_at=START + 5 * HOUR)


def test_a_naive_decision_instant_is_refused() -> None:
    """Same argument as everywhere else in the domain: a naive datetime is not an instant."""
    with pytest.raises(ValueError, match=r"EntrySnapshot\.decided_at"):
        EntrySnapshot(bars=(_flat(0),), decided_at=dt.datetime(2024, 1, 1))  # noqa: DTZ001


def test_a_region_needs_a_label() -> None:
    """An unnamed rectangle is undrawable: a chart with two of them has no way to say which is
    the zone and which is, later, the fair value gap."""
    with pytest.raises(ValueError, match="needs a label"):
        SnapshotRegion(label="", top=Decimal(100), bottom=Decimal(90), from_time=START)


def test_a_region_cannot_be_upside_down() -> None:
    """Inverted edges render as a rectangle of negative height, which most chart libraries
    silently normalise — leaving a zone that looks right and is wrong."""
    with pytest.raises(ValueError, match="is below bottom"):
        SnapshotRegion(label="zone", top=Decimal(90), bottom=Decimal(100), from_time=START)


def test_a_region_of_zero_height_is_allowed() -> None:
    """A zone whose candle had no range is degenerate, not invalid — it draws as a line, which
    is an honest picture of a marking candle that was a doji."""
    region = SnapshotRegion(label="zone", top=Decimal(100), bottom=Decimal(100), from_time=START)
    assert region.top == region.bottom


def test_a_region_may_start_before_the_window_but_not_after_the_decision() -> None:
    """The asymmetry is the whole rule, and it is the anti-lookahead rule as a drawing.

    A zone marked by a long impulse leg is routinely older than the fifty bars carried with the
    entry, so a left edge before `bars[0]` is normal and must be allowed. A left edge *after*
    the decision is a rectangle justifying a decision with a bar that had not printed yet — and
    no market event produces it today, which is exactly why nothing else would catch it.
    """
    bars = (_flat(0), _flat(1), _flat(2))
    older = SnapshotRegion(
        label="zone", top=Decimal(100), bottom=Decimal(90), from_time=START - 20 * HOUR
    )
    later = SnapshotRegion(
        label="zone", top=Decimal(100), bottom=Decimal(90), from_time=bars[2].time
    )

    accepted = EntrySnapshot(bars=bars, decided_at=bars[1].time, regions=(older,))
    assert accepted.regions[0].from_time < accepted.bars[0].time

    with pytest.raises(ValueError, match="after the decision"):
        EntrySnapshot(bars=bars, decided_at=bars[1].time, regions=(later,))


def test_a_region_starting_on_the_decision_bar_is_allowed() -> None:
    """The boundary belongs to the past: a zone marked by the very bar that armed the entry is
    a bar the strategy had already seen in full. Refusing it would reject a legitimate setup."""
    bars = (_flat(0), _flat(1))
    same = SnapshotRegion(
        label="zone", top=Decimal(100), bottom=Decimal(90), from_time=bars[1].time
    )
    assert EntrySnapshot(bars=bars, decided_at=bars[1].time, regions=(same,)).regions == (same,)


def test_a_snapshot_has_no_regions_unless_the_strategy_gave_it_any() -> None:
    """The swing setups enter off an average, which is a line and not a band. Defaulting to a
    rectangle spanning nothing would put an invisible artefact on every one of their charts."""
    result, _ = _market_run(decision_bar=5)
    order = result.fills[0].order
    assert order.snapshot is not None
    assert order.snapshot.regions == ()


def test_the_broker_carries_the_regions_through_the_extension() -> None:
    """The zone has to survive the splice. Dropping it when the window is extended would erase
    the rectangle from exactly the trades that filled — the only ones anybody looks at."""
    candles = rising(8)
    zone = SnapshotRegion(
        label="zone", top=Decimal("1.10200"), bottom=Decimal("1.10000"), from_time=candles[1].time
    )
    signal = Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=candles[5].close,
        stop_loss=candles[5].close - Decimal("0.00500"),
        regions=(zone,),
    )
    result, broker = _run(candles, {5: [signal]})
    snapshot = _entered(result, broker)

    assert snapshot.regions == (zone,)
    assert snapshot.filled_at == broker.positions("EURUSD")[0].entry_time  # it did extend


def test_filled_at_is_the_last_bar_and_decided_at_is_not_derived() -> None:
    """Before the broker extends it the two coincide; after, they must not be read off the same
    end of the list. This is why `decided_at` is stored rather than computed."""
    bars = (_flat(0), _flat(1), _flat(2))
    snapshot = EntrySnapshot(bars=bars, decided_at=bars[0].time)
    assert snapshot.decided_at == bars[0].time
    assert snapshot.filled_at == bars[2].time


# --------------------------------------------------------------------------- #
# The arming window — the loop's half                                           #
# --------------------------------------------------------------------------- #


def test_the_arming_window_ends_on_the_decision_bar() -> None:
    """What the strategy had seen, and not one bar more. A window that ran a bar past the
    decision would be showing the strategy a bar it had not been given yet."""
    result, _ = _market_run(decision_bar=5)
    order = result.fills[0].order
    assert order.snapshot is not None
    assert order.snapshot.bars[-1].time == order.decided_at
    assert order.snapshot.decided_at == order.decided_at


@pytest.mark.parametrize(
    ("decision_bar", "expected_bars"),
    [
        (0, 1),  # the first bar of the run: the decision bar is the whole window
        (5, 6),
        (49, 50),
        (50, SNAPSHOT_BARS_BEFORE + 1),  # exactly full
        (51, SNAPSHOT_BARS_BEFORE + 1),  # saturated
        (120, SNAPSHOT_BARS_BEFORE + 1),  # still saturated, far into the run
    ],
)
def test_the_arming_window_grows_to_the_cap_and_stops(
    decision_bar: int, expected_bars: int
) -> None:
    """Measured, not derived. The cap is what keeps a decade of M1 out of memory — `run()`
    takes an `Iterable` for that reason, and an unbounded buffer would hand it back."""
    result, _ = _market_run(decision_bar)
    order = result.fills[0].order
    assert order.snapshot is not None
    assert len(order.snapshot.bars) == expected_bars


def test_an_exit_carries_no_snapshot() -> None:
    """Nothing to picture: an exit closes a position rather than choosing a place to open one,
    and a window hung off it would be a second, contradictory chart for the same trade."""
    candles = rising(6)
    script = {
        1: [_market_entry(candles[1])],
        3: [
            Signal(
                kind=SignalKind.EXIT,
                side=Side.LONG,
                reference_price=candles[3].close,
                reason="test",
            )
        ],
    }
    result, _ = _run(candles, script)
    exits = [fill for fill in result.fills if fill.order.intent is SignalKind.EXIT]
    assert exits, "the scripted exit did not fill"
    assert all(fill.order.snapshot is None for fill in exits)


# --------------------------------------------------------------------------- #
# The extension — the broker's half                                             #
# --------------------------------------------------------------------------- #


def test_a_market_entry_reaches_the_bar_that_filled_it() -> None:
    """The whole point of the extension, in its smallest case: an order decided on bar N fills
    at the open of N+1, and the window has to contain N+1 or the chart stops one bar short of
    the entry it exists to show."""
    result, broker = _market_run(decision_bar=5)
    snapshot = _entered(result, broker)
    position = broker.positions("EURUSD")[0]

    assert snapshot.filled_at == position.entry_time
    assert snapshot.decided_at == snapshot.filled_at - HOUR
    assert len(snapshot.bars) == 7  # 6 through the decision, plus the fill bar


@pytest.mark.parametrize("bars_of_rest", [1, 5, 100, SNAPSHOT_MAX_REST_BARS - 1])
def test_a_resting_order_carries_every_bar_it_waited_through(bars_of_rest: int) -> None:
    """A limit is not filled by the next open — it waits for price to come back. What price did
    while it waited is how you tell a level price *reached* from one a gap ran over, and it is
    knowledge only the broker has."""
    result, broker = _resting_run(bars_of_rest)
    snapshot = _entered(result, broker)
    position = broker.positions("EURUSD")[0]

    assert snapshot.filled_at == position.entry_time
    assert _gaps(snapshot) == []
    # The decision was bar 0, so the arming window is that one bar and everything else is the
    # extension: the bars it rested through, plus the bar that reached the level.
    assert len(snapshot.bars) == bars_of_rest + 2


def test_an_order_that_rests_past_the_buffer_keeps_the_arming_window_rather_than_splicing() -> None:
    """The one case where the picture is deliberately incomplete.

    Past the cap the decision bar has dropped out of the broker's buffer, so the bars still held
    do not adjoin the arming window. Joining them anyway would produce a chart with a hole in the
    middle and nothing on it to say so — strictly worse than a chart that stops early, because
    the hole is invisible. So the snapshot keeps the arming window and stops.
    """
    result, broker = _resting_run(SNAPSHOT_MAX_REST_BARS)
    snapshot = _entered(result, broker)
    position = broker.positions("EURUSD")[0]

    assert snapshot.filled_at != position.entry_time
    assert snapshot.filled_at == snapshot.decided_at  # the arming window, alone
    assert _gaps(snapshot) == []


def test_the_boundary_between_extending_and_giving_up_is_one_bar() -> None:
    """Written as a pair, because a cap tested only from the inside is a cap that could be off
    by one in the direction nothing checks."""

    def reaches_the_fill(bars_of_rest: int) -> bool:
        result, broker = _resting_run(bars_of_rest)
        return _entered(result, broker).filled_at == broker.positions("EURUSD")[0].entry_time

    assert reaches_the_fill(SNAPSHOT_MAX_REST_BARS - 1)
    assert not reaches_the_fill(SNAPSHOT_MAX_REST_BARS)


# --------------------------------------------------------------------------- #
# The invariant that holds across both halves                                   #
# --------------------------------------------------------------------------- #


@settings(max_examples=60, deadline=None)
@given(
    decision_bar=st.integers(min_value=0, max_value=60),
    bars_of_rest=st.integers(min_value=0, max_value=8),
)
def test_the_window_is_contiguous_however_the_two_halves_meet(
    decision_bar: int, bars_of_rest: int
) -> None:
    """The splice is the only place a hole can appear, and it appears at exactly one seam: the
    join between the loop's window and the broker's extension. Sweeping the decision across the
    cap boundary and the rest across a few bars puts that seam in every position it can take.
    """
    waiting = [_flat(index) for index in range(decision_bar + bars_of_rest + 1)]
    reaches = _bar(len(waiting), open_="1.10000", high="1.10100", low="1.09400", close="1.09800")
    tail = _bar(len(waiting) + 1, open_="1.09800", high="1.09900", low="1.09700", close="1.09850")
    candles = [*waiting, reaches, tail]

    result, broker = _run(candles, {decision_bar: [_limit_entry(candles[decision_bar])]})
    if not broker.positions("EURUSD") and not result.trades:
        return  # the limit was never reached in this shape; nothing to assert

    snapshot = _entered(result, broker)
    assert _gaps(snapshot) == []
    assert snapshot.decided_at in _times(snapshot)
    assert len(snapshot.bars) == len(set(_times(snapshot)))


def test_re_arming_every_bar_does_not_pin_the_whole_stream() -> None:
    """The bound that makes the window affordable, asserted where it can actually be lost.

    `maxlen` bounds **one** window; it does not bound how many windows are alive. A flat
    strategy re-arms on most bars, so an order kept for inspection with its snapshot attached
    pins ~50 candles per bar — which pins the entire stream, and hands back exactly the cost
    `run()` takes an `Iterable` to avoid. Nothing computes a wrong number when this regresses;
    a long backtest simply dies of `MemoryError` at a length that used to pass.

    So `submitted` is asserted to be snapshot-free, and the count is asserted too: a version
    that stopped recording orders would pass the first assertion for the wrong reason.
    """
    candles = rising(40)
    script = {index: [_market_entry(candles[index])] for index in range(30)}
    _, broker = _run(candles, script)

    assert len(broker.submitted) == 30  # it really did accept one per bar
    assert all(order.snapshot is None for order in broker.submitted)


def test_the_stripped_record_keeps_everything_except_the_bars() -> None:
    """Stripped for size, not redacted. An inspection list that quietly lost the level an order
    rested at would make `submitted` useless for the debugging it exists for."""
    candles = rising(8)
    result, broker = _run(candles, {5: [_market_entry(candles[5])]})

    (recorded,) = broker.submitted
    filled = result.fills[0].order
    assert recorded.snapshot is None
    assert filled.snapshot is not None  # the order that fills is untouched
    assert (recorded.side, recorded.volume, recorded.stop_loss, recorded.decided_at) == (
        filled.side,
        filled.volume,
        filled.stop_loss,
        filled.decided_at,
    )


def test_the_snapshot_changes_no_number_the_backtest_reports() -> None:
    """It is something to look at, never something the arithmetic reads. Said as a test because
    the day it stops being true is the day a chart starts moving the P&L."""
    result, broker = _market_run(decision_bar=5)
    snapshot = _entered(result, broker)
    position = broker.positions("EURUSD")[0]

    assert snapshot.bars  # it is there
    assert position.entry_price == Decimal("1.10600")  # measured
    assert broker.account().balance == Decimal("100000")  # nothing realised yet
