"""The regions' bookkeeping, made cheap on 24/09, still answers exactly what the walks answered.

Three places used to walk every tracked region on every bar: the detector advancing regions
already taken, the higher-timeframe gate asking its `_touched` set about every region, and every
reader looking a block up by comparing it against the whole list. Each was replaced by an
equivalent that does less (`OrderBlockDetector._live`, `HigherTimeframeGate._untouched`,
`ZoneView`). None of them may change a result — `AGENTS.md §5.2` — so each is checked here
against the walk it replaced, over random markets.
"""

import datetime as dt
from dataclasses import replace
from decimal import Decimal, localcontext

from hypothesis import given, settings
from hypothesis import strategies as st

from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.higher_timeframe import (
    HigherTimeframeGate,
    Release,
    _innermost,
    _reaches,
    _search_over,
)
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.structure import (
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    StructureKind,
    TrackedZone,
    ZoneKind,
    ZoneView,
    holding,
)
from tradeforge_engine.testing import HOUR, bar


@st.composite
def _random_walk(draw: st.DrawFn) -> list[Candle]:
    """Valid hourly candles that wander and trend, long enough to break structure many times."""
    count = draw(st.integers(min_value=120, max_value=400))
    drift = draw(st.decimals(min_value="-0.8", max_value="0.8", places=1))
    step = st.decimals(min_value="-3", max_value="3", places=1)
    wick = st.decimals(min_value="0", max_value="1.5", places=1)
    candles: list[Candle] = []
    price = Decimal(200)
    for index in range(count):
        open_ = price
        close = max(Decimal(20), open_ + drift + draw(step))
        high = max(open_, close) + draw(wick)
        low = min(open_, close) - draw(wick)
        candles.append(bar(index, open_=str(open_), close=str(close), high=str(high), low=str(low)))
        price = close
    return candles


def _reached(block: OrderBlock, candle: Candle) -> bool:
    if block.kind is ZoneKind.DEMAND:
        return candle.low <= block.top
    return candle.high >= block.bottom


class _SmallDetector(OrderBlockDetector):
    """A cap of three, so a random market trims regions while they are still standing."""

    _MAX_ZONES = 3  # type: ignore[misc]  # a test's cap, overriding the Final on purpose


class TestTheDetectorAdvancesOnlyWhatCanChange:
    @settings(max_examples=60, deadline=None)
    @given(candles=_random_walk(), small=st.booleans())
    def test_each_region_is_taken_by_the_first_touch_while_it_is_followed(
        self, candles: list[Candle], small: bool
    ) -> None:
        """The walk's answer, worked out from the candles: a region offered on bar `i` and still
        followed through bar `k` is taken by the first bar in `(i, k]` that reaches it — and a
        region trimmed before any touch is never taken, as when the walk stopped following it."""
        detector = _SmallDetector() if small else OrderBlockDetector()
        structure = MarketStructure()
        offered: dict[int, tuple[TrackedZone, int]] = {}
        dropped: dict[int, int] = {}
        with localcontext(ENGINE_CONTEXT):
            for index, candle in enumerate(candles):
                before = {id(tracked) for tracked in detector.zones}
                detector.update(candle, structure.update(candle))
                now = {id(tracked): tracked for tracked in detector.zones}
                for key, tracked in now.items():
                    offered.setdefault(key, (tracked, index))
                for key in before - now.keys():
                    dropped[key] = index

        for key, (tracked, born) in offered.items():
            last = dropped.get(key, len(candles) - 1)
            touch = next(
                (
                    candle.time
                    for candle in candles[born + 1 : last + 1]
                    if _reached(tracked.block, candle)
                ),
                None,
            )
            assert tracked.mitigated_at == touch
            assert tracked.mitigated is (touch is not None)

    @settings(max_examples=60, deadline=None)
    @given(candles=_random_walk(), small=st.booleans())
    def test_follows_exactly_the_regions_still_standing(
        self, candles: list[Candle], small: bool
    ) -> None:
        """`_live` is `_zones` minus the taken, the same objects in the same order, after every bar
        — a region trimmed while still standing leaves both lists at once. Checked on the lists
        rather than on a later touch, because the walk this replaced stopped following a trimmed
        region too, and a random market almost never comes back to one."""
        detector = _SmallDetector() if small else OrderBlockDetector()
        structure = MarketStructure()
        with localcontext(ENGINE_CONTEXT):
            for candle in candles:
                detector.update(candle, structure.update(candle))
                standing = [id(tracked) for tracked in detector._zones if not tracked.mitigated]
                assert [id(tracked) for tracked in detector._live] == standing

    @settings(max_examples=30, deadline=None)
    @given(candles=_random_walk())
    def test_hands_out_the_same_view_until_the_list_changes(self, candles: list[Candle]) -> None:
        detector = OrderBlockDetector()
        structure = MarketStructure()
        with localcontext(ENGINE_CONTEXT):
            for candle in candles:
                before = detector.zones
                marked = detector.update(candle, structure.update(candle))
                if not marked:
                    assert detector.zones is before
                assert list(detector.zones) == list(detector._zones)


