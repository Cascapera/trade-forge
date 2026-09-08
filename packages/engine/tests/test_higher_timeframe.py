"""His higher-timeframe filter: a region above releases one entry below.

Two layers, tested apart and then together. `BarAggregator` is arithmetic on bars and is tested
on hand-made hourly candles. `HigherTimeframeGate` is fed a real stream and asked its questions
directly. Then `StructureStrategy` is driven with the filter on and off over the *same* stream,
because the filter's whole claim is about the difference between the two.

**The stream is built from the golden, twice.** The order-block golden (`BULLISH_START` +
`GAPPING_IMPULSE`) is played first as the *higher* timeframe — each of its bars expanded into
four hourly ones that assemble back into it exactly, so the H4 detector reads the golden it was
validated on — and then again as the hourly stream, shifted up two so that its impulse breaks
the high the prefix left. The higher run leaves the author's demand region at [80, 100] (bar 3
dug down to 80, which moves nothing else: it is already the leg's origin); the hourly run comes
back to touch it and marks its own zone at [88, 92], which is the one the filter releases.

⚠️ **Every number below was read off a probe, not reasoned** (`docs/aulas/PR-205`). The two
detectors share the hourly stream, so the hourly structure's state is coloured by the prefix,
and the first expansion tried — flat sub-bars — gave the hourly machine nothing to arm on at all.
The stepped expansion is what makes it produce breaks; which bars they land on is the probe's
answer, and the tests assert those bars.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import Decimal, localcontext

import pytest

from tradeforge_engine.domain import (
    Candle,
    Context,
    Position,
    Refusal,
    RefusedBy,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.higher_timeframe import (
    GIVE_UP_AT_REGION_HEIGHTS,
    BarAggregator,
    HigherTimeframeGate,
)
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.setups import SetupContext, StructureStrategy
from tradeforge_engine.structure import OrderBlock, StructureKind, ZoneKind
from tradeforge_engine.testing import (
    AAPL,
    BULLISH_START,
    GAPPING_IMPULSE,
    HOUR,
    START,
    ImmediateFillBroker,
    bar,
)

H4 = 4 * HOUR
_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()
_TICK = Decimal("0.01")


def _at(index: int) -> dt.datetime:
    return START + index * HOUR


def _index_of(candle: Candle) -> int:
    return round((candle.time - START) / HOUR)


def _sub(time: dt.datetime, open_: Decimal, close: Decimal) -> Candle:
    return Candle(time=time, open=open_, high=max(open_, close), low=min(open_, close), close=close)


def _expand(candle: Candle, first_hour: int) -> list[Candle]:
    """Four hourly bars that assemble back into `candle` exactly, stepping in its direction.

    An up bar dips to its low first and climbs to its high in two strictly rising steps; a down
    bar mirrors that. The strictness is the point: the hourly structure arms on three bars with
    strictly higher (or lower) highs *and* lows, and flat sub-bars never give it one. The tick
    nudges are what keep consecutive lows strictly apart without moving any extreme.
    """
    open_, high, low, close = candle.open, candle.high, candle.low, candle.close
    times = [_at(first_hour + i) for i in range(4)]
    if close >= open_:
        middle = low + (high - low) / 2
        return [
            _sub(times[0], open_, low),
            _sub(times[1], low + _TICK, middle),
            _sub(times[2], middle + _TICK, high),
            _sub(times[3], high - _TICK, close),
        ]
    middle = high - (high - low) / 2
    return [
        _sub(times[0], open_, high),
        _sub(times[1], high - _TICK, middle),
        _sub(times[2], middle - _TICK, low),
        _sub(times[3], low + _TICK, close),
    ]


def _shift(candles: list[Candle], *, hours: int, price: Decimal) -> list[Candle]:
    return [
        Candle(
            time=candle.time + hours * HOUR,
            open=candle.open + price,
            high=candle.high + price,
            low=candle.low + price,
            close=candle.close + price,
        )
        for candle in candles
    ]


_MIRROR_AXIS = Decimal(200)


def _mirror(candles: list[Candle]) -> list[Candle]:
    """Reflect about a price: the demand scenario becomes its supply twin, extremes swapped."""
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


# The golden as the higher timeframe: eighteen H4 bars, hours 0-71. Bar 3 dug down to 80 so the
# region it marks is [80, 100] — wide enough that the hourly run below touches it (low 92) without
# closing through it (lowest close 89). The impulse also leaves the secondary [110, 117].
_HTF_IMPULSE = [*GAPPING_IMPULSE]
_HTF_IMPULSE[3] = bar(3, open_="99", close="99", high="100", low="80")
PREFIX = [
    sub
    for position, candle in enumerate([*BULLISH_START, *_HTF_IMPULSE])
    for sub in _expand(candle, 4 * position)
]
# The golden again as the hourly stream, hours 72-89, two higher so its bar-9 close of 126 breaks
# the 125 top the prefix left. Its bar -8 (hour 72) is the touch: low 92 <= 100.
BASE = _shift([*BULLISH_START, *GAPPING_IMPULSE], hours=80, price=Decimal(2))
STREAM = [*PREFIX, *BASE]

TOUCH = 72
"""The hourly bar that reaches [80, 100]; the release opens here."""
ARMS = 89
"""The hourly bullish BOS (126 > 125) that marks [88, 92] — the one zone armed under the filter."""


@dataclass
class _Marked:
    """A stand-in setup: qualify the first zone the detector marks, on the bar it marks it."""

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        return context.marked[0] if context.marked else None


@dataclass
class _Names:
    """Remembers every zone marked, in order, and names one of them on chosen bars."""

    picks: dict[int, int]
    seen: list[OrderBlock] = field(default_factory=list)

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.seen.extend(context.marked)
        pick = self.picks.get(_index_of(context.candle))
        return self.seen[pick] if pick is not None and pick < len(self.seen) else None


def _drive(
    strategy: StructureStrategy,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
    refused_on: dict[int, RefusedBy] | None = None,
) -> dict[int, list[Signal]]:
    """Feed candles one at a time; the signals each bar produced, keyed by bar number.

    `refused_on` hands the strategy, on the named bar, a refusal of the last entry it asked for
    — the way `Context.refusals` does, one bar after the order was placed.
    """
    out: dict[int, list[Signal]] = {}
    last_entry: Signal | None = None
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            index = _index_of(candle)
            position = None
            if index in position_on:
                position = Position(
                    symbol=AAPL.symbol,
                    side=Side.LONG,
                    volume=Decimal(1),
                    entry_price=candle.open,
                    entry_time=candle.time,
                )
            refusals: tuple[Refusal, ...] = ()
            refused_by = (refused_on or {}).get(index)
            if refused_by is not None and last_entry is not None:
                assert last_entry.client_id is not None
                refusals = (
                    Refusal(
                        client_id=last_entry.client_id,
                        intent=SignalKind.ENTRY,
                        refused_by=refused_by,
                        reason=last_entry.reason,
                        detail="the venue would not take it",
                    ),
                )
            context = Context(
                candle=candle,
                instrument=AAPL,
                account=_ACCOUNT,
                position=position,
                refusals=refusals,
            )
            out[index] = list(strategy.on_bar(context))
            for signal in out[index]:
                if signal.kind is SignalKind.ENTRY:
                    last_entry = signal
    return out


def _entries(signals: dict[int, list[Signal]]) -> dict[int, Signal]:
    return {
        index: signal
        for index, bar_signals in signals.items()
        for signal in bar_signals
        if signal.kind is SignalKind.ENTRY
    }


def _demand(confirmed_at: dt.datetime) -> OrderBlock:
    """A hand-built hourly zone, for asking the gate about a break confirmed at a given bar."""
    return OrderBlock(
        kind=ZoneKind.DEMAND,
        top=Decimal(92),
        bottom=Decimal(88),
        time=confirmed_at - HOUR,
        confirmed_at=confirmed_at,
        break_kind=StructureKind.BOS,
        primary=True,
    )


def _fed(candles: list[Candle], *, target: dt.timedelta = H4) -> HigherTimeframeGate:
    gate = HigherTimeframeGate(base=HOUR, target=target)
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            gate.observe(candle)
    return gate


def _through(hour: int) -> list[Candle]:
    return [candle for candle in STREAM if _index_of(candle) <= hour]


# --------------------------------------------------------------------------- #
# The aggregator                                                                #
# --------------------------------------------------------------------------- #


def test_four_hourly_bars_make_one_h4_bar_with_the_widest_extremes() -> None:
    bars = BarAggregator(base=HOUR, target=H4)
    hourly = [
        bar(0, open_="100", close="101", high="102", low="99", tick_volume=5),
        bar(1, open_="101", close="103", high="105", low="100", tick_volume=7),
        bar(2, open_="103", close="98", high="104", low="97", tick_volume=1),
        bar(3, open_="98", close="100", high="101", low="98", tick_volume=2),
    ]
    completed = [done for candle in hourly for done in bars.update(candle)]

    assert len(completed) == 1
    (h4,) = completed
    assert (h4.time, h4.open, h4.high, h4.low, h4.close) == (
        _at(0),
        Decimal(100),
        Decimal(105),
        Decimal(97),
        Decimal(100),
    )
    assert h4.tick_volume == 15


def test_the_h4_bar_is_returned_on_the_hourly_bar_that_ends_it_and_only_then() -> None:
    """The 03:00 bar closes at 04:00, the bucket's edge — the H4 is complete on that bar's own
    update, not one bar later. And the next hourly bar returns nothing again."""
    bars = BarAggregator(base=HOUR, target=H4)
    assert [bars.update(bar(i, open_="1", close="1")) for i in range(3)] == [(), (), ()]
    (h4,) = bars.update(bar(3, open_="1", close="1"))
    assert h4.time == _at(0)
    assert bars.update(bar(4, open_="1", close="1")) == ()


def test_a_bucket_left_open_is_flushed_by_the_first_bar_of_a_later_one() -> None:
    """A session ends and the bar that would have closed the bucket never comes. The next bucket's
    first bar returns the partial one — one bar late, which is the only honest timing."""
    bars = BarAggregator(base=HOUR, target=H4)
    assert bars.update(bar(4, open_="10", close="11", high="12", low="9")) == ()
    assert bars.update(bar(5, open_="11", close="13", high="14", low="10")) == ()
    (partial,) = bars.update(bar(12, open_="20", close="21"))
    assert (partial.time, partial.open, partial.high, partial.low, partial.close) == (
        _at(4),
        Decimal(10),
        Decimal(14),
        Decimal(9),
        Decimal(13),
    )


def test_one_bar_can_flush_the_old_bucket_and_end_its_own() -> None:
    bars = BarAggregator(base=HOUR, target=H4)
    bars.update(bar(4, open_="10", close="11"))
    completed = bars.update(bar(15, open_="20", close="21"))
    assert [done.time for done in completed] == [_at(4), _at(12)]


def test_a_bucket_still_open_is_never_returned() -> None:
    bars = BarAggregator(base=HOUR, target=H4)
    assert [bars.update(bar(i, open_="1", close="1")) for i in range(3)] == [(), (), ()]


@pytest.mark.parametrize(
    ("target", "moment", "bucket"),
    [
        (
            H4,
            dt.datetime(2024, 1, 1, 5, 30, tzinfo=dt.UTC),
            dt.datetime(2024, 1, 1, 4, tzinfo=dt.UTC),
        ),
        (H4, dt.datetime(2024, 1, 1, 4, tzinfo=dt.UTC), dt.datetime(2024, 1, 1, 4, tzinfo=dt.UTC)),
        (
            dt.timedelta(days=1),
            dt.datetime(2024, 1, 3, 23, 45, tzinfo=dt.UTC),
            dt.datetime(2024, 1, 3, tzinfo=dt.UTC),
        ),
        # 2024-01-03 is a Wednesday; the week's bucket opens on the Monday before it.
        (
            dt.timedelta(weeks=1),
            dt.datetime(2024, 1, 3, 10, tzinfo=dt.UTC),
            dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        ),
    ],
)
def test_buckets_close_on_the_utc_clock(
    target: dt.timedelta, moment: dt.datetime, bucket: dt.datetime
) -> None:
    assert BarAggregator(base=dt.timedelta(minutes=15), target=target).bucket_of(moment) == bucket


@pytest.mark.parametrize(
    ("base", "target"),
    [
        (HOUR, HOUR),
        (H4, HOUR),
        (HOUR, dt.timedelta(minutes=90)),
    ],
)
def test_the_higher_timeframe_must_be_coarser_and_a_whole_number_of_bars(
    base: dt.timedelta, target: dt.timedelta
) -> None:
    with pytest.raises(ValueError, match="higher timeframe"):
        BarAggregator(base=base, target=target)


# --------------------------------------------------------------------------- #
# The gate, asked directly                                                      #
# --------------------------------------------------------------------------- #


def test_a_region_above_exists_only_once_the_bar_that_reveals_it_has_closed() -> None:
    """The H4 that breaks structure spans hours 68-71. On hour 70 there is no region to know of;
    on hour 71, the bar that closes it, both regions the golden leaves are there."""
    assert _fed(_through(70)).zones == ()
    zones = _fed(_through(71)).zones
    assert [(str(zone.block.bottom), str(zone.block.top)) for zone in zones] == [
        ("80", "100"),
        ("110", "117"),
    ]


def test_the_touch_releases_the_side_on_the_bar_that_reaches_the_region() -> None:
    probe = _demand(_at(ARMS))
    assert _fed(_through(TOUCH - 1)).reference(probe) is None
    reference = _fed(_through(TOUCH)).reference(probe)
    assert reference is not None
    assert (str(reference.bottom), str(reference.top)) == ("80", "100")


def test_a_bar_that_falls_through_one_region_into_another_references_the_innermost() -> None:
    """Hour 72 reaches both regions — low 92 is under 117 and under 100 — and closes at 93, through
    the secondary [110, 117]. Taking the newer region as the reference would release and end the
    search on the same bar; the region price is *in* is the primary."""
    gate = _fed(_through(TOUCH))
    reference = gate.reference(_demand(_at(ARMS)))
    assert reference is not None
    assert str(reference.top) == "100"
    # Both were reached, so both are spent: nothing touching [110, 117] later releases anything.
    gate.spend(Side.LONG)
    with localcontext(ENGINE_CONTEXT):
        gate.observe(bar(73, open_="112", close="114", high="115", low="111"))
    assert gate.reference(_demand(_at(ARMS))) is None


def test_the_touch_is_the_wick_reaching_the_edge_and_the_edge_counts() -> None:
    """His mitigation rule, read on the base bar: the first wick to the region's entry edge. A
    bar whose low is exactly 100 and whose close is back at 105 has touched [80, 100] — the same
    `<=` `TrackedZone._advance` uses — so the side is released although nothing closed inside."""
    gate = _fed(_through(TOUCH - 1))
    with localcontext(ENGINE_CONTEXT):
        gate.observe(bar(TOUCH, open_="104", close="105", high="106", low="100"))
    reference = gate.reference(_demand(_at(ARMS)))
    assert reference is not None
    assert (str(reference.bottom), str(reference.top)) == ("80", "100")

    short_of_it = _fed(_through(TOUCH - 1))
    with localcontext(ENGINE_CONTEXT):
        short_of_it.observe(bar(TOUCH, open_="104", close="105", high="106", low="100.01"))
    assert short_of_it.reference(_demand(_at(ARMS))) is None


def test_the_supply_touch_is_the_wick_reaching_the_bottom_edge() -> None:
    """The mirror, with its own numbers: the supply region at [100, 120] is reached by a high of
    exactly 100 that closes back at 95, and not by one that stops at 99.99."""
    prefix = [candle for candle in _mirror(STREAM) if _index_of(candle) < TOUCH]
    probe = OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal(112),
        bottom=Decimal(108),
        time=_at(ARMS - 1),
        confirmed_at=_at(ARMS),
        break_kind=StructureKind.BOS,
        primary=True,
    )
    gate = _fed(prefix)
    short_of_it = _fed(prefix)
    with localcontext(ENGINE_CONTEXT):
        gate.observe(bar(TOUCH, open_="96", close="95", high="100", low="94"))
        short_of_it.observe(bar(TOUCH, open_="96", close="95", high="99.99", low="94"))
    reference = gate.reference(probe)
    assert reference is not None
    assert (str(reference.bottom), str(reference.top)) == ("100", "120")
    assert short_of_it.reference(probe) is None


def test_the_release_opens_on_the_touching_bar_not_on_its_h4_bucket() -> None:
    """Hour 72 opens an H4 bucket, so on the golden the touch and the bucket coincide. Here the
    touch is hour 73: hour 72 reaches only the secondary and closes through it — spent, nothing
    released — and hour 73 reaches the primary. A break confirmed on hour 72 came before the
    touch and is refused; on 73 it is allowed."""
    gate = _fed(_through(TOUCH - 1))
    with localcontext(ENGINE_CONTEXT):
        gate.observe(bar(72, open_="112", close="105", high="113", low="101"))
        assert gate.reference(_demand(_at(ARMS))) is None
        gate.observe(bar(73, open_="105", close="103", high="106", low="95"))
    assert not gate.allows(_demand(_at(72)))
    assert gate.allows(_demand(_at(73)))


def test_a_deeper_region_becomes_the_reference_but_the_release_keeps_its_first_bar() -> None:
    """Hour 72 reaches only the secondary [110, 117] and holds above it; hour 73 comes down to the
    primary. The two heights and the break are now measured from [80, 100], and *from that bar
    on* still counts from hour 72: a break confirmed then was a reaction to a region price had
    come to."""
    gate = _fed(_through(TOUCH - 1))
    with localcontext(ENGINE_CONTEXT):
        gate.observe(bar(72, open_="112", close="112", high="113", low="111"))
        first = gate.reference(_demand(_at(ARMS)))
        assert first is not None
        assert str(first.top) == "117"
        gate.observe(bar(73, open_="112", close="103", high="112", low="95"))
    reference = gate.reference(_demand(_at(ARMS)))
    assert reference is not None
    assert str(reference.top) == "100"
    assert gate.allows(_demand(_at(72)))
    assert not gate.allows(_demand(_at(71)))


def test_a_break_that_confirmed_before_the_touch_is_not_released() -> None:
    """His answer 5: *from that bar on*. A zone whose break confirmed on hour 71 was not the
    reaction to the region; one confirmed on the touching bar itself, or after, was."""
    gate = _fed(_through(TOUCH))
    assert not gate.allows(_demand(_at(TOUCH - 1)))
    assert gate.allows(_demand(_at(TOUCH)))
    assert gate.allows(_demand(_at(TOUCH + 1)))


def test_spending_shuts_the_side_and_the_same_region_never_releases_again() -> None:
    gate = _fed(_through(TOUCH))
    gate.spend(Side.LONG)
    assert not gate.allows(_demand(_at(ARMS)))
    # Hour 73 dips to 91, inside [80, 100] again — the second visit releases nothing.
    with localcontext(ENGINE_CONTEXT):
        gate.observe(STREAM[TOUCH + 1])
    assert not gate.allows(_demand(_at(ARMS)))
    assert gate.reference(_demand(_at(ARMS))) is None


def test_two_heights_past_the_region_end_the_search_on_the_wick() -> None:
    """[80, 100] is twenty high, so the search ends at 140. A high of 140 that closes back at 120
    ends it — *"atingir"* is trading there — and a high one tick short does not."""
    assert GIVE_UP_AT_REGION_HEIGHTS == 2
    ran_away = _fed(_through(TOUCH))
    fell_short = _fed(_through(TOUCH))
    with localcontext(ENGINE_CONTEXT):
        ran_away.observe(bar(73, open_="95", close="120", high="140", low="95"))
        fell_short.observe(bar(73, open_="95", close="120", high="139.99", low="95"))
    assert not ran_away.allows(_demand(_at(ARMS)))
    assert fell_short.allows(_demand(_at(ARMS)))


def test_a_close_through_the_region_ends_the_search_and_a_close_on_its_edge_does_not() -> None:
    """His example: released, no entry, a close at 85 under [90, 100] — stop looking. The wick is
    not the event, the close is; and a close exactly on the far edge is neither."""
    broke = _fed(_through(TOUCH))
    rested = _fed(_through(TOUCH))
    with localcontext(ENGINE_CONTEXT):
        broke.observe(bar(73, open_="93", close="79.99", high="93", low="79"))
        rested.observe(bar(73, open_="93", close="80", high="93", low="79"))
    assert not broke.allows(_demand(_at(ARMS)))
    assert rested.allows(_demand(_at(ARMS)))


def test_the_supply_side_mirrors_the_release_the_two_heights_and_the_break() -> None:
    """The stream reflected about 200: the primary becomes a supply region at [100, 120], the
    secondary [83, 90], and hour 72 reaches both from below, closing at 107 — through the
    secondary. The reference is the innermost; the search ends at 60 on the wick or on a close
    above 120. Numbers that differ from the demand side's, so a sign error cannot hide."""
    mirrored = _mirror(STREAM)
    through_touch = [candle for candle in mirrored if _index_of(candle) <= TOUCH]
    probe = OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal(112),
        bottom=Decimal(108),
        time=_at(ARMS - 1),
        confirmed_at=_at(ARMS),
        break_kind=StructureKind.BOS,
        primary=True,
    )
    gate = _fed(through_touch)
    reference = gate.reference(probe)
    assert reference is not None
    assert (str(reference.bottom), str(reference.top)) == ("100", "120")

    ran_away = _fed(through_touch)
    fell_short = _fed(through_touch)
    broke = _fed(through_touch)
    rested = _fed(through_touch)
    with localcontext(ENGINE_CONTEXT):
        ran_away.observe(bar(73, open_="105", close="80", high="105", low="60"))
        fell_short.observe(bar(73, open_="105", close="80", high="105", low="60.01"))
        broke.observe(bar(73, open_="107", close="120.01", high="121", low="107"))
        rested.observe(bar(73, open_="107", close="120", high="121", low="107"))
    assert not ran_away.allows(probe)
    assert fell_short.allows(probe)
    assert not broke.allows(probe)
    assert rested.allows(probe)


