"""The live loop, exercised without a terminal and without a Redis.

Every test here runs on Linux CI. That is not a convenience: the loop is the piece of phase 3
that decides whether a bar closed, and a component whose behaviour can only be observed against
a broker is one whose behaviour is observed once, by hand, on a Windows box. The acceptance
criterion of this PR is "kill the terminal and bring it back", and it is a test in this file
rather than a paragraph in a runbook for exactly that reason.
"""

import datetime as dt
import logging
from decimal import Decimal

import pytest

from tradeforge_collector.live import (
    Backoff,
    CandlePublisher,
    LiveSource,
    Subscription,
    poll_once,
    run,
    stream_name,
)
from tradeforge_collector.publisher import candle_fields, candle_from_fields, entry_id
from tradeforge_collector.source import Candle
from tradeforge_collector.synthetic import SyntheticSource

EURUSD_M5 = Subscription("EURUSD", "M5")
EURUSD_M1 = Subscription("EURUSD", "M1")
GBPUSD_M1 = Subscription("GBPUSD", "M1")

NOON = dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)


def candle(minute: int, *, price: str = "1.10000") -> Candle:
    return Candle(
        time=NOON + dt.timedelta(minutes=minute),
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        tick_volume=7,
        spread=2,
        real_volume=0,
    )


def minutes(*offsets: int) -> list[Candle]:
    """A run of one-minute bars at the given offsets from noon, oldest first."""
    return [candle(offset) for offset in offsets]


def noop(_seconds: float) -> None:
    """A `sleep` that does not sleep. A reconnection test that waited is a test nobody runs."""


