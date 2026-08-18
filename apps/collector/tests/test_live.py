"""The live loop, exercised without a terminal and without a Redis.

Every test here runs on Linux CI. That is not a convenience: the loop is the piece of phase 3
that decides whether a bar closed, and a component whose behaviour can only be observed against
a broker is one whose behaviour is observed once, by hand, on a Windows box.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_collector.live import (
    CandlePublisher,
    LiveSource,
    Subscription,
    poll_once,
    stream_name,
)
from tradeforge_collector.publisher import candle_fields, candle_from_fields, entry_id
from tradeforge_collector.source import Candle
from tradeforge_collector.synthetic import SyntheticSource

EURUSD_M5 = Subscription("EURUSD", "M5")
EURUSD_M1 = Subscription("EURUSD", "M1")


def candle(minute: int, *, price: str = "1.10000") -> Candle:
    return Candle(
        time=dt.datetime(2026, 8, 18, 12, minute, tzinfo=dt.UTC),
        open=Decimal(price),
        high=Decimal(price),
        low=Decimal(price),
        close=Decimal(price),
        tick_volume=7,
        spread=2,
        real_volume=0,
    )


class FakeSource:
    """A source whose last closed bar is whatever the test last put there."""

    def __init__(self) -> None:
        self.bars: dict[tuple[str, str], Candle | None] = {}
        self.raises: set[tuple[str, str]] = set()

    def latest_closed(self, symbol: str, timeframe: str) -> Candle | None:
        if (symbol, timeframe) in self.raises:
            raise ConnectionError("the terminal went away")
        return self.bars.get((symbol, timeframe))


class FakePublisher:
    """Remembers what it was given, and refuses a bar it already has — like the real one."""

    def __init__(self) -> None:
        self.published: list[tuple[Subscription, Candle]] = []
        self._ids: set[tuple[Subscription, dt.datetime]] = set()

    def publish(self, subscription: Subscription, candle: Candle) -> bool:
        key = (subscription, candle.time)
        if key in self._ids:
            return False
        self._ids.add(key)
        self.published.append((subscription, candle))
        return True


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


def test_a_bar_is_published_once_however_often_it_is_polled() -> None:
    """The core of the loop: polling is frequent, closing is not."""
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = candle(5)

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
    source.bars[("EURUSD", "M5")] = candle(5)
    poll_once(source, publisher, [EURUSD_M5])

    source.bars[("EURUSD", "M5")] = candle(10)
    published = poll_once(source, publisher, [EURUSD_M5])

    assert published[EURUSD_M5].time.minute == 10
    assert [one.time.minute for _, one in publisher.published] == [5, 10]


def test_the_publisher_decides_newness_and_not_the_loop() -> None:
    """A restarted loop remembers nothing, and must not announce everything again.

    ⚠️ This is why `seen` is an optimisation rather than the guarantee. Here the loop is given
    a fresh `seen` — exactly what a process restart produces — and the publisher is what keeps
    the bar from going out twice.
    """
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = candle(5)
    poll_once(source, publisher, [EURUSD_M5], seen={})

    republished = poll_once(source, publisher, [EURUSD_M5], seen={})

    assert republished == {}
    assert len(publisher.published) == 1


def test_a_source_with_no_bar_yet_is_not_an_error() -> None:
    # Before the first bar of a session, and for a symbol the broker has never quoted.
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M5")] = None

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
    source.bars[("EURUSD", "M5")] = candle(5)

    published = poll_once(source, publisher, [Subscription("AAPL", "M5"), EURUSD_M5])

    assert list(published) == [EURUSD_M5]


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
    source.bars[("EURUSD", "M5")] = candle(5)
    source.bars[("GBPUSD", "M5")] = candle(5, price="1.30000")

    published = poll_once(source, publisher, [EURUSD_M5, gbp], seen={})

    assert set(published) == {EURUSD_M5, gbp}


def test_two_timeframes_of_one_symbol_are_tracked_on_their_own() -> None:
    # The other axis of the same key: M1 must not suppress M5.
    source, publisher = FakeSource(), FakePublisher()
    source.bars[("EURUSD", "M1")] = candle(1)
    source.bars[("EURUSD", "M5")] = candle(5)

    published = poll_once(source, publisher, [EURUSD_M1, EURUSD_M5], seen={})

    assert set(published) == {EURUSD_M1, EURUSD_M5}


class TestTheStreamEntry:
    def test_the_id_is_the_candle_opening_instant_in_milliseconds(self) -> None:
        """Which is what makes a duplicate impossible rather than unlikely.

        ⚠️ Redis refuses an id that is not greater than the last one in the stream, so
        re-announcing a bar is refused *by the store* — and that survives the loop being killed
        and restarted, which an in-process set does not.
        """
        # 2026-08-18T12:05:00Z. Written out rather than computed from the candle, because a
        # test that recomputes the implementation cannot disagree with it.
        assert entry_id(candle(5)) == "1787054700000-0"
        # Two different bars, two different ids, in the order they closed.
        assert entry_id(candle(5)) < entry_id(candle(10))

    def test_a_candle_survives_the_round_trip_through_a_stream_entry(self) -> None:
        """⚠️ Prices go out as decimal text, not as floats.

        The engine prices in `Decimal` so that a tick is a tick. A float at the edge would give
        that back for nothing, and the loss would appear as a price nobody can reconcile with
        the broker's own.
        """
        original = Candle(
            time=dt.datetime(2026, 8, 18, 12, 5, tzinfo=dt.UTC),
            open=Decimal("1.10005"),
            high=Decimal("1.10009"),
            low=Decimal("1.09998"),
            close=Decimal("1.10001"),
            tick_volume=42,
            spread=3,
            real_volume=1,
        )
        fields = candle_fields(EURUSD_M5, original)

        assert candle_from_fields({str(k): str(v) for k, v in fields.items()}) == original
        # ⚠️ And the **text** is the quantised price, not a number that happens to be equal.
        # `Decimal` compares numerically, so `1.1 == 1.10000` and equality alone cannot see a
        # float round trip — which turns `1.10000` into `1.1` and throws away the precision the
        # instrument is quoted at. A consumer rebuilding from `1.1` gets a different exponent
        # and a price nobody can line up against the broker's own quote.
        assert fields["open"] == "1.10005"
        assert fields["close"] == "1.10001"
        # All four price fields, and against a bar quoted at the instrument's own precision:
        # `1.10001` survives a float trip untouched, so asserting only on it would leave the
        # loss invisible. `1.10000` does not — it comes back `1.1`.
        quantised = candle_fields(EURUSD_M5, candle(5))
        assert [quantised[field] for field in ("open", "high", "low", "close")] == [
            "1.10000",
            "1.10000",
            "1.10000",
            "1.10000",
        ]
        # And the subscription travels with it, so a consumer reading a merged view still knows
        # what it is holding.
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

        found = source.latest_closed_at("EURUSD", "M5", now)

        assert found is not None
        assert found.time == dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)

    def test_it_is_exactly_on_a_boundary_that_the_previous_bar_is_the_closed_one(self) -> None:
        # ⚠️ At exactly 12:05:00 the 12:05 bar has just opened, so the closed one is 12:00. An
        # implementation that floored *inclusively* would answer 12:05 here and nowhere else,
        # which is a bug that appears once every five minutes and never in a hand test.
        source = SyntheticSource()
        exact = dt.datetime(2026, 8, 18, 12, 5, tzinfo=dt.UTC)

        found = source.latest_closed_at("EURUSD", "M5", exact)

        assert found is not None
        assert found.time == dt.datetime(2026, 8, 18, 12, 0, tzinfo=dt.UTC)

    def test_a_shut_market_simply_has_no_closed_bar(self) -> None:
        """Saturday. The synthetic feed has no weekend bars, on purpose.

        ⚠️ The live loop meets a closed market in a test here rather than for the first time on
        a real Saturday — and `None` is the answer, not an exception, because a shut market is
        an ordinary state of the world.
        """
        source = SyntheticSource()
        saturday = dt.datetime(2026, 8, 22, 12, 7, tzinfo=dt.UTC)

        assert source.latest_closed_at("EURUSD", "M5", saturday) is None

    def test_the_same_instant_always_gives_the_same_bar(self) -> None:
        # Determinism is invariant 2. A live source that drifted would make every test above
        # flaky for reasons that have nothing to do with the loop.
        source = SyntheticSource()
        now = dt.datetime(2026, 8, 18, 12, 7, tzinfo=dt.UTC)

        assert source.latest_closed_at("EURUSD", "M5", now) == source.latest_closed_at(
            "EURUSD", "M5", now
        )

    def test_an_unknown_timeframe_is_refused_before_anything_is_polled(self) -> None:
        with pytest.raises(ValueError, match="unknown timeframe"):
            SyntheticSource().latest_closed_at("EURUSD", "M7", dt.datetime.now(tz=dt.UTC))