# --------------------------------------------------------------------------- #
# The strategy under the filter                                                 #
# --------------------------------------------------------------------------- #


def test_without_the_filter_the_stream_arms_five_times() -> None:
    """The baseline every test below is read against: the hourly structure marks zones on five
    bars of this stream, four of them before price ever reaches the region above."""
    entries = _entries(_drive(StructureStrategy(qualifier=_Marked()), STREAM))
    assert sorted(entries) == [26, 30, 44, 70, ARMS]


def test_nothing_is_armed_before_price_reaches_a_region_above() -> None:
    strategy = StructureStrategy(qualifier=_Marked(), htf=H4, timeframe=HOUR)
    signals = _drive(strategy, _through(TOUCH - 1))
    assert all(bar_signals == [] for bar_signals in signals.values())


def test_the_touch_releases_one_entry_and_the_entry_carries_the_region_above() -> None:
    strategy = StructureStrategy(qualifier=_Marked(), htf=H4, timeframe=HOUR)
    entries = _entries(_drive(strategy, STREAM))

    assert list(entries) == [ARMS]
    entry = entries[ARMS]
    assert entry.side is Side.LONG
    assert str(entry.limit_price) == "92"
    assert entry.context is not None
    assert {key: str(value) for key, value in entry.context.items()} == {
        "zone_top": "92",
        "zone_bottom": "88",
        "htf_top": "100",
        "htf_bottom": "80",
    }
    assert [(region.label, str(region.bottom), str(region.top)) for region in entry.regions] == [
        ("zone", "88", "92"),
        ("htf", "80", "100"),
    ]