class TestTheIndexAnswersWhatTheWalkAnswered:
    @settings(max_examples=30, deadline=None)
    @given(candles=_random_walk())
    def test_every_block_finds_the_regions_holding_it(self, candles: list[Candle]) -> None:
        detector = OrderBlockDetector()
        structure = MarketStructure()
        with localcontext(ENGINE_CONTEXT):
            for candle in candles:
                detector.update(candle, structure.update(candle))
                view = detector.zones
                walked = tuple(view)
                for tracked in view:
                    assert holding(view, tracked.block) == holding(walked, tracked.block)
                    assert holding(view, tracked.block)[0] is tracked

    def test_equal_blocks_are_all_found_oldest_first(self) -> None:
        """Two equal blocks are one region to every reader: both are found, the older first."""
        block = OrderBlock(
            kind=ZoneKind.DEMAND,
            top=Decimal(100),
            bottom=Decimal(90),
            time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
            confirmed_at=dt.datetime(2024, 1, 2, tzinfo=dt.UTC),
            break_kind=StructureKind.CHOCH,
            primary=True,
        )
        # Equal, not identical: a dict finds it by value, as `==` did.
        twin = replace(block)
        first, second = TrackedZone(block=block), TrackedZone(block=twin)
        other = TrackedZone(block=replace(block, top=Decimal(101)))
        view = ZoneView([first, other, second])

        assert twin is not block
        # By identity: `TrackedZone` compares by value, so `==` would not see the order.
        assert [id(zone) for zone in view.holding(twin)] == [id(first), id(second)]
        assert [id(zone) for zone in holding(tuple(view), block)] == [id(first), id(second)]
        assert view.holding(replace(block, primary=False)) == ()
        assert ZoneView().holding(block) == ()


class _WalkingGate(HigherTimeframeGate):
    """The gate's `observe` as it was before 24/09, kept as the oracle: every region the detector
    holds is asked about on every base bar."""

    def observe(self, candle: Candle) -> None:
        for higher in self._bars.update(candle):
            self._blocks.update(higher, self._structure.update(higher))
            held = {tracked.block for tracked in self._blocks.zones}
            self._touched.intersection_update(held)

        reached: dict[Side, list[OrderBlock]] = {}
        for tracked in self._blocks.zones:
            block = tracked.block
            if block in self._touched or not _reaches(block, candle):
                continue
            self._touched.add(block)
            reached.setdefault(block.side, []).append(block)
        for side, blocks in reached.items():
            standing = self._releases.get(side)
            opened_at = candle.time if standing is None else standing.opened_at
            self._releases[side] = Release(block=_innermost(blocks), opened_at=opened_at)

        for side in (Side.LONG, Side.SHORT):
            release = self._releases.get(side)
            if release is not None and _search_over(release.block, candle):
                del self._releases[side]


class TestTheGateReleasesWhatTheWalkReleased:
    @settings(max_examples=60, deadline=None)
    @given(
        candles=_random_walk(),
        target=st.sampled_from([dt.timedelta(hours=2), dt.timedelta(hours=4)]),
        spends=st.lists(st.booleans(), min_size=400, max_size=400),
    )
    def test_bar_for_bar(
        self, candles: list[Candle], target: dt.timedelta, spends: list[bool]
    ) -> None:
        """Both gates fed the same bars and spent on the same bars — the setup's arming, which
        shuts a side — hold the same release on every side after every bar."""
        gate = HigherTimeframeGate(base=HOUR, target=target, offset=dt.timedelta(0))
        walking = _WalkingGate(base=HOUR, target=target, offset=dt.timedelta(0))
        with localcontext(ENGINE_CONTEXT):
            for index, candle in enumerate(candles):
                gate.observe(candle)
                walking.observe(candle)
                assert gate._releases == walking._releases
                if spends[index]:
                    side = Side.LONG if index % 2 else Side.SHORT
                    gate.spend(side)
                    walking.spend(side)
                assert gate._touched == walking._touched
