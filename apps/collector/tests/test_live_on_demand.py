"""One broker's live loop watching what sessions ask for (ADR-0032, signals PR 3)."""

import time
from collections.abc import Callable

from tradeforge_collector.demand import DemandLease, demand_key, release, want, wanted
from tradeforge_collector.live import Subscription, TickerLiveSource, run_on_demand

from .test_live import FakePublisher, FakeSource, minutes, noop, published_minutes

WIN_M5 = Subscription("WIN", "M5")
PETR4_M5 = Subscription("PETR4", "M5")


class FakeRedis:
    """A sorted set per key, enough for the demand: add, remove, range, drop by score."""

    def __init__(self) -> None:
        self.sets: dict[str, dict[str, float]] = {}

    def zadd(self, name: str, mapping: dict[str, float]) -> int:
        self.sets.setdefault(name, {}).update(mapping)
        return len(mapping)

    def zrem(self, name: str, *values: str) -> int:
        found = self.sets.get(name, {})
        return sum(found.pop(value, None) is not None for value in values)

    def zremrangebyscore(self, name: str, low: float | str, high: float | str, /) -> int:
        found = self.sets.get(name, {})
        lapsed = [member for member, score in found.items() if score <= float(high)]
        for member in lapsed:
            del found[member]
        return len(lapsed)

    def zrange(self, name: str, start: int, end: int) -> list[str]:
        return sorted(self.sets.get(name, {}), key=lambda member: self.sets[name][member])


def clock(at: list[float]) -> Callable[[], float]:
    return lambda: at[0]


class TestTheDemand:
    def test_an_ask_is_kept_per_broker_until_its_lease_lapses(self) -> None:
        redis, at = FakeRedis(), [1000.0]
        want(redis, "xp", WIN_M5, lease=90, now=clock(at))
        want(redis, "tradeview", Subscription("AAPL", "M15"), lease=90, now=clock(at))

        assert wanted(redis, "xp", now=clock(at)) == [WIN_M5]
        at[0] = 1089.0
        assert wanted(redis, "xp", now=clock(at)) == [WIN_M5]
        at[0] = 1091.0
        assert wanted(redis, "xp", now=clock(at)) == []
        assert demand_key("xp") not in redis.sets or not redis.sets[demand_key("xp")]

    def test_renewing_moves_the_lapse_forward_and_release_ends_it_at_once(self) -> None:
        redis, at = FakeRedis(), [1000.0]
        want(redis, "xp", WIN_M5, lease=90, now=clock(at))
        at[0] = 1060.0
        want(redis, "xp", WIN_M5, lease=90, now=clock(at))
        at[0] = 1120.0
        assert wanted(redis, "xp", now=clock(at)) == [WIN_M5]

        release(redis, "xp", WIN_M5)
        assert wanted(redis, "xp", now=clock(at)) == []

    def test_a_lease_asks_at_start_renews_and_withdraws_on_stop(self) -> None:
        redis = FakeRedis()
        lease = DemandLease(redis, "xp", WIN_M5, lease=0.06).start()
        assert wanted(redis, "xp") == [WIN_M5]  # asked before start() returned
        time.sleep(0.15)  # past two whole leases: only renewal keeps it
        assert wanted(redis, "xp") == [WIN_M5]

        lease.stop()
        assert wanted(redis, "xp") == []