def test_the_arming_spends_the_release_and_a_later_zone_is_refused() -> None:
    """Hour 89 marks three zones; the stand-in takes the first, then names the second on the next
    bar. Without the filter that is an ordinary replacement — cancel and a new order. With it,
    the release was spent on the first arming and the second zone is refused outright."""
    tail = [*STREAM, bar(90, open_="126", close="126")]
    # Secondaries are offered too, so the stand-in can name hour 89's second zone. Ten zones are
    # marked before hour 89 (three on 26, one each on 30 and 44, five on 70): the first of hour
    # 89's three is the eleventh seen and the second is the twelfth.
    picks = {ARMS: 10, ARMS + 1: 11}

    plain = _drive(StructureStrategy(qualifier=_Names(picks), allow_secondary=True), tail)
    assert [signal.kind for signal in plain[ARMS + 1]] == [SignalKind.CANCEL, SignalKind.ENTRY]

    filtered = _drive(
        StructureStrategy(qualifier=_Names(picks), allow_secondary=True, htf=H4, timeframe=HOUR),
        tail,
    )
    assert filtered[ARMS + 1] == []
    assert list(_entries(filtered)) == [ARMS]


def test_the_short_side_spends_its_own_release() -> None:
    """The mirror of the test above, and the one a `spend(Side.LONG)` written by hand would pass
    without: the supply release has to be the one shut by a sold zone's arming."""
    tail = _mirror([*STREAM, bar(90, open_="126", close="126")])
    # The mirror is not the demand run's twin bar for bar — the tick nudges in `_expand` step the
    # same way on both — so the hourly structure marks seven zones before hour 89 here (one each
    # on 30 and 44, five on 70), and hour 89's first and second are the eighth and ninth seen.
    picks = {ARMS: 7, ARMS + 1: 8}

    plain = _drive(StructureStrategy(qualifier=_Names(picks), allow_secondary=True), tail)
    assert [signal.kind for signal in plain[ARMS + 1]] == [SignalKind.CANCEL, SignalKind.ENTRY]

    filtered = _drive(
        StructureStrategy(qualifier=_Names(picks), allow_secondary=True, htf=H4, timeframe=HOUR),
        tail,
    )
    assert filtered[ARMS + 1] == []
    assert [entry.side for entry in _entries(filtered).values()] == [Side.SHORT]


