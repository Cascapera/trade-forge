"""The seam between history on disk and bars that have not happened yet.

Every scenario here is built around one question: **where does the cut fall?** Inside the
Parquet, inside the backlog, or before the first bar that ever arrives. The bug this module
exists to prevent is a bar disappearing at that boundary, and a bar that disappears leaves
nothing behind — no exception, no log, just an equity curve that is quietly wrong.
"""

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal

import pytest

from tradeforge_api.live import CandleStream
from tradeforge_api.live.splice import BarSource, SplicedCandles, splice
from tradeforge_api.live.testing import FakeRedisStreams
from tradeforge_collector.live import Subscription
from tradeforge_engine.domain import Candle

HOUR = dt.timedelta(hours=1)
START = dt.datetime(2026, 8, 25, 0, tzinfo=dt.UTC)


def bar(index: int) -> Candle:
    """The bar opening `index` hours after `START`. Prices exist only to be a legal candle."""
    price = Decimal("1.1000") + Decimal(index) / Decimal(10000)
    return Candle(
        time=START + index * HOUR,
        open=price,
        high=price + Decimal("0.0005"),
        low=price - Decimal("0.0005"),
        close=price,
        tick_volume=100,
        spread=2,
    )


def closes_of(candles: Iterator[Candle]) -> list[int]:
    """Bars as their index, so a scenario reads as `[0, 1, 2]` rather than as timestamps."""
    return [round((candle.time - START) / HOUR) for candle in candles]


def cut_after(index: int) -> dt.datetime:
    """The instant the bar at `index` has just closed — so bars 0..index are history."""
    return START + (index + 1) * HOUR


class FakeSource:
    """A `BarSource` that hands back fixed lists and records that its group was created."""

    def __init__(self, *, backlog: list[Candle], live: list[Candle]) -> None:
        self._backlog = backlog
        self._live = live
        self.events: list[str] = []

    def ensure_group(self) -> bool:
        self.events.append("ensure_group")
        return True

    def backlog(self) -> Iterator[Candle]:
        self.events.append("backlog")
        yield from self._backlog

    def candles(self) -> Iterator[Candle]:
        self.events.append("candles")
        yield from self._live


def spliced(
    *,
    history: list[Candle],
    backlog: list[Candle],
    live: list[Candle],
    opened_at: dt.datetime,
) -> tuple[SplicedCandles, FakeSource]:
    source = FakeSource(backlog=backlog, live=live)
    candles = splice(source, history=lambda: history, timeframe=HOUR, opened_at=opened_at)
    return candles, source


# --------------------------------------------------------------------------------------------
# Where the cut falls
# --------------------------------------------------------------------------------------------


def test_the_cut_inside_the_backlog_is_the_ordinary_case() -> None:
    """Parquet is behind the wire, because the live publisher writes to Redis only — the
    collector's backfill is what fills the disk. So the backlog routinely holds bars that are
    history, and telling those from the session's own bars is the whole point of the cut.
    """
    candles, _ = spliced(
        history=[bar(0), bar(1)],
        backlog=[bar(2), bar(3), bar(4)],
        live=[bar(5)],
        opened_at=cut_after(3),
    )

    assert closes_of(candles.warmup()) == [0, 1, 2, 3]
    assert closes_of(candles.live()) == [4, 5]
    assert candles.warmed == 4


def test_the_cut_inside_the_history_leaves_the_whole_stream_live() -> None:
    """⚠️ The backlog must still be reached. Holding a *history* bar and then jumping to the
    live stream would drop the backlog on the floor — bars read by nobody, and no error."""
    candles, _ = spliced(
        history=[bar(0), bar(1), bar(2)],
        backlog=[bar(3), bar(4)],
        live=[bar(5)],
        opened_at=cut_after(0),
    )

    assert closes_of(candles.warmup()) == [0]
    assert closes_of(candles.live()) == [1, 2, 3, 4, 5], "the backlog was skipped"


def test_a_current_parquet_and_an_empty_backlog_warm_from_disk_alone() -> None:
    """The healthy case, and the one that must not block. With nothing waiting, deciding where
    the cut is means asking the stream — and asking it *blockingly* would keep a session in
    warm-up until the next bar closed, which on H4 is four hours with no row saying it exists.
    """
    candles, source = spliced(
        history=[bar(0), bar(1), bar(2)], backlog=[], live=[bar(3)], opened_at=cut_after(2)
    )

    assert closes_of(candles.warmup()) == [0, 1, 2]
    assert candles.warmed == 3
    assert closes_of(candles.live()) == [3]
    assert source.events == ["ensure_group", "backlog", "candles"]