class TestTheLoopOnDemand:
    def test_a_pair_is_watched_once_asked_and_dropped_once_not(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("WIN", "M5")] = minutes(0, 5)
        asked = [[WIN_M5], [WIN_M5], []]
        rounds = iter(asked)

        run_on_demand(source, publisher, lambda: next(rounds), every=0, polls=3, sleep=noop)

        assert source.subscribed == ["WIN"]
        # The first poll has no position to fill a gap from, so only the newest bar goes out.
        assert [subscription for subscription, _ in publisher.published] == [WIN_M5]
        polled_rounds = [call for call in source.calls if call[0] == "WIN"]
        assert len(polled_rounds) == 2, "the third round asked for nothing and polled nothing"

    def test_a_refused_pair_is_set_aside_without_stopping_the_others(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        source.refuses.add("NOPE")
        source.bars[("PETR4", "M5")] = minutes(0)
        nope = Subscription("NOPE", "M5")

        run_on_demand(source, publisher, lambda: [nope, PETR4_M5], every=0, polls=3, sleep=noop)

        assert source.subscribed == ["PETR4"]
        assert not any(call[0] == "NOPE" for call in source.calls), "never polled once refused"
        assert [subscription for subscription, _ in publisher.published] == [PETR4_M5]

    def test_a_lost_terminal_is_waited_out_and_the_pairs_come_back(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("WIN", "M5")] = minutes(0)

        rounds = [0]

        def demand() -> list[Subscription]:
            rounds[0] += 1
            if rounds[0] == 2:
                source.down = True  # only the second round finds the terminal gone
            return [WIN_M5]

        run_on_demand(source, publisher, demand, every=0, polls=3, sleep=noop)

        assert source.reconnects == 1
        assert [subscription for subscription, _ in publisher.published] == [WIN_M5]


def test_an_internal_name_is_watched_as_the_brokers_ticker() -> None:
    source = FakeSource()
    source.bars[("WIN$", "M5")] = minutes(0, 5)
    watched = TickerLiveSource(source, {"WIN": "WIN$"})

    watched.subscribe("WIN")
    bars = watched.recent_closed("WIN", "M5", 2)
    watched.subscribe("PETR4")  # not in the map: its own name

    assert source.subscribed == ["WIN$", "PETR4"]
    assert len(bars) == 2


class TestCatchingUpFromTheDisk:
    """09/10: a pair new to its stream started at today's bar, and a session warmed on a disk a
    week behind read the hole as a move — the first live signal ever posted was that hole."""

    WIN_M1 = Subscription("WIN", "M1")

    def test_a_pair_new_to_its_stream_is_caught_up_from_the_last_bar_on_disk(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("WIN", "M1")] = minutes(*range(21))
        disk_end = minutes(10)[0].time

        run_on_demand(
            source,
            publisher,
            lambda: [self.WIN_M1],
            every=0,
            polls=1,
            sleep=noop,
            on_disk=lambda _pair: disk_end,
        )

        assert published_minutes(publisher) == list(range(11, 21))

    def test_only_what_came_after_the_disk_is_published_when_the_market_was_shut(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        # Shut from minute 3 to 14: asked by position, the fill reaches back past the disk.
        source.bars[("WIN", "M1")] = minutes(0, 1, 2, *range(15, 21))

        run_on_demand(
            source,
            publisher,
            lambda: [self.WIN_M1],
            every=0,
            polls=1,
            sleep=noop,
            on_disk=lambda _pair: minutes(1)[0].time,
        )

        assert published_minutes(publisher) == [2, *range(15, 21)]

    def test_a_pair_its_stream_already_knows_resumes_from_the_stream_not_the_disk(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("WIN", "M1")] = minutes(*range(21))
        publisher.publish(self.WIN_M1, minutes(18)[0])
        asked: list[Subscription] = []

        def on_disk(pair: Subscription) -> None:
            asked.append(pair)

        run_on_demand(
            source,
            publisher,
            lambda: [self.WIN_M1],
            every=0,
            polls=1,
            sleep=noop,
            on_disk=on_disk,
        )

        assert asked == []
        assert published_minutes(publisher) == [18, 19, 20]

    def test_nothing_on_disk_starts_at_the_newest_bar_as_before(self) -> None:
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("WIN", "M1")] = minutes(*range(21))

        run_on_demand(
            source,
            publisher,
            lambda: [self.WIN_M1],
            every=0,
            polls=1,
            sleep=noop,
            on_disk=lambda _pair: None,
        )

        assert published_minutes(publisher) == [20]

    def test_a_long_outage_resumed_from_the_stream_reaches_as_far_as_a_catch_up(self) -> None:
        """A machine off for a week owes more than an outage: past `max_backfill`, the hole
        stayed inside the stream and the session traded across it."""
        source, publisher = FakeSource(), FakePublisher()
        source.bars[("WIN", "M1")] = minutes(*range(30))
        publisher.publish(self.WIN_M1, minutes(5)[0])

        run_on_demand(
            source,
            publisher,
            lambda: [self.WIN_M1],
            every=0,
            polls=1,
            sleep=noop,
            max_backfill=3,
        )

        assert published_minutes(publisher) == list(range(5, 30))