def test_an_order_the_venue_turned_away_hands_the_release_back() -> None:
    """The order armed on hour 89 never reached the book; the refusal lands on hour 90, and the
    stand-in names the same zone again. Without the filter that is a plain re-arm. With it, the
    release has to come back — his rule spends a region on an order *placed* and withdrawn, and
    this one was never placed — so the zone is armed a second time under a new name."""
    tail = [*STREAM, bar(90, open_="126", close="126")]
    picks = {ARMS: 4, ARMS + 1: 4}
    refused = {ARMS + 1: RefusedBy.BROKER}

    plain = _drive(StructureStrategy(qualifier=_Names(picks)), tail, refused_on=refused)
    assert [signal.kind for signal in plain[ARMS + 1]] == [SignalKind.ENTRY]

    filtered = _drive(
        StructureStrategy(qualifier=_Names(picks), htf=H4, timeframe=HOUR), tail, refused_on=refused
    )
    entries = _entries(filtered)
    assert list(entries) == [ARMS, ARMS + 1]
    assert entries[ARMS].client_id != entries[ARMS + 1].client_id
    context = entries[ARMS + 1].context
    assert context is not None
    assert str(context["htf_top"]) == "100"


def test_an_order_the_market_withdrew_keeps_the_release_spent() -> None:
    """`MARKET` is an order that rested and was taken back by price moving through it — placed
    and withdrawn unfilled, exactly the case his answer 3 spends the region on."""
    tail = [*STREAM, bar(90, open_="126", close="126")]
    picks = {ARMS: 4, ARMS + 1: 4}
    refused = {ARMS + 1: RefusedBy.MARKET}

    plain = _drive(StructureStrategy(qualifier=_Names(picks)), tail, refused_on=refused)
    assert [signal.kind for signal in plain[ARMS + 1]] == [SignalKind.ENTRY]

    filtered = _drive(
        StructureStrategy(qualifier=_Names(picks), htf=H4, timeframe=HOUR), tail, refused_on=refused
    )
    assert filtered[ARMS + 1] == []