def test_a_session_with_no_history_at_all_warms_with_nothing() -> None:
    """A symbol collected for the first time. Zero is a real answer — what must not happen is
    the warm-up refusing to end."""
    candles, _ = spliced(history=[], backlog=[], live=[bar(0)], opened_at=START)

    assert closes_of(candles.warmup()) == []
    assert candles.warmed == 0
    assert closes_of(candles.live()) == [0]


def test_everything_is_history_when_the_session_opens_far_in_the_future() -> None:
    candles, _ = spliced(
        history=[bar(0), bar(1)], backlog=[bar(2)], live=[], opened_at=cut_after(99)
    )

    assert closes_of(candles.warmup()) == [0, 1, 2]
    assert closes_of(candles.live()) == []


# --------------------------------------------------------------------------------------------
# The boundary bar
# --------------------------------------------------------------------------------------------


def test_the_bar_that_ends_the_warm_up_is_held_not_dropped() -> None:
    """⚠️ **The bug this module exists to prevent.** That bar was read to decide where the cut
    was. A `warmup()` that consumed it to make the decision and did not hand it back would lose
    exactly one bar per session, at start-up, silently — and the equity curve would simply be
    missing a bar nobody could name.

    Separating deliberately on the *first* live bar rather than on a count: an implementation
    that dropped it would still produce a plausible-looking sequence.
    """
    candles, _ = spliced(
        history=[bar(0)], backlog=[bar(1), bar(2)], live=[bar(3)], opened_at=cut_after(0)
    )

    warmed = closes_of(candles.warmup())
    lived = closes_of(candles.live())

    assert warmed == [0]
    assert lived == [1, 2, 3], "bar 1 decided the cut and then vanished"
    assert warmed + lived == [0, 1, 2, 3], "the seam lost or repeated a bar"


def test_the_held_bar_is_yielded_exactly_once() -> None:
    """The mirror of the test above: handing it back *twice* would have the engine see a
    duplicate, which `loop._reject_out_of_order` turns into a session that refuses to start."""
    candles, _ = spliced(history=[], backlog=[bar(0), bar(1)], live=[bar(2)], opened_at=START)

    assert closes_of(candles.warmup()) == []
    assert closes_of(candles.live()) == [0, 1, 2]


def test_a_bar_still_forming_when_the_session_opened_is_live_not_history() -> None:
    """⚠️ Separates `time <= cut` from `time + timeframe <= cut`.

    `Candle.time` is the bar's **opening** instant. Bar 2 opens at 02:00 and closes at 03:00; a
    session opening at 02:30 must treat it as live. Comparing the opening instant would file it
    as history — settling an unfinished bar into the warm-up, and starting the session one bar
    ahead of where it actually is.
    """
    half_way_through_bar_two = START + 2 * HOUR + dt.timedelta(minutes=30)
    candles, _ = spliced(
        history=[bar(0), bar(1), bar(2)],
        backlog=[],
        live=[bar(3)],
        opened_at=half_way_through_bar_two,
    )

    assert closes_of(candles.warmup()) == [0, 1], "an unfinished bar was warmed on"
    assert closes_of(candles.live()) == [2, 3]


def test_a_bar_that_closed_exactly_at_the_opening_instant_is_history() -> None:
    """The boundary itself. Bar 2 closes at 03:00 and the session opens at 03:00: it had
    closed, so it is history. The other reading would push one settled bar into the live
    ledger every time a session started on a bar boundary — which is the common case, because
    a session is usually started right after one closes."""
    candles, _ = spliced(
        history=[bar(0), bar(1), bar(2)], backlog=[], live=[bar(3)], opened_at=cut_after(2)
    )

    assert closes_of(candles.warmup()) == [0, 1, 2]
    assert closes_of(candles.live()) == [3]


# --------------------------------------------------------------------------------------------
# Strictly increasing, end to end
# --------------------------------------------------------------------------------------------


def test_the_stream_replaying_the_parquet_tail_is_de_duplicated() -> None:
    """⚠️ Not a tidiness measure. A group created at `0` is offered the whole stream, which
    overlaps the disk by construction — and a repeated bar makes `loop._reject_out_of_order`
    raise, so without this a session simply would not start."""
    candles, _ = spliced(
        history=[bar(0), bar(1), bar(2)],
        backlog=[bar(1), bar(2), bar(3)],
        live=[bar(4)],
        opened_at=cut_after(3),
    )

    assert closes_of(candles.warmup()) == [0, 1, 2, 3]
    assert candles.dropped == 2
    assert closes_of(candles.live()) == [4]