class FakeSource:
    """A terminal that can be watched, killed, refused and brought back.

    `bars` holds the *closed* history per subscription, oldest first, and `recent_closed`
    serves it from the end — which is what asking by position means, and what makes the
    contiguity property real here: a run of positions reaches back over the bars that exist,
    never into the empty time where a market was shut.
    """

    def __init__(self) -> None:
        self.bars: dict[tuple[str, str], list[Candle]] = {}
        self.raises: set[tuple[str, str]] = set()
        self.refuses: set[str] = set()
        self.down = False
        self.reconnect_failures = 0
        self.reconnects = 0
        self.subscribed: list[str] = []
        self.calls: list[tuple[str, str, int]] = []

    def subscribe(self, symbol: str) -> None:
        if self.down:
            raise ConnectionError("the terminal went away")
        if symbol in self.refuses:
            raise LookupError(f"{symbol} is not in Market Watch")
        self.subscribed.append(symbol)

    def recent_closed(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
        self.calls.append((symbol, timeframe, count))
        if self.down:
            raise ConnectionError("the terminal went away")
        if (symbol, timeframe) in self.raises:
            raise LookupError(f"no such symbol {symbol}")
        return self.bars.get((symbol, timeframe), [])[-count:]

    def reconnect(self) -> None:
        self.reconnects += 1
        if self.reconnect_failures > 0:
            self.reconnect_failures -= 1
            raise ConnectionError("the terminal is still coming up")
        self.down = False


class FakePublisher:
    """Remembers what it was given, and refuses a bar that is not newer — like the real one.

    ⚠️ **Refusing by "not newer", not by "already seen", and the difference is the point.**
    Redis refuses an `XADD` whose id is not greater than the stream's last, so an *older* bar
    offered after a newer one is rejected even though it was never published. A fake that
    accepted it would let a gap fill running backwards look correct here and lose bars in
    production.
    """

    def __init__(self) -> None:
        self.published: list[tuple[Subscription, Candle]] = []
        self.offered = 0
        self._newest: dict[Subscription, dt.datetime] = {}

    def publish(self, subscription: Subscription, candle: Candle) -> bool:
        self.offered += 1
        newest = self._newest.get(subscription)
        if newest is not None and candle.time <= newest:
            return False
        self._newest[subscription] = candle.time
        self.published.append((subscription, candle))
        return True

    def last_published(self, subscription: Subscription) -> dt.datetime | None:
        return self._newest.get(subscription)


def published_minutes(publisher: FakePublisher) -> list[int]:
    return [(one.time - NOON) // dt.timedelta(minutes=1) for _, one in publisher.published]


def test_the_fakes_satisfy_the_protocols() -> None:
    """Structural conformance, asserted rather than assumed.

    ⚠️ Without this the fakes could drift from the protocols and every test below would keep
    passing while proving nothing about the real implementations.
    """
    # `LiveSource` is `runtime_checkable` because production code asks — the CLI refuses a
    # source that cannot be watched — so the check here is the same one that runs for real.
    assert isinstance(FakeSource(), LiveSource)
    # `CandlePublisher` is not, because nothing asks at runtime. The annotation is the
    # assertion, and mypy --strict in CI is what enforces it; making the protocol
    # `runtime_checkable` just to satisfy an `isinstance` here would be widening production
    # code to suit a test.
    publisher: CandlePublisher = FakePublisher()
    assert publisher.publish(EURUSD_M5, candle(5)) is True


def test_a_stream_is_named_for_its_symbol_and_timeframe() -> None:
    # One stream per subscription, so a session reading EURUSD M5 never has to skip past bars
    # of anything else to find its own.
    assert stream_name(EURUSD_M5) == "candles.EURUSD.M5"
    assert stream_name(EURUSD_M1) == "candles.EURUSD.M1"


# --------------------------------------------------------------------------- #
# One poll: what closed, and what gets announced                               #
# --------------------------------------------------------------------------- #


def test_a_bar_is_published_once_however_often_it_is_polled() -> None:
    """The core of the loop: polling is frequent, closing is not."""
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = [candle(5)]

    first = poll_once(source, publisher, [EURUSD_M5])
    second = poll_once(source, publisher, [EURUSD_M5])
    third = poll_once(source, publisher, [EURUSD_M5])

    assert list(first) == [EURUSD_M5]
    # ⚠️ The empty answer is the normal one. A loop polling every 5s on M5 sees 59 nothings for
    # every bar, and a test that only ever checked the interesting poll would not notice a loop
    # that republished on every one of them.
    assert second == {}
    assert third == {}
    assert len(publisher.published) == 1


def test_the_next_bar_is_published_when_it_arrives() -> None:
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = [candle(5)]
    poll_once(source, publisher, [EURUSD_M5])

    source.bars[("EURUSD", "M5")] = [candle(5), candle(10)]
    published = poll_once(source, publisher, [EURUSD_M5])

    assert [one.time.minute for one in published[EURUSD_M5]] == [10]
    assert published_minutes(publisher) == [5, 10]


def test_the_publisher_decides_newness_and_not_the_loop() -> None:
    """A restarted loop remembers nothing, and must not announce everything again.

    ⚠️ This is why `seen` is an optimisation rather than the guarantee. Here the loop is given
    a fresh `seen` — exactly what a process restart produces — and the publisher is what keeps
    the bar from going out twice.
    """
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = [candle(5)]
    poll_once(source, publisher, [EURUSD_M5], seen={})

    republished = poll_once(source, publisher, [EURUSD_M5], seen={})

    assert republished == {}
    assert len(publisher.published) == 1


def test_a_source_with_no_bar_yet_is_not_an_error() -> None:
    # Before the first bar of a session, and for a symbol the broker has never quoted.
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = []

    assert poll_once(source, publisher, [EURUSD_M5]) == {}
    assert publisher.published == []


def test_one_symbol_failing_does_not_stop_the_others() -> None:
    """⚠️ The failure this guard exists for is a whole feed going down for one symbol.

    A stock whose session ended, or a symbol dropped from the terminal, must not stop EURUSD
    from being collected — and the loop is a `for` over subscriptions, so without the guard it
    would.
    """
    source, publisher = FakeSource(), FakePublisher()
    source.raises.add(("AAPL", "M5"))
    source.bars[("EURUSD", "M5")] = [candle(5)]

    published = poll_once(source, publisher, [Subscription("AAPL", "M5"), EURUSD_M5])

    assert list(published) == [EURUSD_M5]


def test_a_lost_terminal_is_not_one_symbol_having_a_bad_day() -> None:
    """⚠️ The test that separates the two kinds of failure, and the loop turns on it.

    The guard above swallows a per-symbol error and moves on, which is right. Apply that same
    treatment to a `ConnectionError` and the loop spends the rest of its life logging one
    exception per subscription per poll against a terminal that is gone — the failure mode the
    whole of this PR exists to remove. So this one propagates, and the first subscription is
    enough: there is no point asking the second.
    """
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M1")] = minutes(0)
    source.down = True

    with pytest.raises(ConnectionError):
        poll_once(source, publisher, [EURUSD_M1, GBPUSD_M1])

    assert source.calls == [("EURUSD", "M1", 1)]


def test_each_subscription_is_tracked_on_its_own() -> None:
    """⚠️ Two subscriptions whose bars close at the **same instant**, which is the only scenario
    that separates the two implementations.

    An earlier version of this test used different times for the two, and a mutant that looked
    up `seen` by value across every subscription survived it — because with different times the
    two lookups agree. Two symbols on one timeframe close together every five minutes, so this
    is also the realistic case: EURUSD and GBPUSD both closing 12:05, where a `seen` that is not
    keyed per subscription silently drops the second symbol for ever.
    """
    source, publisher = FakeSource(), FakePublisher()
    gbp = Subscription("GBPUSD", "M5")
    source.bars[("EURUSD", "M5")] = [candle(5)]
    source.bars[("GBPUSD", "M5")] = [candle(5, price="1.30000")]

    published = poll_once(source, publisher, [EURUSD_M5, gbp], seen={})

    assert set(published) == {EURUSD_M5, gbp}


def test_two_timeframes_of_one_symbol_are_tracked_on_their_own() -> None:
    # The other axis of the same key: M1 must not suppress M5.
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M1")] = [candle(1)]
    source.bars[("EURUSD", "M5")] = [candle(5)]

    published = poll_once(source, publisher, [EURUSD_M1, EURUSD_M5], seen={})

    assert set(published) == {EURUSD_M1, EURUSD_M5}


# --------------------------------------------------------------------------- #
# The hole an outage leaves                                                    #
# --------------------------------------------------------------------------- #


class TestFillingAGap:
    def test_every_bar_that_closed_while_the_loop_was_away_is_published(self) -> None:
        """⚠️ The defect this PR exists for, stated as a scenario.

        Before: the poll after an outage announced the newest bar and the ones in between were
        not lost with a message, they were simply absent — a stream that skips, and a strategy
        downstream that never sees those bars and cannot know it.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen: dict[Subscription, dt.datetime] = {}
        source.bars[("EURUSD", "M1")] = minutes(0)
        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4, 5)
        published = poll_once(source, publisher, [EURUSD_M1], seen=seen)

        assert [(one.time - NOON).seconds // 60 for one in published[EURUSD_M1]] == [1, 2, 3, 4, 5]
        assert published_minutes(publisher) == [0, 1, 2, 3, 4, 5]

    def test_the_fill_goes_out_oldest_first(self) -> None:
        """Because Redis refuses an id that is not greater than the stream's last.

        ⚠️ A fill published newest-first would land its first bar and have every older one
        **refused as a duplicate** — the same rule that makes announcing a bar twice impossible
        is what makes announcing them out of order lossy. Nothing raises; the stream just ends
        up with one bar out of five.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen = {EURUSD_M1: NOON}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3)

        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        assert published_minutes(publisher) == [1, 2, 3]

    def test_a_poll_that_missed_nothing_asks_the_terminal_once(self) -> None:
        """⚠️ The normal poll must stay one call, because it is the one that runs every 5s.

        This is what stops the gap machinery from becoming a cost: a fill is a second question,
        asked only when the first answer proves it is needed.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen: dict[Subscription, dt.datetime] = {}
        source.bars[("EURUSD", "M1")] = minutes(0)
        poll_once(source, publisher, [EURUSD_M1], seen=seen)
        source.bars[("EURUSD", "M1")] = minutes(0, 1)
        source.calls.clear()

        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        assert source.calls == [("EURUSD", "M1", 1)]

    def test_the_hole_a_single_missed_poll_leaves_is_filled(self) -> None:
        """⚠️ **One** missing bar, which is the gap that actually happens.

        A poll that lands late, a terminal that hiccuped for six seconds — the everyday hole is
        one bar, not thirty, and it is the one an off-by-one in the comparison lets through
        while every larger gap still gets repaired. The bigger scenarios below cannot see that
        mistake at all.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen = {EURUSD_M1: NOON}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2)

        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        assert published_minutes(publisher) == [1, 2]

    def test_a_quiet_poll_does_not_talk_to_the_stream_at_all(self) -> None:
        """⚠️ 59 polls out of 60 have nothing to say, and the memory is what makes them
        free.

        Offering the same bar again would be *correct* — Redis refuses it — but it is a network
        round trip every five seconds, for every subscription, for ever. The comparison that
        keeps it silent is one character wide, and nothing about the published stream would
        change if it were wrong.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen: dict[Subscription, dt.datetime] = {}
        source.bars[("EURUSD", "M1")] = minutes(0)
        poll_once(source, publisher, [EURUSD_M1], seen=seen)
        assert publisher.offered == 1

        poll_once(source, publisher, [EURUSD_M1], seen=seen)
        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        assert publisher.offered == 1

    def test_a_single_bar_beyond_the_ceiling_is_still_announced(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The boundary of the short-fill warning, where a hole is exactly one bar wide.

        ⚠️ The clamped scenario below has a hole thirty bars wide, so it cannot tell a
        warning that fires one bar late from one that fires on time — and one bar quietly
        missing from a stream is still a bar the strategy never saw.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen = {EURUSD_M1: NOON}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3)

        with caplog.at_level(logging.WARNING):
            poll_once(source, publisher, [EURUSD_M1], seen=seen, max_backfill=2)

        assert published_minutes(publisher) == [2, 3]
        # 12:01 is the only bar the fill could not reach, and it has to be named.
        assert "the stream is missing" in caplog.text
        assert "12:01:00+00:00 to 2026-08-18 12:01:00+00:00" in caplog.text

    def test_a_gap_asks_for_exactly_the_bars_it_is_owed(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        seen = {EURUSD_M1: NOON}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4)
        source.calls.clear()

        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        # One call to find out where the market is, then one for the four bars owed since 12:00.
        assert source.calls == [("EURUSD", "M1", 1), ("EURUSD", "M1", 4)]

    def test_without_a_memory_there_is_no_gap_to_measure(self) -> None:
        """⚠️ Honest rather than limited: `poll_once` given no `seen` has nothing to compare a
        bar against, so it asks for one bar and lets the publisher decide. `run` is what seeds
        the memory, and it seeds it from the stream.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4)

        poll_once(source, publisher, [EURUSD_M1])

        assert source.calls == [("EURUSD", "M1", 1)]
        assert published_minutes(publisher) == [4]

    def test_a_gap_longer_than_the_ceiling_names_what_is_missing(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """⚠️ Truncating in silence would read as "caught up" to anyone looking at the stream.

        A fill has a ceiling because the count is derived from elapsed time. When the ceiling
        bites, the bars beyond it are gone as far as this loop is concerned — so it says which
        ones, and points at the tool that can still get them.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen = {EURUSD_M1: NOON}
        source.bars[("EURUSD", "M1")] = minutes(*range(40))

        with caplog.at_level(logging.WARNING):
            poll_once(source, publisher, [EURUSD_M1], seen=seen, max_backfill=5)

        assert published_minutes(publisher) == [35, 36, 37, 38, 39]
        assert "the stream is missing" in caplog.text
        # The interval, not just the fact — the first bar lost and the last.
        assert "12:01:00" in caplog.text
        assert "12:34:00" in caplog.text

    def test_a_market_closure_is_not_reported_as_a_hole(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """⚠️ The false alarm this design has to avoid, and the reason overshooting is safe.

        The count of bars owed is derived from elapsed time, and over a closure elapsed time is
        mostly not bars: here 12:09 to 20:00 is 471 minutes of clock and five bars of market. So
        the fill asks for far more than exists — and positions are contiguous over the bars that
        **do** exist, so it lands well before the hole started and nothing is missing. A loop
        that compared the count it asked for against the count it got would cry wolf every
        Monday morning.
        """
        source, publisher = FakeSource(), FakePublisher()
        seen: dict[Subscription, dt.datetime] = {}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4, 5, 6, 7, 8, 9)
        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 480, 481, 482)
        with caplog.at_level(logging.WARNING):
            poll_once(source, publisher, [EURUSD_M1], seen=seen)

        # 12:00 to 12:08 come back in the fill and are refused, because the stream already
        # holds 12:09 — the same rule that makes a duplicate impossible, doing the filtering.
        assert published_minutes(publisher) == [9, 480, 481, 482]
        assert "the stream is missing" not in caplog.text

    def test_a_fill_that_comes_back_empty_still_publishes_the_bar_in_hand(self) -> None:
        """⚠️ The guard is not here because the case is likely — it is here because of
        how it fails without it.

        The two calls are separate questions to a live terminal, so the second can legitimately
        answer with nothing the first one had. Remove the guard and `filled[0]` raises
        `IndexError`, which `poll_once` catches and reports as "could not read the last closed
        bar" — a wrong sentence, and the bar that *was* read is dropped with it. Keeping it, the
        loop falls back to what it has.
        """

        class _EmptyFill(FakeSource):
            def recent_closed(self, symbol: str, timeframe: str, count: int) -> list[Candle]:
                found = super().recent_closed(symbol, timeframe, count)
                return found if count == 1 else []

        source, publisher = _EmptyFill(), FakePublisher()
        seen = {EURUSD_M1: NOON}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3)

        poll_once(source, publisher, [EURUSD_M1], seen=seen)

        assert published_minutes(publisher) == [3]

    def test_a_bar_older_than_the_memory_is_not_a_gap(self) -> None:
        """A terminal that answers with a bar it already gave: nothing owed, nothing asked."""
        source, publisher = FakeSource(), FakePublisher()
        seen = {EURUSD_M1: NOON + dt.timedelta(minutes=5)}
        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2)

        assert poll_once(source, publisher, [EURUSD_M1], seen=seen) == {}
        assert source.calls == [("EURUSD", "M1", 1)]


# --------------------------------------------------------------------------- #
# Losing the terminal and getting it back                                      #
# --------------------------------------------------------------------------- #


class TestTheBackoff:
    def test_the_wait_doubles_and_then_stops_growing(self) -> None:
        """⚠️ A fixed delay gets one of the two cases wrong whichever number you pick.

        Short, and a machine that is down until morning reconnects thousands of times for
        nothing. Long, and a terminal restarted by hand sits idle for a minute after it is
        already back. Doubling to a ceiling is what serves both.
        """
        backoff = Backoff(first=1.0, cap=8.0)

        assert [backoff.delay(attempt) for attempt in range(1, 7)] == [1, 2, 4, 8, 8, 8]

    def test_attempts_are_counted_from_one(self) -> None:
        # Off by one here would make the first wait half of what it says it is, for ever.
        with pytest.raises(ValueError, match="counted from 1"):
            Backoff().delay(0)


class TestTheLoop:
    def test_the_terminal_is_killed_and_brought_back_without_losing_a_bar(self) -> None:
        """⚠️ **The acceptance criterion of this PR**, as a test rather than a runbook.

        The injected `sleep` is the script of events: the terminal dies during the first wait,
        five bars close while it is gone, and the reconnection brings it back. What the stream
        must show at the end is every bar in order — the reconnection on its own would only
        prove the loop kept running.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("EURUSD", "M1")] = minutes(0)
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 1:
                source.down = True
            elif len(slept) == 2:
                source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4, 5)

        published = run(source, publisher, [EURUSD_M1], every=5.0, polls=4, sleep=sleep)

        assert published_minutes(publisher) == [0, 1, 2, 3, 4, 5]
        assert published == 6
        assert source.reconnects == 1
        # Five between polls, one before the retry, five again once it is back.
        assert slept == [5.0, 1.0, 5.0]

    def test_the_wait_starts_over_after_the_terminal_comes_back(self) -> None:
        """⚠️ Two separate outages, because one cannot show a counter failing to reset.

        A `failures` that only ever grows turns the second brief hiccup of the day into a
        sixteen-second wait, and the tenth into a minute — a collector that gets slower to
        recover the longer it has been running, for no reason anyone would find in a log.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("EURUSD", "M1")] = minutes(0)
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            # Down on the first wait, and down again on the wait after it has recovered.
            if len(slept) in (1, 3):
                source.down = True

        run(source, publisher, [EURUSD_M1], every=5.0, polls=6, sleep=sleep)

        # Five between polls, one for the first retry, five, one *again* for the second — not
        # two, which is what a counter that never reset would ask for.
        assert slept == [5.0, 1.0, 5.0, 1.0, 5.0]
        assert source.reconnects == 2

    def test_a_reconnection_that_fails_costs_a_retry_and_not_the_loop(self) -> None:
        """⚠️ A terminal being restarted is unreachable for as long as that takes.

        A loop that ended on the first refused reconnection would need a human for something
        that fixes itself, which is the opposite of what a supervised collector is for.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.down = True
        source.reconnect_failures = 2
        slept: list[float] = []

        run(source, publisher, [EURUSD_M1], every=5.0, polls=4, sleep=slept.append)

        assert source.reconnects == 3
        assert slept == [1.0, 2.0, 4.0]
        assert source.down is False

    def test_a_single_shot_run_against_a_dead_terminal_fails(self) -> None:
        """⚠️ Otherwise `--once` reports success for a collector that collected nothing.

        There is no round left to recover in, so retrying would only turn a failure into a
        slower failure that exits zero.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.down = True

        with pytest.raises(ConnectionError):
            run(source, publisher, [EURUSD_M1], every=5.0, polls=1, sleep=noop)

    def test_a_restarted_process_resumes_from_the_stream(self) -> None:
        """⚠️ Without this a restart is indistinguishable from a first run.

        Two separate `run` calls share nothing but the publisher — which is exactly what a
        process being killed and started again looks like. The second one has an empty memory,
        and the only place the answer survives is the stream, so that is where it is read from.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("EURUSD", "M1")] = minutes(0)
        run(source, publisher, [EURUSD_M1], every=5.0, polls=1, sleep=noop)

        source.bars[("EURUSD", "M1")] = minutes(0, 1, 2, 3, 4, 5)
        run(source, publisher, [EURUSD_M1], every=5.0, polls=1, sleep=noop)

        assert published_minutes(publisher) == [0, 1, 2, 3, 4, 5]

    def test_a_loop_with_no_poll_budget_runs_until_it_is_interrupted(self) -> None:
        """⚠️ The real thing: `polls=None` is a loop with no natural end.

        It stops the way it is meant to — the process is killed — and `run` deliberately does
        not catch that. Ctrl-C belongs to the command that owns the terminal, which turns it
        into a clean exit; swallowing it here would leave the CLI unable to tell an interrupt
        from a loop that decided to stop on its own.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("EURUSD", "M1")] = minutes(0)
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 3:
                raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            run(source, publisher, [EURUSD_M1], every=5.0, sleep=sleep)

        assert slept == [5.0, 5.0, 5.0]
        assert published_minutes(publisher) == [0]

    def test_starting_before_the_terminal_is_up_waits_instead_of_dying(self) -> None:
        """⚠️ Boot order is not a configuration mistake, and it is the same condition
        the loop already knows how to wait out.

        The symbols cannot be selected while the terminal is down, and a collector that died
        there would need a human every time it happened to win the race against MetaTrader.
        A symbol the terminal *refuses*, on the other hand, still stops it — see below.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.down = True
        source.bars[("EURUSD", "M1")] = minutes(0)
        slept: list[float] = []

        published = run(source, publisher, [EURUSD_M1], every=5.0, polls=2, sleep=slept.append)

        assert source.reconnects == 1
        assert published == 1
        assert source.subscribed == ["EURUSD"]

    def test_every_distinct_symbol_is_selected_once_before_the_first_poll(self) -> None:
        source, publisher = FakeSource(), FakePublisher()

        run(
            source,
            publisher,
            [EURUSD_M1, EURUSD_M5, GBPUSD_M1],
            every=5.0,
            polls=1,
            sleep=noop,
        )

        # Two timeframes of EURUSD are one symbol in Market Watch, and selecting it twice would
        # be a write to the operator's terminal for nothing.
        assert source.subscribed == ["EURUSD", "GBPUSD"]

    def test_a_symbol_that_cannot_be_selected_stops_the_watch_before_it_starts(self) -> None:
        """⚠️ The silence this replaces: an unselected symbol answers "no bars" for ever, which
        is also the honest answer for a market that has not closed one yet. Polling it would
        look exactly like a quiet Sunday, indefinitely.
        """
        source, publisher = FakeSource(), FakePublisher()
        source.refuses.add("EURUSD")

        with pytest.raises(LookupError, match="Market Watch"):
            run(source, publisher, [EURUSD_M1], every=5.0, polls=1, sleep=noop)

        assert source.calls == []

    def test_the_symbols_are_selected_again_after_a_reconnection(self) -> None:
        """Market Watch is terminal state, and a reattached session is a fresh conversation.

        Re-selecting something already selected costs nothing; assuming it survived and being
        wrong costs a subscription that answers nothing for ever.
        """
        source, publisher = FakeSource(), FakePublisher()
        slept: list[float] = []

        def sleep(seconds: float) -> None:
            slept.append(seconds)
            if len(slept) == 1:
                source.down = True

        run(source, publisher, [EURUSD_M1], every=5.0, polls=3, sleep=sleep)

        assert source.subscribed == ["EURUSD", "EURUSD"]