def test_a_refusal_that_lands_after_the_search_ended_hands_nothing_back() -> None:
    """The order armed on hour 89 is turned away at the gate, but the bar the refusal lands on
    runs to 140 — two heights past [80, 100]. His rule ended the search on that bar; handing the
    release back would reopen it, and the same zone named again must stay refused."""
    tail = [*STREAM, bar(90, open_="126", close="128", high="140", low="126")]
    picks = {ARMS: 4, ARMS + 1: 4}
    refused = {ARMS + 1: RefusedBy.BROKER}

    plain = _drive(StructureStrategy(qualifier=_Names(picks)), tail, refused_on=refused)
    assert [signal.kind for signal in plain[ARMS + 1]] == [SignalKind.ENTRY]

    filtered = _drive(
        StructureStrategy(qualifier=_Names(picks), htf=H4, timeframe=HOUR), tail, refused_on=refused
    )
    assert filtered[ARMS + 1] == []


def test_a_region_above_reached_while_a_position_is_open_still_releases() -> None:
    """Every bar that touches [80, 100] — 72 through 88 — is spent inside a trade, and the trade
    ends just before the break that marks the zone. The gate has to have been reading those bars
    from behind the position branch, or nothing is released when it matters."""
    strategy = StructureStrategy(qualifier=_Marked(), htf=H4, timeframe=HOUR)
    entries = _entries(_drive(strategy, STREAM, position_on=frozenset(range(TOUCH, ARMS))))
    assert list(entries) == [ARMS]