def test_a_single_bar_of_overlap_is_dropped() -> None:
    """The smallest overlap there is, and the common one: the collector's last write to disk is
    also the oldest thing still on the wire.

    Both copies land inside the warm-up here. The case where the surviving copy is on the *live*
    side of the seam is `test_a_bar_repeating_the_one_that_ended_the_warm_up_is_dropped`, and it
    is a different proof — that one is about the mark surviving the hand-over.
    """
    candles, _ = spliced(
        history=[bar(0), bar(1)], backlog=[bar(1)], live=[bar(2)], opened_at=cut_after(1)
    )

    assert closes_of(candles.warmup()) == [0, 1]
    assert closes_of(candles.live()) == [2]
    assert candles.dropped == 1


def test_a_bar_that_goes_backwards_is_refused_rather_than_handed_on() -> None:
    candles, _ = spliced(history=[], backlog=[], live=[bar(5), bar(3), bar(6)], opened_at=START)

    assert closes_of(candles.warmup()) == []
    assert closes_of(candles.live()) == [5, 6]
    assert candles.dropped == 1


def test_a_drop_is_reported(caplog: pytest.LogCaptureFixture) -> None:
    """Silence here would make "the stream replayed my history" and "the collector is
    publishing out of order" the same event. They are not, and only the count tells them
    apart."""
    candles, _ = spliced(history=[bar(0), bar(0)], backlog=[], live=[], opened_at=cut_after(9))

    with caplog.at_level("INFO"):
        assert closes_of(candles.warmup()) == [0]

    assert candles.dropped == 1
    assert "dropped a bar" in caplog.text


def test_warmed_counts_bars_the_strategy_saw_not_bars_that_arrived() -> None:
    """⚠️ `warmed` is what the session records as its warm-up, and a dropped bar never reached
    the strategy. Counting arrivals would report a warm-up longer than the one that happened —
    a number that reads as reassurance and is not."""
    candles, _ = spliced(
        history=[bar(0), bar(1)], backlog=[bar(0), bar(1), bar(2)], live=[], opened_at=cut_after(5)
    )

    assert closes_of(candles.warmup()) == [0, 1, 2]
    assert candles.warmed == 3, "dropped bars were counted as warm-up"
    assert candles.dropped == 2


# --------------------------------------------------------------------------------------------
# Ordering, refusals, and the protocol
# --------------------------------------------------------------------------------------------


def test_the_group_is_created_before_history_is_read() -> None:
    """⚠️ The ordering hazard, and it is the one with no symptom. Reading Parquet takes seconds
    to tens of seconds; a bar closing in that window is on the wire and not on disk, and a group
    created afterwards has already missed it. The result is a hole in the equity curve with
    nothing anywhere recording that a bar was skipped.

    Proved by recording the order rather than by reading the source: `history` is a callable
    precisely so that this function, not the call site, decides when the read happens.
    """
    order: list[str] = []
    source = FakeSource(backlog=[], live=[])
    source.events = order

    def read_history() -> list[Candle]:
        order.append("read_history")
        return [bar(0)]

    candles = splice(source, history=read_history, timeframe=HOUR, opened_at=cut_after(9))
    list(candles.warmup())

    assert order[:2] == ["ensure_group", "read_history"], f"wrong order: {order}"


def test_history_is_not_read_until_the_group_exists_even_if_nobody_iterates() -> None:
    """⚠️ The separating half of the test above. `splice()` returning without creating the group
    — leaving it to the first `next()` — would pass an ordering check that only looks at a
    driven sequence, while still losing bars for a caller that builds the splice early."""
    source = FakeSource(backlog=[], live=[])

    splice(source, history=lambda: [bar(0)], timeframe=HOUR, opened_at=cut_after(9))

    assert source.events == ["ensure_group"], "the group was not created eagerly"


def test_live_before_the_warm_up_finished_is_refused() -> None:
    """⚠️ Both generators pull from the same iterator, so an interleaved `live()` would take
    bars the warm-up was about to see. Each would then trade half the market, and nothing would
    raise — the same failure a shared consumer group produces, one layer up."""
    candles, _ = spliced(
        history=[bar(0), bar(1)], backlog=[], live=[bar(2)], opened_at=cut_after(1)
    )

    with pytest.raises(RuntimeError, match="driven to completion"):
        candles.live()