class TestTheStreamEntry:
    def test_the_id_is_the_candle_opening_instant_in_milliseconds(self) -> None:
        """Which is what makes a duplicate impossible rather than unlikely.

        `XADD` refuses an id that is not greater than the stream's last, so republishing a bar
        is rejected by Redis itself — no bookkeeping in this process, and it survives the
        process being killed.
        """
        assert entry_id(candle(5)) == f"{int(candle(5).time.timestamp() * 1000)}-0"

    def test_two_bars_of_the_same_stream_get_increasing_ids(self) -> None:
        assert entry_id(candle(5)) < entry_id(candle(10))

    def test_a_candle_survives_the_round_trip_as_text(self) -> None:
        """⚠️ Asserted on the **text**, not on equality of the parsed candles.

        `Decimal` compares numerically, so `Decimal("1.1") == Decimal("1.10000")` is True — a
        round trip that lost the quantisation would pass an equality check while handing the
        engine a price that is no longer the tick the market printed.
        """
        original = candle(5, price="1.10005")
        fields = {str(key): str(value) for key, value in candle_fields(EURUSD_M5, original).items()}

        restored = candle_from_fields(fields)

        assert str(restored.open) == "1.10005"
        assert restored == original
        assert fields["symbol"] == "EURUSD"
        assert fields["timeframe"] == "M5"