def test_a_zone_from_a_break_before_the_touch_is_not_armed_under_the_release() -> None:
    """Hour 70 marks [80.01, 90] on a break before price reached the region above. Named on hour
    73 — released, the zone standing — it is still refused: the break was not the reaction."""
    # Only primaries reach the qualifier: one each on hours 26, 30 and 44, then hour 70's.
    picks = {73: 3}
    plain = _drive(StructureStrategy(qualifier=_Names(picks)), _through(73))
    assert [signal.kind for signal in plain[73]] == [SignalKind.ENTRY]

    filtered = _drive(
        StructureStrategy(qualifier=_Names(picks), htf=H4, timeframe=HOUR), _through(73)
    )
    assert filtered[73] == []


def test_the_short_side_releases_end_to_end() -> None:
    strategy = StructureStrategy(qualifier=_Marked(), htf=H4, timeframe=HOUR)
    entries = _entries(_drive(strategy, _mirror(STREAM)))

    assert list(entries) == [ARMS]
    entry = entries[ARMS]
    assert entry.side is Side.SHORT
    assert str(entry.limit_price) == "108"
    assert entry.context is not None
    assert (str(entry.context["htf_bottom"]), str(entry.context["htf_top"])) == ("100", "120")


def test_the_filter_needs_the_setup_s_own_timeframe() -> None:
    with pytest.raises(ValueError, match="own timeframe"):
        StructureStrategy(qualifier=_Marked(), htf=H4)


def test_a_timeframe_alone_builds_no_gate() -> None:
    strategy = StructureStrategy(qualifier=_Marked(), timeframe=HOUR)
    assert strategy._gate is None
