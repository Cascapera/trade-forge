"""The live loop and a broker that rewrites what was already published (14/10).

XP's `WIN$` is adjusted by a ratio at every roll: when WINV26 expired, every past bar moved up
2.1 %. The sessions had warmed on the old scale, the next bar came in the new one, and the step
read as a bullish BOS the market never made — signal #38, annulled. Given an `on_rescale`, the
loop reads the bars it remembers again with every new one, holds the new bar back when they were
rewritten, and watches the pair again from scratch once the series is mended.
"""

import datetime as dt
from decimal import Decimal

from tradeforge_collector.live import Subscription, run_on_demand
from tradeforge_collector.rescale import Comparison
from tradeforge_engine.domain import Candle

from .test_live import NOON, FakePublisher, FakeSource, candle, noop, published_minutes

WIN_M1 = Subscription("WIN", "M1")
WIN_H1 = Subscription("WIN", "H1")
OLD, NEW = "1.10000", "1.12343"
"""A price before and after the roll: 2.13 % apart, as XP's `WIN$` was on 14/10."""


def priced(*offsets: int, price: str = OLD) -> list[Candle]:
    return [candle(offset, price=price) for offset in offsets]


def hourly(count: int, price: str) -> list[Candle]:
    return [
        Candle(
            time=NOON + dt.timedelta(hours=n),
            open=Decimal(price),
            high=Decimal(price),
            low=Decimal(price),
            close=Decimal(price),
            tick_volume=7,
            spread=2,
        )
        for n in range(count)
    ]


class Rescues:
    """`on_rescale` as the CLI wires it, recorded: what it was told, and whether it mended it."""

    def __init__(self, *, mends: bool = True) -> None:
        self.calls: list[tuple[Subscription, float]] = []
        self.mends = mends

    def __call__(self, pair: Subscription, comparison: Comparison) -> bool:
        self.calls.append((pair, comparison.ratio))
        return self.mends


def test_the_new_bar_is_held_back_until_the_series_is_mended_then_watched_again() -> None:
    source, publisher, rescues = FakeSource(), FakePublisher(), Rescues()
    source.bars[("WIN", "M1")] = priced(*range(40))
    # What the loop remembers of a pair it starts watching: the disk's or the stream's last bars
    # — the old scale, and the new one once the series has been collected again.
    remembered = {"price": OLD}
    rounds = [0]

    def demand() -> list[Subscription]:
        rounds[0] += 1
        if rounds[0] == 2:
            source.bars[("WIN", "M1")] = priced(*range(41))
        if rounds[0] == 3:
            source.bars[("WIN", "M1")] = priced(*range(42), price=NEW)
            remembered["price"] = NEW
        return [WIN_M1]

    run_on_demand(
        source,
        publisher,
        demand,
        every=0,
        polls=4,
        sleep=noop,
        tail=lambda _pair: priced(*range(10, 40), price=remembered["price"]),
        on_rescale=rescues,
    )

    assert len(rescues.calls) == 1
    pair, ratio = rescues.calls[0]
    assert pair == WIN_M1
    assert abs(ratio - 1.0213) < 1e-3
    # 39 on the first round, 40 on the second; 41 held back on the third and published on the
    # fourth, once the pair was watched again from scratch — never before the rescue.
    assert published_minutes(publisher) == [39, 40, 41]
    assert source.subscribed == ["WIN", "WIN"], "subscribed again, as a pair asked anew"


def test_an_unchanged_broker_is_never_reported() -> None:
    source, publisher, rescues = FakeSource(), FakePublisher(), Rescues()
    rounds = [0]

    def demand() -> list[Subscription]:
        rounds[0] += 1
        source.bars[("WIN", "M1")] = priced(*range(39 + rounds[0]))
        return [WIN_M1]

    run_on_demand(
        source,
        publisher,
        demand,
        every=0,
        polls=4,
        sleep=noop,
        tail=lambda _pair: priced(*range(10, 40)),
        on_rescale=rescues,
    )

    assert rescues.calls == []
    assert published_minutes(publisher) == [39, 40, 41, 42]


def test_a_rescue_that_fails_publishes_in_the_new_scale_and_is_not_asked_again() -> None:
    """Nothing better can be done than to go on — the sessions are restarted by hand — and asking
    again at every bar would collect the whole series again every minute."""
    source, publisher, rescues = FakeSource(), FakePublisher(), Rescues(mends=False)
    rounds = [0]

    def demand() -> list[Subscription]:
        rounds[0] += 1
        # The old scale on the first round; from the second on, the roll and one bar a round.
        price = OLD if rounds[0] == 1 else NEW
        source.bars[("WIN", "M1")] = priced(*range(39 + rounds[0]), price=price)
        return [WIN_M1]

    run_on_demand(
        source,
        publisher,
        demand,
        every=0,
        polls=4,
        sleep=noop,
        tail=lambda _pair: priced(*range(10, 40)),
        on_rescale=rescues,
    )

    assert len(rescues.calls) == 1
    assert published_minutes(publisher) == [39, 40, 41, 42]


def test_a_symbol_is_rescued_once_whatever_the_timeframes_it_is_watched_on() -> None:
    """The rescue collects every timeframe of the symbol and restarts all its markets: a second
    call for the same roll would do the whole of it twice."""
    source, publisher, rescues = FakeSource(), FakePublisher(), Rescues()
    source.bars[("WIN", "M1")] = priced(*range(40))
    source.bars[("WIN", "H1")] = hourly(40, OLD)
    rounds = [0]

    def demand() -> list[Subscription]:
        rounds[0] += 1
        if rounds[0] == 2:
            source.bars[("WIN", "M1")] = priced(*range(41), price=NEW)
            source.bars[("WIN", "H1")] = hourly(41, NEW)
        return [WIN_M1, WIN_H1]

    tails = {pair: source.bars[(pair.symbol, pair.timeframe)][-30:] for pair in (WIN_M1, WIN_H1)}

    run_on_demand(
        source,
        publisher,
        demand,
        every=0,
        polls=2,
        sleep=noop,
        tail=tails.__getitem__,
        on_rescale=rescues,
    )

    assert len(rescues.calls) == 1


def test_without_a_rescue_the_loop_reads_what_it_always_read() -> None:
    """No `on_rescale`, no memory: one bar asked for per poll, as before 14/10."""
    source, publisher = FakeSource(), FakePublisher()
    rounds = [0]

    def demand() -> list[Subscription]:
        rounds[0] += 1
        source.bars[("WIN", "M1")] = priced(*range(39 + rounds[0]))
        return [WIN_M1]

    run_on_demand(source, publisher, demand, every=0, polls=3, sleep=noop)

    assert [count for symbol, _, count in source.calls if symbol == "WIN"] == [1, 1, 1]