class TestTheSyntheticSource:
    """The seam that lets everything above run on Linux — and it has to behave."""

    def test_it_satisfies_the_live_protocol(self) -> None:
        assert isinstance(SyntheticSource(), LiveSource)

    def test_the_bar_it_calls_closed_is_the_one_before_the_forming_one(self) -> None:
        """⚠️ The off-by-one that this whole module exists to avoid.

        At 12:07, the M5 bar that opened at 12:05 is still forming. The last *closed* one opened
        at 12:00 — and a source that answered 12:05 would hand the engine a bar whose high and
        low are not final, which is lookahead wearing a live-data costume.
        """
        source = SyntheticSource()
        now = dt.datetime(2026, 8, 18, 12, 7, 30, tzinfo=dt.UTC)

        found = source.recent_closed_at("EURUSD", "M5", 1, now)

        assert [one.time for one in found] == [dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)]

    def test_it_is_exactly_on_a_boundary_that_the_previous_bar_is_the_closed_one(self) -> None:
        # ⚠️ At exactly 12:05:00 the 12:05 bar has just opened, so the closed one is 12:00. An
        # implementation that floored *inclusively* would answer 12:05 here and nowhere else,
        # which is a bug that appears once every five minutes and never in a hand test.
        source = SyntheticSource()
        exact = dt.datetime(2026, 8, 18, 12, 5, tzinfo=dt.UTC)

        found = source.recent_closed_at("EURUSD", "M5", 1, exact)

        assert [one.time for one in found] == [dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)]

    def test_asking_for_several_walks_backwards_from_the_same_bar(self) -> None:
        source = SyntheticSource()
        now = dt.datetime(2026, 8, 18, 12, 7, 30, tzinfo=dt.UTC)

        found = source.recent_closed_at("EURUSD", "M5", 3, now)

        # Oldest first, ending on the same bar the singular question answers.
        assert [one.time.minute for one in found] == [50, 55, 0]

    def test_a_shut_market_simply_has_no_closed_bar(self) -> None:
        """Saturday. The synthetic feed has no weekend bars, on purpose.

        ⚠️ The live loop meets a closed market in a test here rather than for the first time on
        a real Saturday — and an empty answer, not an exception, because a shut market is an
        ordinary state of the world.
        """
        source = SyntheticSource()
        saturday = dt.datetime(2026, 8, 22, 12, 7, tzinfo=dt.UTC)

        assert source.recent_closed_at("EURUSD", "M5", 1, saturday) == []

    def test_the_same_instant_always_gives_the_same_bar(self) -> None:
        # Determinism is invariant 2. A live source that drifted would make every test above
        # flaky for reasons that have nothing to do with the loop.
        source = SyntheticSource()
        now = dt.datetime(2026, 8, 18, 12, 7, tzinfo=dt.UTC)

        assert source.recent_closed_at("EURUSD", "M5", 1, now) == source.recent_closed_at(
            "EURUSD", "M5", 1, now
        )

    def test_an_unknown_timeframe_is_refused_before_anything_is_polled(self) -> None:
        with pytest.raises(ValueError, match="unknown timeframe"):
            SyntheticSource().recent_closed_at("EURUSD", "M7", 1, dt.datetime.now(tz=dt.UTC))

    def test_a_poll_asks_for_at_least_one_bar(self) -> None:
        with pytest.raises(ValueError, match="at least one bar"):
            SyntheticSource().recent_closed_at("EURUSD", "M5", 0, dt.datetime.now(tz=dt.UTC))

    def test_subscribing_to_a_symbol_it_does_not_have_is_refused(self) -> None:
        """⚠️ There is no Market Watch here, but the *contract* is what has to survive.

        Subscribing is the moment a typo is supposed to be caught. A synthetic source that
        shrugged at an unknown symbol would let `EURSUD` run as a permanently empty watch —
        the exact failure the real `subscribe` was added to prevent.
        """
        with pytest.raises(ValueError, match="no synthetic instrument"):
            SyntheticSource().subscribe("EURSUD")