def test_live_is_refused_when_the_warm_up_was_abandoned_part_way() -> None:
    """Starting the generator is not finishing it. A caller that broke out of the warm-up loop
    has a strategy warmed over some prefix nobody can name, and letting it open a session would
    record a `warmup_bars` that is not what happened."""
    candles, _ = spliced(
        history=[bar(0), bar(1), bar(2)], backlog=[], live=[bar(3)], opened_at=cut_after(2)
    )

    warming = candles.warmup()
    next(warming)

    with pytest.raises(RuntimeError, match="driven to completion"):
        candles.live()


def test_a_naive_opening_instant_is_refused() -> None:
    """A naive cut against a UTC bar raises `TypeError` inside the first comparison — surfacing
    as a crash mid-warm-up rather than as the configuration mistake it is."""
    with pytest.raises(ValueError, match="timezone-aware"):
        SplicedCandles([], timeframe=HOUR, opened_at=dt.datetime(2026, 8, 25))  # noqa: DTZ001


@pytest.mark.parametrize("timeframe", [dt.timedelta(0), dt.timedelta(seconds=-1)])
def test_a_non_positive_timeframe_is_refused(timeframe: dt.timedelta) -> None:
    """With a zero timeframe every bar's close equals its open, so the cut would file bars as
    history by their opening instant — the off-by-one this module is written against."""
    with pytest.raises(ValueError, match="must be positive"):
        SplicedCandles([], timeframe=timeframe, opened_at=START)


def test_the_real_candle_stream_satisfies_the_bar_source_protocol() -> None:
    """⚠️ Proved by assignment, so mypy checks it. A `Protocol` nothing is ever assigned to
    describes an imaginary client: this repository has already shipped one that the real redis
    client did not satisfy, and the tests were all green.
    """
    stream = CandleStream(
        FakeRedisStreams(Subscription(symbol="EURUSD", timeframe="H1")),
        Subscription(symbol="EURUSD", timeframe="H1"),
        group="a-session",
    )

    source: BarSource = stream

    assert source.ensure_group() is True


def test_live_can_only_be_taken_once() -> None:
    """⚠️ Two live generators over one iterator is the shared-consumer-group failure again: each
    takes bars from the other, both look plausible, and nothing raises. A session that retried
    after a reconnect by asking for `live()` again would silently trade every other bar."""
    candles, _ = spliced(
        history=[bar(0)], backlog=[], live=[bar(1), bar(2)], opened_at=cut_after(0)
    )
    list(candles.warmup())
    candles.live()

    with pytest.raises(RuntimeError, match="only pass"):
        candles.live()


def test_the_refusals_do_not_wait_for_the_first_bar() -> None:
    """⚠️ Separates a check in `live()` from the same check inside the generator it returns.

    A `raise` written in a generator body does not run until the first `next()`. So a session
    that asked for `live()` twice would be handed two apparently valid iterators and find out —
    or not — hours later, on whichever bar arrived first. Calling `live()` without advancing it
    is what tells the two implementations apart.
    """
    candles, _ = spliced(history=[bar(0)], backlog=[], live=[bar(1)], opened_at=cut_after(0))

    with pytest.raises(RuntimeError, match="driven to completion"):
        candles.live()

    list(candles.warmup())
    candles.live()

    with pytest.raises(RuntimeError, match="only pass"):
        candles.live()


def test_a_bar_repeating_the_one_that_ended_the_warm_up_is_dropped() -> None:
    """⚠️ The load-bearing half of running the held bar through the de-duplicator.

    Bar 1 is the last bar on disk *and* the first bar the session trades — the cut falls between
    bar 0 and bar 1 — and the stream then replays it. If handing the held bar over did not
    advance the "latest seen" mark, the replay would be admitted, the engine would be given the
    same bar twice, and `loop._reject_out_of_order` would kill the session on its second bar.

    Distinct from `test_a_duplicate_across_the_cut_is_dropped_too`, where both copies land
    inside the warm-up: here the surviving copy is on the live side of the seam.
    """
    candles, _ = spliced(
        history=[bar(0), bar(1)], backlog=[bar(1), bar(2)], live=[bar(3)], opened_at=cut_after(0)
    )

    assert closes_of(candles.warmup()) == [0]
    assert closes_of(candles.live()) == [1, 2, 3]
    assert candles.dropped == 1
