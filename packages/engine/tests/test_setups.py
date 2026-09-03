"""The shared entry machinery: where the order goes, and how long it lives.

These drive the strategy bar by bar with a stand-in qualifier, because the machinery is what is
under test and not the choice of zone — choch and continuation are separate work, and wiring a
real setup in here would make every test below depend on a market-structure scenario as well.

The zones themselves are real. They come out of `MarketStructure` and `OrderBlockDetector` from
the same impulse the order-block goldens use, tuned so the primary lands on the author's own
example: a demand zone of [90, 100], bought at 100 with the stop at 89.
"""

import datetime as dt
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext

import pytest

from tradeforge_engine import setups
from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.costs import NoCostModel
from tradeforge_engine.domain import (
    AccountState,
    Candle,
    Context,
    Fill,
    OrderRequest,
    OrderResult,
    Position,
    Refusal,
    RefusedBy,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.loop import ENGINE_CONTEXT, iter_run, run
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.setup_factory import build_setup
from tradeforge_engine.setups import (
    MAX_ARMING_ATTEMPTS,
    ChochQualifier,
    ContinuationQualifier,
    SetupContext,
    StructureStrategy,
    ZoneEntryPoint,
    _to_tick,
)
from tradeforge_engine.structure import (
    OrderBlock,
    StructureBreak,
    StructureKind,
    TrackedZone,
    Trend,
    ZoneKind,
)
from tradeforge_engine.testing import (
    AAPL,
    BULLISH_START,
    EURUSD,
    HOUR,
    START,
    FixedRisk,
    ImmediateFillBroker,
    arms_a_resting_limit,
    bar,
)

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()


def _at(index: int) -> dt.datetime:
    return START + index * HOUR


def _index_of(candle: Candle) -> int:
    """The bar number `bar()` stamped into this candle — negative for anything before bar 0.

    The stub qualifiers below count bars with this rather than with a call counter of their own,
    so that `at=9` means bar 9 and keeps meaning it when a scenario is run behind
    `_BULLISH_START`. A call counter would silently be off by the length of the prefix.
    """
    return round((candle.time - START) / HOUR)


# The order-block golden's impulse, with bar 3 dug down to 90 so the zone it marks is the
# author's own [90, 100]. Bar 3 is already the leg's origin, so deepening it moves no other part
# of the reading: the BOS still confirms on bar 9's close of 124 through the 123 top.
_IMPULSE = [
    bar(0, open_="122", close="122", high="123", low="120"),  # top 123
    bar(1, open_="119", close="119", high="122", low="118"),  # correction 1
    bar(2, open_="117", close="117", high="121", low="116"),  # correction 2 -> armed
    bar(3, open_="99", close="99", high="100", low="90"),  # the marking candle: zone [90, 100]
    bar(4, open_="104", close="104", high="105", low="103"),
    bar(5, open_="108", close="108", high="110", low="102"),  # gap A: 100 < 102
    bar(6, open_="113", close="113", high="115", low="107"),  # gap B
    bar(7, open_="112", close="112", high="117", low="110"),  # pause
    bar(8, open_="116", close="118", high="119", low="112"),  # pause; closes clear of 117
    bar(9, open_="124", close="124", high="125", low="120"),  # gap C, and close 124 > 123 -> BOS
]

_MIRROR_AXIS = Decimal(200)


def _mirror(candles: list[Candle]) -> list[Candle]:
    """Reflect a sequence about a price, turning a demand scenario into its supply twin.

    Reflection swaps the extremes — the mirror of a high is a low — which is exactly the symmetry
    the machinery is supposed to have. Hand-writing ten opposite candles instead would test that
    the author of the test can subtract, not that the code mirrors.
    """
    return [
        Candle(
            time=candle.time,
            open=_MIRROR_AXIS - candle.open,
            high=_MIRROR_AXIS - candle.low,
            low=_MIRROR_AXIS - candle.high,
            close=_MIRROR_AXIS - candle.close,
            tick_volume=candle.tick_volume,
            real_volume=candle.real_volume,
        )
        for candle in candles
    ]


@dataclass
class _Marked:
    """A stand-in setup: qualify a zone the moment the detector marks it."""

    index: int = 0
    seen: list[SetupContext] = field(default_factory=list)

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.seen.append(context)
        return context.marked[self.index] if context.marked else None


@dataclass
class _Once:
    """Qualifies the first zone the detector ever marks, and nothing after it.

    Needed wherever a test drives price far enough to kill a zone: a move decisive enough to
    close through a demand region is usually a change of character, which marks a supply zone of
    its own — and a qualifier that took it would leave the test asserting two setups at once.
    """

    done: bool = False

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if self.done or not context.marked:
            return None
        self.done = True
        return context.marked[0]


@dataclass
class _OnBar:
    """Qualifies a zone the detector marked earlier, but only once a chosen bar arrives.

    Lets a test put the qualifying event on a bar where price is *inside* the region, which the
    detector's own timing never does — a break confirms with price well clear of the zone it
    reveals.
    """

    at: int
    block: OrderBlock | None = None

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if context.marked:
            self.block = context.marked[0]
        return self.block if _index_of(context.candle) == self.at else None


@dataclass
class _Fixed:
    """Names one zone handed in by the test, on the bar the detector first marks anything."""

    block: OrderBlock
    done: bool = False

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if self.done or not context.marked:
            return None
        self.done = True
        return self.block


@dataclass
class _Remembers:
    """Remembers the first zone marked and names it on a chosen bar, however long after.

    The shape of the continuation setup, which has to remember a change of character before the
    break that confirms it can qualify anything — so the gap between "the zone was marked" and
    "the setup names it" is real, and price moves inside it.
    """

    at: int
    block: OrderBlock | None = None

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if self.block is None and context.marked:
            self.block = context.marked[0]
        return self.block if _index_of(context.candle) == self.at else None


@dataclass
class _FromTracker:
    """Reaches past `marked` into `SetupContext.zones` — the way the flip setup has to.

    Flip does not qualify on a break: it qualifies when a *zone* is taken out, so it reads the
    tracker rather than the list of zones a break just revealed.
    """

    index: int
    done: bool = False

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if self.done or len(context.zones) <= self.index:
            return None
        self.done = True
        return context.zones[self.index].block


@dataclass
class _StickyFrom:
    """Names the remembered zone on every bar from `at` onward — a stateful qualifier that keeps
    saying the same thing while price is still inside the region it named."""

    at: int
    block: OrderBlock | None = None

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if self.block is None and context.marked:
            self.block = context.marked[0]
        return self.block if _index_of(context.candle) >= self.at else None


@dataclass
class _Sticky:
    """Names the same zone on every bar from the one that marked it — a qualifier with no memory
    of having already spoken. The machinery must not re-arm on the repeat."""

    block: OrderBlock | None = None

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        if context.marked:
            self.block = context.marked[0]
        return self.block


@dataclass
class _Script:
    """Names, on chosen bars, any zone the detector has marked so far — by order of marking.

    The shape a re-offered region arrives in: a later event names a newer zone, and a later bar
    still names the first one again. `picks` maps a bar index to an index into every zone seen."""

    picks: dict[int, int]
    seen: list[OrderBlock] = field(default_factory=list)

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.seen.extend(context.marked)
        pick = self.picks.get(_index_of(context.candle))
        return self.seen[pick] if pick is not None else None


def _drive(
    strategy: StructureStrategy,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
    held: Position | None = None,
    held_by_bar: dict[int, Position] | None = None,
) -> list[list[Signal]]:
    """Feed candles one at a time and collect the signals each bar produced.

    `position_on` names the bars where a fake position is open, standing in for the broker having
    filled the order — the strategy reads `context.position`, not the fill. Given as a set of bars
    rather than "from here on" because the interesting case is a trade that *ends*: the machinery
    has to still be right on the bars after the stop closed it.

    `held` is for conduction: those scenarios need a fixed entry price and known stops, because
    what the strategy computes is measured *from* them. Without it the position is a placeholder
    whose only job is to exist. `held_by_bar` is the same thing per bar, for the scenarios that
    need **two different trades** — which is the only way to see state leak from one to the next.
    """
    by_bar = held_by_bar or {}
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for candle in candles:
            # By bar number, not by position in the list: a scenario run behind `_BULLISH_START`
            # must still find its position on the bar the test named.
            index = _index_of(candle)
            position = None
            if index in by_bar:
                position = by_bar[index]
            elif index in position_on:
                position = held or Position(
                    symbol=AAPL.symbol,
                    side=Side.LONG,
                    volume=Decimal(1),
                    entry_price=candle.open,
                    entry_time=candle.time,
                )
            context = Context(candle=candle, instrument=AAPL, account=_ACCOUNT, position=position)
            out.append(list(strategy.on_bar(context)))
    return out


def _drive_from_bullish(
    strategy: StructureStrategy,
    candles: list[Candle],
    **kwargs: object,
) -> list[list[Signal]]:
    """`_drive`, on a machine already in an uptrend, returning only the scenario's own bars.

    A fresh `MarketStructure` starts at the indicator's `DIR = -1`, so a scenario that rises from
    bar 0 confirms nothing and the setup under test is never given anything to qualify. The prefix
    supplies the uptrend; its own bars are dropped from the result, so the lists these tests index
    into still line up with the bars they name. See `_BULLISH_START`.
    """
    everything = _drive(strategy, [*BULLISH_START, *candles], **kwargs)  # type: ignore[arg-type]
    return everything[len(BULLISH_START) :]


# --------------------------------------------------------------------------- #
# Where the order goes                                                          #
# --------------------------------------------------------------------------- #


def test_the_authors_geometry_a_demand_zone_is_bought_at_its_top() -> None:
    """The author's own numbers: demand [90, 100] is bought at 100 with the stop at 89.

    The near edge is where the order rests, because that is the side price has to come back to.
    The stop clears the far edge by a tenth of the zone's width — the region is where price is
    expected to turn, and a stop level *on* the edge is taken out by the turn itself.
    """
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    signals = _drive_from_bullish(strategy, _IMPULSE)

    assert [len(bar_signals) for bar_signals in signals] == [0] * 9 + [1]
    [signal] = signals[9]
    assert signal.kind is SignalKind.ENTRY
    assert signal.side is Side.LONG
    assert signal.limit_price == Decimal("100")
    assert signal.stop_loss == Decimal("89")
    assert signal.reason == "entry.test"
    assert signal.client_id is not None  # it has to be nameable to be withdrawable


def test_the_entry_records_the_region_it_is_waiting_at() -> None:
    """This setup enters *at the edge of a zone*, not at a price the decision bar names.

    So the two edges are the entry's justification, and neither survives anywhere else: the
    order carries the near edge as its limit and a stop derived from the far one, which is not
    the same as knowing where the region was. A chart missing them shows an order resting in
    mid-air.

    The author's own zone: demand [90, 100], bought at its top.
    """
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    [signal] = _drive_from_bullish(strategy, _IMPULSE)[9]

    assert signal.context == {"zone_top": Decimal("100"), "zone_bottom": Decimal("90")}
    # The limit rests on the near edge — the side price has to come back to. Asserted against
    # the recorded edge rather than the literal, so a zone that moved would move both.
    assert signal.limit_price == signal.context["zone_top"]


def test_the_region_is_a_rectangle_starting_on_the_candle_before_the_gap() -> None:
    """The author draws a zone the way he trades it: a rectangle over the marking candle's
    range, from that candle, extended right until price comes back into it.

    So the region needs a **left edge in time**, and it has to be `OrderBlock.time` — the candle
    before the gap — rather than `confirmed_at`, the later bar whose break revealed the zone.
    Starting the rectangle at the confirmation would draw the zone as younger than it is and
    hide the impulse that made it, which is the part that says the zone is worth anything.

    Measured on the author's impulse: candle 3 runs 90 to 100, candle 4 opens at a low of 103 —
    the gap — so candle 3 is the marking candle and the rectangle is [90, 100] from candle 3.
    """
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    [signal] = _drive_from_bullish(strategy, _IMPULSE)[9]

    (region,) = signal.regions
    assert region.label == "zone"
    assert (region.top, region.bottom) == (Decimal("100"), Decimal("90"))
    assert region.from_time == _IMPULSE[3].time

    # The two facts that make candle 3 the marking candle, asserted rather than asserted-about:
    # the rectangle is that candle's own range, and the candle after it gaps clear of its high.
    assert (region.top, region.bottom) == (_IMPULSE[3].high, _IMPULSE[3].low)
    assert _IMPULSE[4].low > _IMPULSE[3].high

    # The rectangle and the scalars describe one zone. They are written a line apart from the
    # same block, and this is what would catch them drifting.
    assert signal.context == {"zone_top": region.top, "zone_bottom": region.bottom}


def test_the_entry_records_the_structure_that_broke() -> None:
    """The event that made the zone worth entering, drawn as the line the author draws by hand.

    A zone on its own justifies nothing: it is a stretch of price like any other until a break
    of structure reveals it. Without the broken level in the record, a chart of this entry shows
    price crossing a price, with nothing saying which price mattered — and a reader trying to
    judge whether the entry was right is looking at the wrong half of the setup.

    The segment is bounded at **both** ends, unlike the zone rectangle: a zone is still in force
    after the entry so it runs rightward, but a level is over the moment it is crossed, and
    drawing it onward would show a structure still standing that is not.

    Measured on the author's impulse: the level is 123, the high of bar 0, and bar 9 closes 124
    through it. Nine bars is how long that structure held.
    """
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    [signal] = _drive_from_bullish(strategy, _IMPULSE)[9]

    (level,) = signal.levels
    assert level.label == "bos"
    assert level.price == Decimal("123")
    assert (level.from_time, level.to_time) == (_IMPULSE[0].time, _IMPULSE[9].time)

    # The two facts that make this the broken level, asserted rather than asserted-about: the
    # price is bar 0's high, and bar 9 is the bar whose *close* went through it.
    assert level.price == _IMPULSE[0].high
    assert _IMPULSE[9].close > level.price


def test_the_structural_level_ends_where_it_broke_not_at_the_entry() -> None:
    """The right edge is the break, and the entry can be much later.

    Conflating the two would draw every structure as having held right up to the trade, which is
    the opposite of what a reader is checking: how long ago the break was is exactly the thing
    that says whether this entry is still trading that break or a stale memory of one.
    """
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    [signal] = _drive_from_bullish(strategy, _IMPULSE)[9]

    (level,) = signal.levels
    # The signal is emitted on bar 9, which is also the break here — so the claim is made
    # against the region instead, whose own left edge is bar 3, well after the level was set.
    (region,) = signal.regions
    assert level.from_time < region.from_time <= level.to_time


def test_the_recorded_region_mirrors_for_a_supply_zone() -> None:
    """Reflected about 200: supply [100, 110], sold at its bottom. `top` stays the higher price
    — it names the edge, not the entry side — so a short's limit sits at `zone_bottom`."""
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    [signal] = _drive(strategy, _mirror(_IMPULSE))[9]

    assert signal.context == {"zone_top": Decimal("110"), "zone_bottom": Decimal("100")}
    assert signal.limit_price == signal.context["zone_bottom"]


def test_the_geometry_mirrors_for_a_supply_zone() -> None:
    """The same impulse reflected about 200: supply [100, 110], sold at 100 with the stop at 111.

    Sold at the *bottom* — a supply zone is approached from below, so its near edge is its low.
    Getting this backwards is the sign error `Signal` refuses, and it would be easy to write.
    """
    strategy = StructureStrategy(qualifier=_Marked(), name="test")
    signals = _drive(strategy, _mirror(_IMPULSE))

    [signal] = signals[9]
    assert signal.side is Side.SHORT
    assert signal.limit_price == Decimal("100")
    assert signal.stop_loss == Decimal("111")


def test_the_stop_is_rounded_onto_the_tick_grid_away_from_the_entry() -> None:
    """A tenth of a zone's width is not generally a multiple of the tick, and a stop at a price
    that does not exist would fill in the backtest and be rejected by the venue.

    Zone [90, 100.05] is 10.05 wide, so the buffer is 1.005 and the raw stop is 88.995 — half a
    cent off AAPL's grid. It rounds **down** to 88.99, away from the entry: rounding the other way
    would shave the buffer back toward the very edge it exists to clear.
    """
    candles = [*_IMPULSE]
    candles[3] = bar(3, open_="99", close="99", high="100.05", low="90")
    strategy = StructureStrategy(qualifier=_Marked(), name="test")

    [signal] = _drive_from_bullish(strategy, candles)[9]
    assert signal.limit_price == Decimal("100.05")
    assert signal.stop_loss == Decimal("88.99")  # not 89.00, which is nearer the zone

    [short_signal] = _drive(StructureStrategy(qualifier=_Marked(), name="test"), _mirror(candles))[
        9
    ]
    assert short_signal.limit_price == Decimal("99.95")
    assert short_signal.stop_loss == Decimal("111.01")  # not 111.00


def test_to_tick_rounds_in_the_direction_it_is_told() -> None:
    """The helper on its own, both directions, so the two callers above cannot both be wrong in
    the same way and still agree with each other."""
    with localcontext(ENGINE_CONTEXT):
        tick = Decimal("0.01")
        assert _to_tick(Decimal("88.995"), tick, ROUND_FLOOR) == Decimal("88.99")
        assert _to_tick(Decimal("111.005"), tick, ROUND_CEILING) == Decimal("111.01")
        # already on the grid: rounding must not move it in either direction
        assert _to_tick(Decimal("89"), tick, ROUND_FLOOR) == Decimal("89")
        assert _to_tick(Decimal("89"), tick, ROUND_CEILING) == Decimal("89")


# --------------------------------------------------------------------------- #
# Which zone, and how many orders                                               #
# --------------------------------------------------------------------------- #


def _seen_at(contexts: list[SetupContext], index: int) -> SetupContext:
    """The context the qualifier was handed on bar `index`.

    By bar number rather than by position in the list: the qualifier is called on every bar it is
    driven over, `_BULLISH_START` included, so counting calls would land eight bars early.
    """
    (context,) = [c for c in contexts if _index_of(c.candle) == index]
    return context


def test_only_the_primary_zone_reaches_the_qualifier_by_default() -> None:
    """The impulse marks two zones. By default a setup is offered only the primary — the first
    gap event of the move — and the secondary is not its business to refuse."""
    qualifier = _Marked()
    _drive_from_bullish(StructureStrategy(qualifier=qualifier), _IMPULSE)

    marked = _seen_at(qualifier.seen, 9).marked
    assert [(zone.time, zone.primary) for zone in marked] == [(_at(3), True)]


def test_allow_secondary_offers_both_zones() -> None:
    """Turned on, the same impulse offers both, primary first — the flag the author asked for."""
    qualifier = _Marked()
    _drive_from_bullish(StructureStrategy(qualifier=qualifier, allow_secondary=True), _IMPULSE)

    marked = _seen_at(qualifier.seen, 9).marked
    assert [(zone.time, zone.primary) for zone in marked] == [
        (_at(3), True),
        (_at(7), False),
    ]


def test_a_newly_qualified_zone_withdraws_the_order_resting_on_the_old_one() -> None:
    """One live order at a time: the new zone's order does not join the old one, it replaces it.

    The cancel has to come **first** in the same bar's signals. Emitted the other way round the
    broker would hold two orders for an instant, and on a bar that reaches both levels the fill
    would be decided by arrival order in a list.
    """
    qualifier = _Marked()
    strategy = StructureStrategy(qualifier=qualifier, name="test")
    # A second impulse after the first, marking a second zone the qualifier will name.
    second = [
        bar(10, open_="124", close="118", high="125", low="117"),  # correction 1
        bar(11, open_="118", close="116", high="119", low="115"),  # correction 2
        bar(12, open_="115", close="115", high="116", low="114"),
        bar(13, open_="121", close="121", high="122", low="120"),  # gap: 116 < 120
        bar(14, open_="128", close="128", high="129", low="126"),  # close past the 125 top -> BOS
    ]
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *second])

    first_id = signals[9][0].client_id
    kinds = [(s.kind, s.client_id) for s in signals[14]]
    assert kinds[0] == (SignalKind.CANCEL, first_id)  # the old order, withdrawn first
    assert kinds[1][0] is SignalKind.ENTRY
    assert kinds[1][1] != first_id  # a filled or withdrawn name is never reused


def test_naming_the_same_zone_again_does_not_churn_the_order() -> None:
    """The repeat guard, and now the only test of it.

    A sibling used to cover the other window — a zone qualified but whose order had not reached
    the book yet, because price was still inside the region. His mitigation rule removed that
    window (see `test_a_region_price_is_inside_is_already_gone`), so this is where the guard is
    held.

    A qualifier that keeps pointing at the zone it already qualified is not a new setup.

    Acting on the repeat would withdraw a resting order and put an identical one back a bar later,
    every bar — and the fill would land on whichever bar the qualifier last repeated itself
    instead of on the bar price reached the level.

    The bars after the zone is armed are the whole point of this test. Stopping on the bar that
    armed it would prove nothing: `_armed` is still empty when the first order goes out, so the
    guard against a repeat has nothing to compare against and is never reached.
    """
    quiet = [
        bar(10, open_="124", close="122", high="125", low="121"),
        bar(11, open_="122", close="120", high="123", low="119"),
        bar(12, open_="120", close="121", high="122", low="119"),
    ]
    signals = _drive_from_bullish(StructureStrategy(qualifier=_Sticky()), [*_IMPULSE, *quiet])

    assert len(signals[9]) == 1  # armed once
    assert signals[9][0].kind is SignalKind.ENTRY
    # and the three bars of the qualifier saying the same thing again produce nothing at all
    assert (signals[10], signals[11], signals[12]) == ([], [], [])


# --------------------------------------------------------------------------- #
# How long the order lives                                                      #
# --------------------------------------------------------------------------- #


def test_the_order_is_withdrawn_when_its_zone_is_spent() -> None:
    """The order's life is the zone's life — there is no second clock.

    Price closes below 90, straight through the demand zone: whatever was defending that level is
    gone, so an order still resting at 100 would buy into a region that no longer exists.
    """
    through = [
        bar(10, open_="124", close="110", high="125", low="108"),
        bar(11, open_="110", close="89", high="111", low="88"),  # closes under the zone
    ]
    signals = _drive_from_bullish(StructureStrategy(qualifier=_Once()), [*_IMPULSE, *through])

    entry_id = signals[9][0].client_id
    assert [(s.kind, s.client_id) for s in signals[11]] == [(SignalKind.CANCEL, entry_id)]


def test_a_live_zone_keeps_its_order_resting() -> None:
    """The other half, so the cancel above cannot be a strategy that withdraws everything.

    Price wanders for two bars without touching the zone or closing through it. The zone still
    stands, so the order stays exactly where it was put — silence, not a re-arm.
    """
    quiet = [
        bar(10, open_="124", close="120", high="125", low="119"),
        bar(11, open_="120", close="115", high="121", low="114"),
    ]
    signals = _drive_from_bullish(StructureStrategy(qualifier=_Marked()), [*_IMPULSE, *quiet])

    assert signals[10] == []
    assert signals[11] == []


def test_nothing_is_armed_while_a_position_is_open() -> None:
    """This phase holds one position at a time, and the trade is the broker's to *end*.

    A position open means our order filled. Arming another zone would submit an entry the broker
    refuses, and withdrawing the filled order would be a cancel for something that no longer
    rests — noise either way.

    The stop is the exception, and the only one: the strategy may move it. Bar 9 of this impulse
    confirms a bullish BOS, which is the open long's first break in favour, so it asks for
    breakeven — the entry price, 124. What no bar may produce is an entry or a cancel.
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()), _IMPULSE, position_on=frozenset({9})
    )

    kinds = {signal.kind for bar_signals in signals for signal in bar_signals}
    assert kinds <= {SignalKind.MODIFY_STOP}
    assert [(index, s.stop_loss) for index, b in enumerate(signals) for s in b] == [
        (9, Decimal("124"))
    ]


def test_a_region_price_is_inside_is_already_gone() -> None:
    """The invariant that replaced the wait, asserted on the shape that used to exercise it.

    The machinery used to hold an order back while price sat inside the region: a buy limit rests
    *below* the market, so with price inside, the level is above it and `Signal` refuses that as a
    sign error (ADR-0014). Two tests asserted that wait, and a third asserted the repeat that
    happened while it lasted.

    His mitigation rule removes the state they described. Price being inside a demand region means
    `close < top`, which forces `low <= top` — and `low <= top` is exactly what retires it. So by
    the time anything could ask for an order, the region has been marked and its order withdrawn.

    Bar 10 closes at 96, inside [90, 100]. Under the old rule the setup qualified there and the
    entry appeared on bar 11 at 100. Now bar 10 takes the region instead, and bar 11 has nothing
    left to place — which is the correct reading of his method: price came back to the region and
    the region is where it was traded, once.
    """
    inside = [
        bar(10, open_="124", close="96", high="125", low="95"),  # reaches into the region
        bar(11, open_="96", close="101", high="102", low="95"),  # too late; it is spent
    ]
    strategy = StructureStrategy(qualifier=_OnBar(at=10), name="test")
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *inside])

    assert signals[11] == []
    (region,) = [z for z in strategy._blocks.zones if z.block.time == _at(3)]
    assert region.mitigated
    assert not region.usable

    # And no order was ever placed on it — not withdrawn later, never sent.
    assert not [s for per_bar in signals for s in per_bar if s.kind is SignalKind.ENTRY]


def test_a_zone_the_tracker_no_longer_holds_is_never_armed() -> None:
    """A zone that aged out of the tracker is as dead as a mitigated one, and it is refused at
    the moment of arming rather than withdrawn a bar later.

    A bar later is too late. The broker fills before the strategy runs, so an order armed on a
    dead zone can be filled by the very next bar — the cancel would arrive to withdraw an order
    that already became a trade, in a region nothing is watching any more.

    Standing in for an aged-out zone is one the tracker never held, which is indistinguishable
    from a dropped one and does not need the two hundred impulses it would take to overflow the
    window. It also pins that the check is by *value*: the tracker holds two real zones on this
    bar, and neither may answer for this one.
    """
    foreign = OrderBlock(
        kind=ZoneKind.DEMAND,
        top=Decimal("100"),
        bottom=Decimal("90"),
        time=_at(1),  # a bar the detector marked nothing on
        confirmed_at=_at(9),
        break_kind=StructureKind.BOS,
        primary=True,
    )
    quiet = [bar(10, open_="124", close="120", high="125", low="119")]
    signals = _drive_from_bullish(StructureStrategy(qualifier=_Fixed(foreign)), [*_IMPULSE, *quiet])

    assert all(bar_signals == [] for bar_signals in signals)


def test_a_zone_spent_before_the_setup_names_it_is_never_armed() -> None:
    """The same rule against the case it exists for: a *stateful* qualifier naming a zone that
    died while it was remembering it.

    This is the shape of the continuation setup — it has to remember a change of character before
    the break that confirms it can qualify anything — so the gap between "the zone was marked" and
    "the setup names it" is real, and price moves inside it.

    Here the demand zone at [90, 100] is touched on bar 10 and then driven a full width clear of
    it (bar 11 closes at 112, past 110): mitigated the healthy way, meaning the orders that were
    resting there are already in the market and the move they fund is underway. Bar 12 names it
    anyway. Nothing may be armed — and if it were, price dipping back to 100 would buy a region
    that has already done its work.
    """
    spent = [
        bar(10, open_="124", close="105", high="125", low="98"),  # touches the zone
        bar(11, open_="105", close="112", high="113", low="104"),  # driven off: 112 > 110
        bar(12, open_="112", close="114", high="115", low="111"),  # the setup names it here
    ]
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Remembers(at=12)), [*_IMPULSE, *spent]
    )

    assert all(bar_signals == [] for bar_signals in signals)


def test_a_zone_that_gave_a_trade_is_never_armed_again() -> None:
    """One trade per zone, ever — the rule that stops the machine averaging down.

    A zone survives being traded: a wick down through a demand region only marks it *flipped*,
    and mitigation wants a close beyond it. So after the stop takes the trade out, the region is
    still `usable`, and a stateful qualifier still pointing at it would have the machine buy the
    same level again — and again, in a downtrend, until the zone finally breaks. Three losses
    charged to a setup that only ever said "this region is interesting" once.

    The trade opens on bar 10 and is stopped out on bar 11; bar 12 finds the zone still standing
    and the qualifier still naming it. Nothing may be armed a second time. The fill is observed
    here through its fallback sign — the open position while the armed order was on the book —
    which is how a fill whose notification was never delivered still spends its zone.
    """
    after = [
        bar(10, open_="124", close="103", high="125", low="99"),  # fills at 100
        bar(11, open_="103", close="101", high="104", low="88"),  # stopped at 89
        bar(12, open_="101", close="104", high="105", low="100"),
    ]
    strategy = StructureStrategy(qualifier=_Sticky())
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *after], position_on=frozenset({10}))

    assert signals[9][0].kind is SignalKind.ENTRY  # armed once, on the qualifying bar
    assert (signals[11], signals[12]) == ([], [])  # and never again, though the zone still stands
    # The fill *is* the mitigation now: price reaching the near edge is both the entry and what
    # retires the region, so the two reasons to refuse it have become the same event. `_traded`
    # is kept as defence in depth rather than removed — re-arming a spent region is the failure
    # this guard exists to make impossible, and it costs a set lookup.
    (region,) = [z for z in strategy._blocks.zones if z.block.time == _at(3)]
    assert not region.usable
    assert region.block in strategy._traded


def test_a_trade_that_opened_and_died_inside_one_bar_still_spends_its_zone() -> None:
    """The one fill no position can report, and the reason `Context.fills` exists (ADR-0015).

    The broker fills before the strategy runs, so a limit taken at 100 and stopped at 89 by the
    same bar's wick opens a position that is already closed when the strategy sees the bar —
    `context.position` is `None` the whole way through. The zone survives that bar too: a wick
    through only marks it flipped. Miss the fill and the region looks untraded, the sticky
    qualifier re-arms it, and the martingale this class exists to prevent is back.

    Also pinned: **no cancel** is emitted for the consumed order, on this bar or the next — the
    fill observation forgets the armed name, so nothing later tries to withdraw an order the
    trade already used up.
    """
    strategy = StructureStrategy(qualifier=_Sticky())
    descent = [
        bar(10, open_="124", close="115", high="125", low="114"),
        bar(11, open_="115", close="105", high="116", low="104"),
    ]
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *descent])
    [entry] = signals[9]

    wick_out = bar(12, open_="105", close="101", high="106", low="88")  # entry and stop, one bar
    fill = Fill(
        order=OrderRequest(
            symbol=AAPL.symbol,
            side=Side.LONG,
            intent=SignalKind.ENTRY,
            volume=Decimal(1),
            decided_at=_at(9),
            stop_loss=entry.stop_loss,
            limit_price=entry.limit_price,
            client_id=entry.client_id,
        ),
        time=_at(12),
        price=Decimal("100"),
        volume=Decimal(1),
        costs=Decimal(0),
    )
    with localcontext(ENGINE_CONTEXT):
        during = strategy.on_bar(
            Context(candle=wick_out, instrument=AAPL, account=_ACCOUNT, fills=(fill,))
        )
        named_again = bar(13, open_="101", close="104", high="105", low="100")
        after = strategy.on_bar(Context(candle=named_again, instrument=AAPL, account=_ACCOUNT))

    assert (during, after) == ((), ())  # no re-arm, and no cancel for the consumed order
    # Spent and recorded: under his rule the fill retires the region, so "refused as traded" and
    # "refused as dead" now name one event. See the note in the sibling test above.
    (region,) = [z for z in strategy._blocks.zones if z.block.time == _at(3)]
    assert not region.usable
    assert region.block in strategy._traded


def test_a_fill_spends_the_region_it_traded_and_no_other() -> None:
    """A trade retires **its own** region. Every other region the impulse left stays tradeable.

    The two siblings above both end in silence, so neither can tell "the fill spent one region"
    apart from "the fill ended the machine". Nor is the difference academic: the impulse leaves a
    primary and a secondary, and a rule that let one trade retire the pair would quietly halve the
    regions the method offers — the same class of loss as the mitigation bug this PR fixed, and
    just as invisible in the totals.

    Geometry, and it is the reverse of what this scenario used to do. The traded region has to be
    the **upper** one, because a fill is a touch of the near edge and price cannot reach a lower
    region's edge without first passing through a higher one's. So the secondary at [110, 117] is
    the one bought — limit 117, stop 109.3, a tenth of the zone's own width under the floor — and
    the flash bar's low of 109 takes entry and stop together without ever reaching 100. The
    primary [90, 100] is left untouched, alive, and free to arm on the next bar that names it.

    Two honest notes on what this does *not* own, so nobody has to rediscover them. The bar-12
    silence is the sibling's claim, not this one: under his mitigation rule an unobserved fill
    leaves `_armed` on a region the same bar just retired, so the phantom withdrawal fires on bar
    12 there too. And the mutants this kills — the fill forgetting to clear `_armed`, forgetting
    to record `_traded`, spending the whole tracker — each die in some sibling as well; measured,
    and the file's coverage of `setups.py` is unchanged without it. It is kept because it is the
    only scenario here whose *subject* is a fill's blast radius, and the only one that reaches it
    through a hand-delivered `Context.fills` (ADR-0015) rather than through a broker.

    What no test in this file kills is the `_traded` **guard** in `_may_arm` — deleting it leaves
    the suite green. That is not an oversight to fix here; the guard is unreachable by
    construction under his mitigation rule and is kept on purpose. `StructureStrategy`'s docstring
    carries the argument and the measurement.
    """
    strategy = StructureStrategy(
        qualifier=_Script(picks={9: 1, 13: 0, 14: 1}), allow_secondary=True
    )
    descent = [  # down off the break, but clear of 117: the secondary must survive to be filled
        bar(10, open_="124", close="120", high="125", low="119"),
        bar(11, open_="120", close="119", high="121", low="118"),
    ]
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *descent])
    [entry] = signals[9]
    assert (entry.limit_price, entry.stop_loss) == (Decimal("117"), Decimal("109.3"))

    wick_out = bar(12, open_="118", close="118", high="119", low="109")  # fills 117, stops 109.3
    fill = Fill(
        order=OrderRequest(
            symbol=AAPL.symbol,
            side=Side.LONG,
            intent=SignalKind.ENTRY,
            volume=Decimal(1),
            decided_at=_at(9),
            stop_loss=entry.stop_loss,
            limit_price=entry.limit_price,
            client_id=entry.client_id,
        ),
        time=_at(12),
        price=Decimal("117"),
        volume=Decimal(1),
        costs=Decimal(0),
    )
    with localcontext(ENGINE_CONTEXT):
        during = strategy.on_bar(
            Context(candle=wick_out, instrument=AAPL, account=_ACCOUNT, fills=(fill,))
        )
        primary_bar = bar(13, open_="118", close="119", high="120", low="117")  # names [90, 100]
        primary_signals = strategy.on_bar(
            Context(candle=primary_bar, instrument=AAPL, account=_ACCOUNT)
        )
        traded_again = bar(14, open_="119", close="120", high="121", low="118")  # names the spent
        again_signals = strategy.on_bar(
            Context(candle=traded_again, instrument=AAPL, account=_ACCOUNT)
        )

    assert during == ()  # nothing to withdraw: the fill took the name with it
    [primary_entry] = primary_signals  # exactly one signal, and no cancel in front of it
    assert primary_entry.kind is SignalKind.ENTRY
    assert primary_entry.limit_price == Decimal("100")  # the primary region, untouched and alive
    assert again_signals == ()  # the traded region is spent; the new order stays where it is

    primary, secondary = strategy._blocks.zones
    assert primary.usable  # untouched by the trade that happened above it
    assert primary.block not in strategy._traded
    assert not secondary.usable  # the fill was the touch of its edge
    assert secondary.block in strategy._traded


def test_a_zone_withdrawn_unfilled_may_be_offered_again() -> None:
    """The author's rule, drawn at the fill: placing the order and *activating the trade* is
    what spends a region. Zone one's order was withdrawn to make room for zone two before price
    ever came back, so no trade happened there — and when the setup names zone one again, the
    machinery arms it again, under a fresh name. Only a fill closes a region for good.
    """
    second = [
        bar(10, open_="124", close="118", high="125", low="117"),  # correction 1
        bar(11, open_="118", close="116", high="119", low="115"),  # correction 2
        bar(12, open_="115", close="115", high="116", low="114"),
        bar(13, open_="121", close="121", high="122", low="120"),  # gap: 116 < 120
        bar(14, open_="128", close="128", high="129", low="126"),  # BOS -> zone two supersedes
        bar(15, open_="128", close="127", high="129", low="126"),  # zone one is named again
    ]
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Script(picks={9: 0, 14: 1, 15: 0})), [*_IMPULSE, *second]
    )

    [first_entry] = signals[9]
    second_entry = signals[14][1]
    [cancel, re_entry] = signals[15]
    assert (cancel.kind, cancel.client_id) == (SignalKind.CANCEL, second_entry.client_id)
    assert re_entry.kind is SignalKind.ENTRY
    assert (re_entry.limit_price, re_entry.stop_loss) == (Decimal("100"), Decimal("89"))
    assert re_entry.client_id != first_entry.client_id  # the old name died with its withdrawal


def test_a_filled_order_is_not_withdrawn_when_a_later_zone_qualifies() -> None:
    """The other half of forgetting the armed zone once a position opens.

    Holding on to the name would mean the next qualified zone emits a cancel for it — an order
    that is not resting any more, because it became the trade that just closed. The broker
    answers `False` and nothing breaks, but the signal is a lie about what the strategy holds,
    and in live it is a round trip to the venue for an order that no longer exists.
    """
    after = [
        bar(10, open_="124", close="118", high="125", low="117"),  # position open (filled)
        bar(11, open_="118", close="116", high="119", low="115"),  # correction 1
        bar(12, open_="115", close="115", high="116", low="114"),  # correction 2
        bar(13, open_="121", close="121", high="122", low="120"),  # gap: 116 < 120
        bar(14, open_="128", close="128", high="129", low="126"),  # BOS -> a second zone
    ]
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()), [*_IMPULSE, *after], position_on=frozenset({10})
    )

    assert [s.kind for s in signals[14]] == [SignalKind.ENTRY]  # no cancel for the filled order


def test_a_secondary_zone_the_qualifier_read_from_the_tracker_is_refused() -> None:
    """`allow_secondary` is a rule about which regions may be *traded*, so it bites where the
    trade is decided — not only on the list of zones the qualifier is offered.

    A qualifier can name any zone it can see, and `SetupContext.zones` deliberately shows all of
    them: the flip setup does not qualify on a break at all, it qualifies when a zone is taken
    out, so filtering only `marked` would leave the flag with no effect on flip. Here the
    qualifier reaches past `marked` into the tracker and names the secondary zone; with the flag
    off, nothing is armed.
    """
    off = _drive_from_bullish(StructureStrategy(qualifier=_FromTracker(index=1)), _IMPULSE)
    assert all(bar_signals == [] for bar_signals in off)

    on = _drive_from_bullish(
        StructureStrategy(qualifier=_FromTracker(index=1), allow_secondary=True), _IMPULSE
    )
    [signal] = on[9]
    assert signal.limit_price == Decimal("117")  # the secondary zone's top


# --------------------------------------------------------------------------- #
# Refusals                                                                      #
# --------------------------------------------------------------------------- #


def test_a_negative_stop_buffer_is_refused() -> None:
    with pytest.raises(ValueError, match="fraction of the zone width"):
        StructureStrategy(qualifier=_Marked(), stop_buffer=Decimal("-0.1"))


def test_a_stop_that_would_land_at_or_below_zero_arms_nothing() -> None:
    """A stop at a non-positive price is not a wide stop — it is *no stop*, because `low <= stop`
    can never be true, and nothing downstream asks whether a stop is reachable.

    It takes a zone more than ten times as tall as its own floor, which no currency pair produces
    and a crypto flash crash does. Zone [1, 100]: the width is 99, the buffer 9.9, and the stop
    would be 1 - 9.9 = -8.9. The trade would run with no exit at all on the losing side.
    """
    candles = [*_IMPULSE]
    candles[3] = bar(3, open_="99", close="99", high="100", low="1")  # zone [1, 100]
    signals = _drive_from_bullish(StructureStrategy(qualifier=_Marked()), candles)

    assert all(bar_signals == [] for bar_signals in signals)


def test_a_zone_with_no_width_arms_nothing() -> None:
    """Both edges at one price: the stop would land on the entry and the trade would carry no
    risk at all — which is not a free trade, it is a division by zero in position sizing."""
    candles = [*_IMPULSE]
    # A marking candle with no range: high == low, so top == bottom.
    candles[3] = bar(3, open_="100", close="100", high="100", low="100")
    signals = _drive_from_bullish(StructureStrategy(qualifier=_Marked()), candles)

    assert all(bar_signals == [] for bar_signals in signals)


# --------------------------------------------------------------------------- #
# End to end, through the real loop and broker                                  #
# --------------------------------------------------------------------------- #


def test_the_order_fills_at_the_zone_edge_when_price_comes_back() -> None:
    """The whole point, through `run()`: the order rests at 100 and fills there, not at an open.

    Price leaves the zone on the break, drifts back down, and bar 12 dips to 98 — through the
    100 edge. The limit fills at the level itself, and the trade is sized against the 89 stop the
    machinery set. Filling at bar 12's open of 105 instead would be the entry the method never
    took, five dollars worse on an eleven-dollar risk.
    """
    pullback = [
        bar(10, open_="124", close="115", high="125", low="114"),
        bar(11, open_="115", close="105", high="116", low="104"),
        bar(12, open_="105", close="99", high="106", low="98"),  # reaches the 100 edge
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *pullback],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=_Marked()),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    [fill] = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert fill.time == _at(12)
    assert fill.price == Decimal("100")  # the zone's edge, not bar 12's open of 105
    assert fill.order.stop_loss == Decimal("89")


# --------------------------------------------------------------------------- #
# Where the order rests inside the region — his book, 11.4                     #
# --------------------------------------------------------------------------- #

# The region `_IMPULSE` marks is [90, 100] — the author's own example numbers. Midpoint 95,
# stop 89, and the level a visited region is abandoned at is 100 + 10 = 110.
_PULLBACK_TO_98 = [
    bar(10, open_="124", close="115", high="125", low="114"),
    bar(11, open_="115", close="105", high="116", low="104"),
    bar(12, open_="105", close="99", high="106", low="98"),  # takes the 100 edge, stops at 98
]


def test_the_midpoint_entry_rests_at_half_the_region_with_the_same_stop() -> None:
    """His model 3: the order at 50%, the stop still past the far edge.

    The stop is the point. Both models put it at 89, so the midpoint entry buys the *same* zone
    with 6 of risk instead of 11 — which is his whole argument for preferring it: "um stop 50%
    menor... permite aumentar a quantidade de contratos, mantendo o mesmo valor de risco".
    """
    [edge] = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.EDGE), _IMPULSE
    )[9]
    [midpoint] = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.MIDPOINT), _IMPULSE
    )[9]

    assert (edge.limit_price, edge.stop_loss) == (Decimal("100"), Decimal("89"))
    assert (midpoint.limit_price, midpoint.stop_loss) == (Decimal("95"), Decimal("89"))


def test_the_midpoint_is_rounded_so_the_tick_never_flatters_the_entry() -> None:
    """A region whose half lands between ticks, on both sides of the book.

    [90.01, 100] is 9.99 wide, so the true midpoint is 95.005 and AAPL's grid is a cent. A buy
    rounds **up** to 95.01 and the mirrored sell rounds **down** to 104.99: in both directions the
    order is placed at a price no better than the midpoint really is. Rounding to nearest would
    hand half of all odd-width regions an entry the method never offered — the same reasoning that
    already rounds the stop away from the zone rather than onto it.
    """
    odd = list(_IMPULSE)
    odd[3] = bar(3, open_="99", close="99", high="100", low="90.01")  # region [90.01, 100]

    [long_] = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.MIDPOINT), odd
    )[9]
    [short] = _drive(
        StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.MIDPOINT), _mirror(odd)
    )[9]

    assert long_.limit_price == Decimal("95.01")  # up, away from a better buy
    assert short.limit_price == Decimal("104.99")  # down, away from a better sell


def test_the_pullback_that_fills_on_the_edge_leaves_the_midpoint_on_the_stone() -> None:
    """The same three bars, the two models, opposite outcomes — through the real broker.

    Price dips to 98. That is through the 100 edge and nowhere near 95, so the edge model is in a
    trade and the midpoint model is not. His own words for the second half: "muitas vezes o preço
    não chega aos 50% e acaba indo em direção ao nosso alvo sem nos ativar, deixando-nos na
    pedra". It is the cost of the smaller stop, and it is a real cost.
    """

    def entries(entry_point: ZoneEntryPoint) -> list[Fill]:
        result = run(
            candles=[*BULLISH_START, *_IMPULSE, *_PULLBACK_TO_98],
            timeframe=HOUR,
            instrument=AAPL,
            strategy=StructureStrategy(qualifier=_Marked(), entry_point=entry_point),
            broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
            risk=FixedRisk(volume=Decimal(1)),
        )
        return [f for f in result.fills if f.order.intent is SignalKind.ENTRY]

    [filled] = entries(ZoneEntryPoint.EDGE)
    assert (filled.time, filled.price) == (_at(12), Decimal("100"))
    assert entries(ZoneEntryPoint.MIDPOINT) == []


def test_the_midpoint_order_outlives_the_touch_that_retires_its_region() -> None:
    """⚠️ **The rule this whole entry point depends on**, and the one a mutant kills silently.

    Bar 12 wicks to 98, which takes the region's 100 edge: under his indicator the region is
    *mitigated* there, and it is never offered again. Until now that same touch also withdrew the
    order, because on the edge the two were one event. An order at 95 has not been reached by it.

    Bar 13 goes to 94 and the order fills at 95 — one bar after the region was retired. Restore
    "mitigation withdraws the order" and this is a scenario with no trade in it at all, which is
    what `ZoneEntryPoint.MIDPOINT` would be worth.
    """
    result = run(
        candles=[
            *BULLISH_START,
            *_IMPULSE,
            *_PULLBACK_TO_98,
            bar(13, open_="99", close="95", high="100", low="94"),  # reaches the 95 midpoint
        ],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.MIDPOINT),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    [fill] = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert fill.time == _at(13)  # a bar *after* the region was mitigated
    assert fill.price == Decimal("95")
    assert fill.order.stop_loss == Decimal("89")


def test_an_order_is_abandoned_once_price_leaves_a_region_it_visited() -> None:
    """His second event: the region was visited, the order was not reached, the market left.

    Bar 12 takes the 100 edge and turns at 98. Bar 14 reaches 110 — one region height above the
    edge — and the order is withdrawn there. "Quando o preço atingir 110 a ordem deve ser retirada
    e a entrada neste ponto deve ser abortada."
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Once(), entry_point=ZoneEntryPoint.MIDPOINT),
        [
            *_IMPULSE,
            *_PULLBACK_TO_98,
            bar(13, open_="99", close="105", high="106", low="99"),
            bar(14, open_="105", close="109", high="110", low="104"),  # 110: one height clear
        ],
    )

    [armed] = signals[9]
    assert (armed.kind, armed.limit_price) == (SignalKind.ENTRY, Decimal("95"))
    assert signals[13] == []  # 106 is not yet a height clear of the edge
    [cancel] = signals[14]
    assert (cancel.kind, cancel.client_id) == (SignalKind.CANCEL, armed.client_id)


def test_the_sell_side_abandons_below_the_region_it_visited() -> None:
    """The same rule mirrored — and it is a separate test because the sign is a separate line.

    ⚠️ Written after a review found that **nothing here exercised the sell branch of
    `_ran_away`**. Two plausible mutants survived the whole suite: flipping `bottom - clearance`
    to `bottom + clearance`, and pasting the buy branch's `candle.high >=` over it. Either one
    withdraws the order on the bar the region is *mitigated* — reinstating, on the sell side only,
    the exact behaviour this entry point exists to remove. `MIDPOINT` would then be a setting that
    cannot produce a short trade, in a backtest that runs clean and reports a number.

    Mirrored about 200: the supply region is [100, 110], the order rests at 105 with its stop at
    111, and abandonment is one height *below* the near edge — 90. Bar 12 wicks to 94 and takes
    the 100 edge; bar 13 goes no lower than 94; bar 14 reaches 90.
    """
    signals = _drive(
        StructureStrategy(qualifier=_Once(), entry_point=ZoneEntryPoint.MIDPOINT),
        _mirror(
            [
                *_IMPULSE,
                *_PULLBACK_TO_98,
                bar(13, open_="99", close="105", high="106", low="99"),
                bar(14, open_="105", close="109", high="110", low="104"),
            ]
        ),
    )

    [armed] = signals[9]
    assert (armed.side, armed.limit_price, armed.stop_loss) == (
        Side.SHORT,
        Decimal("105"),
        Decimal("111"),
    )
    assert signals[12] == []  # the bar that mitigates the region does not take the order
    assert signals[13] == []  # 94 is not yet a height clear of the 100 edge
    [cancel] = signals[14]
    assert (cancel.kind, cancel.client_id) == (SignalKind.CANCEL, armed.client_id)


def test_an_order_is_not_abandoned_while_price_has_never_come_back() -> None:
    """⚠️ **The clock starts at the visit, not at the arming** — and getting this backwards is not
    a corner case, it retires every order the setup ever places.

    A demand region is *left behind* by an upward impulse, so at the moment it is marked price is
    already walking away from it: `_IMPULSE` itself trades 110, 115 and 125 while the region sits
    at [90, 100]. Bar 10 here goes to 131. Measured against the first version of this rule, which
    counted clearance from the arming bar: 9 of this file's tests failed, all of them orders
    abandoned before price had any chance to return.

    Nothing is emitted after the entry. The order is still waiting for exactly the event it was
    placed for.
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Once(), entry_point=ZoneEntryPoint.MIDPOINT),
        [*_IMPULSE, bar(10, open_="124", close="130", high="131", low="123")],  # never near 100
    )

    [armed] = signals[9]
    assert armed.kind is SignalKind.ENTRY
    assert signals[10] == []


def test_a_one_bar_round_trip_through_the_real_broker_spends_the_zone() -> None:
    """The whole chain — broker fill, loop hand-off, strategy observation — on the trade the
    strategy never sees as a position.

    Bar 12 dips through the 100 edge and the 89 stop in one sweep: the limit fills and the wick
    takes the stop before the bar even closes, so the strategy runs with `position=None` on the
    very bar its trade happened. The zone survives — a wick through only marks it flipped — and
    the sticky qualifier keeps naming it. Bar 13 then dips through the level again: if the fill
    had gone unnoticed, a re-armed order would fill there at 100 a second time. Exactly one
    entry and one closed trade may exist.
    """
    pullback = [
        bar(10, open_="124", close="115", high="125", low="114"),
        bar(11, open_="115", close="105", high="116", low="104"),
        bar(12, open_="105", close="101", high="106", low="88"),  # fills at 100, stopped at 89
        bar(13, open_="101", close="104", high="105", low="99"),  # back through the level
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *pullback],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=_Sticky()),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    entries = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert [(f.time, f.price) for f in entries] == [(_at(12), Decimal("100"))]  # once, not twice
    [trade] = result.trades
    assert (trade.entry_price, trade.exit_price) == (Decimal("100"), Decimal("89"))


# --------------------------------------------------------------------------- #
# The return pass — his book, 11.5                                             #
# --------------------------------------------------------------------------- #

# The same [90, 100] region, read the other way round. Nothing is placed when the zone is armed;
# price coming back to the 50% at 95 is what places a **stop** at 101, one buffer above the near
# edge, and the order fills only if price then passes back out through the region. It is cancelled
# at 89 — the level its own stop would have occupied.
_RETURN_TO_95 = bar(13, open_="99", close="97", high="100", low="95")


def _return_pass() -> StructureStrategy:
    """A return-pass setup on the first zone the detector marks, and nothing after it.

    `_Once` rather than `_Marked` because these scenarios drive price back through the region and
    out again, which is decisive enough to mark zones of its own — and a qualifier that took one
    would leave the test asserting two setups at once.
    """
    return StructureStrategy(qualifier=_Once(), entry_point=ZoneEntryPoint.RETURN_PASS)


def test_the_return_pass_waits_for_the_fifty_percent_before_placing_anything() -> None:
    """His model 11.5 against his 11.4, on the *same bars*, which is the whole contrast.

    `MIDPOINT` places on bar 9, the bar its zone is armed on, and waits inside the region for
    price to come down to it. `RETURN_PASS` places nothing there — the book stays empty for four
    bars — and the order appears on bar 13, the bar price finally reaches the 50%. The level that
    was the *entry* for one model is the *trigger* for the other, which is his own sentence:
    "atinge o 50% onde o gatilho anterior ativaria o trade".

    ⚠️ The two assertions on bar 9 are not decoration. Without the empty one, every claim here
    also passes for an implementation that places at arming and merely computes a different
    price — which is the likeliest wrong version of this feature.
    """
    bars = [*_IMPULSE, *_PULLBACK_TO_98, _RETURN_TO_95]

    midpoint = _drive_from_bullish(
        StructureStrategy(qualifier=_Once(), entry_point=ZoneEntryPoint.MIDPOINT), bars
    )
    return_pass = _drive_from_bullish(_return_pass(), bars)

    [resting] = midpoint[9]
    assert (resting.limit_price, resting.stop_loss) == (Decimal("95"), Decimal("89"))
    assert return_pass[9] == []  # nothing is placed when the zone is armed
    assert return_pass[10] == return_pass[11] == return_pass[12] == []
    [placed] = return_pass[13]
    assert placed.kind is SignalKind.ENTRY


def test_the_return_pass_order_is_a_stop_above_the_region_not_a_limit_inside_it() -> None:
    """The order shape, and it is the reason this entry point could not be a third price.

    A demand [90, 100] triggers at 101 — the near edge plus the same buffer that puts the stop at
    89 — and it is a `stop_price`, not a `limit_price` (ADR-0016). The distinction is not
    bookkeeping: a limit at 101 with the market at 97 rests on the wrong side and `Signal` refuses
    it, and a limit that *were* accepted there would fill instantly at the next open instead of
    waiting for price to break back out of the region, which is the entire event this model is
    named after.

    Its cost is visible in the same numbers: risk 12, against the edge's 11 and the midpoint's 6.
    That is what it pays for not needing price to turn while sitting inside the region.
    """
    [placed] = _drive_from_bullish(_return_pass(), [*_IMPULSE, *_PULLBACK_TO_98, _RETURN_TO_95])[13]

    assert placed.limit_price is None
    assert (placed.side, placed.stop_price, placed.stop_loss) == (
        Side.LONG,
        Decimal("101"),
        Decimal("89"),
    )


def test_the_sell_side_of_the_return_pass_places_its_stop_below_the_region() -> None:
    """The mirror, and a separate test because every one of these signs is a separate line.

    ⚠️ Written up front rather than after a review asked for it: the last entry point shipped six
    tests, all of them long, and two plausible mutants lived in the mute half. Mirrored about 200
    the supply region is [100, 110], price comes back up to the 50% at 105, and the sell stop
    goes one buffer *below* the near edge at 99 with its stop at 111.
    """
    [placed] = _drive(_return_pass(), _mirror([*_IMPULSE, *_PULLBACK_TO_98, _RETURN_TO_95]))[13]

    assert placed.limit_price is None
    assert (placed.side, placed.stop_price, placed.stop_loss) == (
        Side.SHORT,
        Decimal("99"),
        Decimal("111"),
    )


def test_the_trigger_is_the_same_fifty_percent_the_midpoint_entry_rests_at() -> None:
    """Both models read one function, and this is the zone where reading two would show.

    ⚠️ **On [90, 100] the midpoint is 95 and every plausible rounding agrees**, so the ordinary
    fixture cannot tell a shared level from two coincidentally-equal ones. Widened by a single
    tick to [90, 100.01], half falls at 95.005 — off the grid — and the direction of the rounding
    becomes observable: `MIDPOINT` rests at 95.01, and the return pass arms on a bar whose low
    reaches 95.01 but not on one that stops at 95.02.

    A `ROUND_FLOOR` trigger would put the level at 95.00 and let the 95.01 bar pass untouched,
    with nothing else in the suite disagreeing.
    """
    odd = list(_IMPULSE)
    odd[3] = bar(3, open_="99", close="99", high="100.01", low="90")

    [resting] = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.MIDPOINT), odd
    )[9]
    assert resting.limit_price == Decimal("95.01")

    def armed_on(low: str) -> list[Signal]:
        stops_at = bar(13, open_="99", close="97", high="100", low=low)
        return _drive_from_bullish(_return_pass(), [*odd, *_PULLBACK_TO_98, stops_at])[13]

    assert armed_on("95.02") == []  # a tick short of the level, and it is a tick that counts
    [placed] = armed_on("95.01")
    assert placed.kind is SignalKind.ENTRY
    # ⚠️ The trigger is off the grid on this zone too — 100.01 + 1.001 = 101.011 — so the
    # direction of *its* rounding is observable here and nowhere else in the suite. Rounded down
    # it would sit at 101.01: a tick nearer the market, filling on bars that should not reach it,
    # and sizing the trade against a risk of 12.02 rather than the 12.03 the region asks for.
    assert (placed.stop_price, placed.stop_loss) == (Decimal("101.02"), Decimal("88.99"))


def test_the_sell_trigger_is_the_same_fifty_percent_its_midpoint_entry_rests_at() -> None:
    """The identity mirrored, on the zone where the direction of the rounding is observable.

    ⚠️ **This is the half the previous entry point shipped without.** Its sell tests all used
    regions whose midpoint lands on the grid, where both roundings agree — so a mutant reading
    half with the *buy's* rounding lived in the mute side of the mirror, and only the sell was
    wrong. The reflection of [90, 100.01] is supply [99.99, 110]: half falls at 104.995, a sell
    is rounded down, and `MIDPOINT` rests at 104.99. Rounded up the level would be 105.00, the
    return pass would arm a bar later than the model it is defined against, and nothing
    downstream would say so.

    The trigger's own rounding is here for the same reason: 99.99 - 1.001 = 98.989 is off the
    grid, and rounded up it would be 98.99 — a tick nearer the market, on the side where nearer
    means easier to trigger.
    """
    odd = list(_IMPULSE)
    odd[3] = bar(3, open_="99", close="99", high="100.01", low="90")

    [resting] = _drive(
        StructureStrategy(qualifier=_Marked(), entry_point=ZoneEntryPoint.MIDPOINT), _mirror(odd)
    )[9]
    assert resting.limit_price == Decimal("104.99")

    def armed_on(low: str) -> list[Signal]:
        # Parametrised on the pre-mirror *low*, as the other mirrored scenarios are: reflection
        # turns a low of 95.01 into the high of 104.99 that this test is actually about.
        stops_at = bar(13, open_="99", close="97", high="100", low=low)
        return _drive(_return_pass(), _mirror([*odd, *_PULLBACK_TO_98, stops_at]))[13]

    assert armed_on("95.02") == []  # mirrors to a high of 104.98, a tick short of the level
    [placed] = armed_on("95.01")
    assert placed.kind is SignalKind.ENTRY
    assert (placed.stop_price, placed.stop_loss) == (Decimal("98.98"), Decimal("111.01"))


def test_the_return_pass_order_is_cancelled_where_its_own_stop_would_have_been() -> None:
    """His cancel rule: reach 89 and the order is taken back rather than left to fill.

    The setup never opens a trade that is already stopped — which is what a fill at 101 would be
    on a bar that had traded 89 first. `_ran_away`, the rule that ends the other two entry points,
    cannot serve here: it fires when price *leaves* the region upward, and leaving upward is this
    order's fill.

    The paired bar is what makes it a rule rather than a coincidence — a low of 90 leaves the
    order standing, and only 89 takes it back.
    """

    def after(low: str) -> list[list[Signal]]:
        return _drive_from_bullish(
            _return_pass(),
            [
                *_IMPULSE,
                *_PULLBACK_TO_98,
                _RETURN_TO_95,
                bar(14, open_="97", close="92", high="98", low=low),
            ],
        )

    standing = after("90")
    assert standing[14] == []  # one above the level, and the order is still there

    cancelled = after("89")
    [placed] = cancelled[13]
    [cancel] = cancelled[14]
    assert (cancel.kind, cancel.client_id) == (SignalKind.CANCEL, placed.client_id)


def test_the_sell_side_is_cancelled_above_its_region() -> None:
    """The cancel rule mirrored — the sign of the comparison is its own line, so it is its own
    test. Supply [100, 110]: the order rests at 99 and is taken back when price reaches 111."""

    def after(low: str) -> list[list[Signal]]:
        # Parametrised on the *low*, because reflection swaps the extremes: a pre-mirror low of 89
        # is the mirrored bar's high of 111. Naming the level we care about directly would build a
        # candle that does not contain its own body, which `Candle` refuses.
        return _drive(
            _return_pass(),
            _mirror(
                [
                    *_IMPULSE,
                    *_PULLBACK_TO_98,
                    _RETURN_TO_95,
                    bar(14, open_="97", close="92", high="98", low=low),
                ]
            ),
        )

    assert after("90")[14] == []  # mirrors to a high of 110, one short of the level
    cancelled = after("89")  # mirrors to 111
    [placed] = cancelled[13]
    [cancel] = cancelled[14]
    assert (cancel.kind, cancel.client_id) == (SignalKind.CANCEL, placed.client_id)


def test_the_cancel_level_is_the_orders_own_stop_on_a_zone_that_falls_between_ticks() -> None:
    """One level, asserted against the order that carries it — not two that happen to agree.

    ⚠️ **On [90, 100] the buffer lands on the grid**: 90 - 1 is 89 whichever way it is rounded,
    so the ordinary fixture cannot tell a shared call from two expressions written separately.
    Widened by a tick to [90, 100.01] the buffer is 1.001, the level falls at 88.999, and the
    rounding becomes visible — the order's stop is 88.99 and so is the level that takes it back.

    Rounded the other way the cancel would sit at 89.00 and retire the order on a bar whose low
    was 89.00: a bar that never touched the stop, and that could go on to break 101.02 for a
    legitimate trade. That trade would disappear from every backtest, filed as a cancellation,
    with no test failing and nothing to say a level had drifted a tick.
    """
    odd = list(_IMPULSE)
    odd[3] = bar(3, open_="99", close="99", high="100.01", low="90")

    def after(low: str) -> list[list[Signal]]:
        return _drive_from_bullish(
            _return_pass(),
            [
                *odd,
                *_PULLBACK_TO_98,
                _RETURN_TO_95,  # a low of 95 still reaches this zone's 50%, which is 95.01
                bar(14, open_="97", close="92", high="98", low=low),
            ],
        )

    standing = after("89.00")
    [placed] = standing[13]
    assert placed.stop_loss == Decimal("88.99")
    assert standing[14] == []  # one tick above its own stop, and the order is still there

    cancelled = after("88.99")  # the stop's own level, to the tick
    [cancel] = cancelled[14]
    assert (cancel.kind, cancel.client_id) == (SignalKind.CANCEL, placed.client_id)


def test_a_bar_that_reaches_the_cancel_level_and_the_trigger_fills_and_is_stopped() -> None:
    """⚠️ The one bar where the cancel rule and the fill rule both apply, and the engine chooses.

    Bar 14 opens at 98, wicks down to 89 — the level that takes the order back — and then trades
    107, through the 101 trigger. Two rules answer on the same bar and only one thing can happen.
    His rule says reaching 89 cancels; the engine fills and stops, for -12.

    It is not lookahead, and it is the **pessimistic** of the two readings — cancelling would end
    the bar at zero — so it is the safe way to be wrong, and it is what a real venue would do
    with an order that was working when the market traded through it. But it is a precedence
    nothing states, in a path whose own documentation says the opposite, so the number is nailed
    down here rather than left to be rediscovered as a surprise in a backtest.
    """
    result = run(
        candles=[
            *BULLISH_START,
            *_IMPULSE,
            *_PULLBACK_TO_98,
            _RETURN_TO_95,  # places the stop at 101; its cancel level is 89
            bar(14, open_="98", close="103", high="107", low="89"),
        ],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=_return_pass(),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    [trade] = result.trades
    assert (trade.entry_price, trade.exit_price, trade.net_pnl) == (
        Decimal("101"),
        Decimal("89"),
        Decimal("-12"),
    )


def test_the_return_pass_is_not_abandoned_by_the_rule_that_ends_the_other_two() -> None:
    """The third of the three rules this model announces, and the one that had no test.

    `_ran_away` retires `EDGE` and `MIDPOINT` when price leaves the region by a full height —
    for an order waiting *inside* the zone, leaving is the trade going away. For `RETURN_PASS`
    leaving upward is the **fill**, so the rule cannot serve, and wiring it back in survives
    every other scenario in this file.

    The bars that separate them: 12 touches the 100 edge and mitigates the region, 13 clears it
    by a full height to 110 *without ever reaching the 50%*, and 14 comes back for 95.
    `_ran_away` would have given the name up on bar 13, and the return the whole model exists to
    wait for would arrive to find nothing armed.
    """
    out = _drive_from_bullish(
        _return_pass(),
        [
            *_IMPULSE,
            bar(10, open_="124", close="115", high="125", low="114"),
            bar(11, open_="115", close="105", high="116", low="104"),
            bar(12, open_="105", close="101", high="106", low="100"),  # first touch: mitigated
            bar(13, open_="101", close="109", high="110", low="100"),  # a full height clear
            bar(14, open_="101", close="97", high="101", low="95"),  # back for the 50%
        ],
    )

    assert (out[12], out[13]) == ([], [])  # nothing placed on the way out, and nothing retired
    [placed] = out[14]
    assert (placed.kind, placed.stop_price, placed.stop_loss) == (
        SignalKind.ENTRY,
        Decimal("101"),
        Decimal("89"),
    )


def test_a_bar_that_closes_past_the_trigger_gives_the_zone_up_instead_of_placing() -> None:
    """⚠️ The bar that does the whole move by itself, and the one place this could fill a trade
    the method never took.

    A bar can wick to the 50% and close back out above 101. The order cannot be placed there: a
    buy stop at 101 with the market at 103 reaches the book already triggered, so it becomes a
    market order at the next open — sized against a level price never had to break. ADR-0016 says
    exactly this about a stop on the wrong side, and `Signal` would raise.

    The zone is given up rather than kept waiting, because keeping it would place the order on
    some later dip to the 50% and enter on a break of a level price has already broken.

    Three closes, one boundary: 100 places, 101 does not, 103 does not. The first is what makes
    the other two mean something — the machine is speaking on these bars, and choosing silence.
    """

    def closing_at(close: str, high: str) -> list[Signal]:
        wick = bar(13, open_="99", close=close, high=high, low="95")
        return _drive_from_bullish(_return_pass(), [*_IMPULSE, *_PULLBACK_TO_98, wick])[13]

    [placed] = closing_at("100", "100")
    assert placed.stop_price == Decimal("101")
    assert closing_at("101", "102") == []  # closed *at* the trigger: already through it
    assert closing_at("103", "104") == []


def test_a_sell_trigger_the_buffer_drives_to_zero_or_below_arms_nothing() -> None:
    """⚠️ A guard this entry point needs and the other two do not, because it inverts which side
    of the region carries the order.

    For a limit entry only a *demand* zone can push a level through zero — its stop is the one
    below the region — and `_entry_for` has refused that for as long as it has existed. A return
    pass puts the **order** on the low side of a supply zone, so it is the supply mirror that
    breaks: here the stop is a healthy 310 and the trigger is -100.

    That asymmetry is the test. Both sides go quiet under an absurd buffer, but for different
    reasons — the buy on the old stop guard, the sell on the new trigger one — so the sell side
    is the only one that says anything about the line this test exists for. Without the guard the
    order is not merely wrong, it raises: `Signal` refuses a non-positive stop price outright.
    """
    absurd = StructureStrategy(
        qualifier=_Once(), entry_point=ZoneEntryPoint.RETURN_PASS, stop_buffer=Decimal(20)
    )
    bars = [*_IMPULSE, *_PULLBACK_TO_98, _RETURN_TO_95]

    assert _drive(absurd, _mirror(bars))[13] == []
    # And the same bars at the real buffer do place, so the silence above is a refusal rather
    # than a scenario that never reached the trigger.
    [placed] = _drive(_return_pass(), _mirror(bars))[13]
    assert placed.stop_price == Decimal("99")


def test_a_zone_given_up_before_its_order_reached_the_book_sends_no_cancel() -> None:
    """A cancel for an order the venue never received is noise, and 11.5 would make it routine.

    Under the two limit entries an armed zone has an order on the book within the same bar, so
    withdrawing an unplaced one was a rarity the broker's tolerant `False` covered. A return-pass
    zone is armed with nothing on the book for as many bars as price takes to come back, and most
    never get an order at all.

    Both halves are asserted here, because silence only means something if the machine had a
    chance to speak: the zone that never placed goes quietly, and the one that did sends its
    cancel by name.
    """
    never_placed = _drive_from_bullish(
        _return_pass(),
        [*_IMPULSE, *_PULLBACK_TO_98, bar(13, open_="99", close="92", high="100", low="89")],
    )
    assert never_placed[13] == []  # it reached the 50% and the cancel level on one bar

    placed_then_cancelled = _drive_from_bullish(
        _return_pass(),
        [
            *_IMPULSE,
            *_PULLBACK_TO_98,
            _RETURN_TO_95,
            bar(14, open_="97", close="92", high="98", low="89"),
        ],
    )
    assert [s.kind for s in placed_then_cancelled[14]] == [SignalKind.CANCEL]


# --------------------------------------------------------------------------- #
# The choch setup                                                               #
# --------------------------------------------------------------------------- #

# The impulse's mirror image: after the bar-9 BOS, a leg down through the 90 anchor. One gap on
# the way (bars 9-10-11: 120 > 118), so the change of character marks the supply zone [120, 125]
# — the c1 of the inefficiency, the candle the leg fell from.
_CHOCH_LEG = [
    bar(10, open_="124", close="116", high="125", low="114"),
    bar(11, open_="116", close="100", high="117", low="98"),
    bar(12, open_="96", close="92", high="96", low="91"),
    bar(13, open_="92", close="88", high="93", low="87"),  # closes under 90: CHoCH
]

# The same reversal with two separated gap runs, so the leg leaves a primary at its origin
# ([120, 125], bar 9) and a secondary on the pause candle ([103, 109], bar 13).
_TWO_ZONE_LEG = [
    bar(10, open_="124", close="118", high="125", low="117"),
    bar(11, open_="118", close="110", high="119", low="108"),
    bar(12, open_="110", close="106", high="112", low="105"),  # 117 > 112: run one's gap
    bar(13, open_="106", close="104", high="109", low="103"),  # the pause between runs
    bar(14, open_="104", close="94", high="105", low="92"),
    bar(15, open_="94", close="88", high="95", low="87"),  # 103 > 95: run two; CHoCH at 90
]

# The same fall with every three-bar window overlapping — no inefficiency anywhere, so the
# change of character marks nothing and the setup has nothing to trade.
_GAPLESS_LEG = [
    bar(10, open_="124", close="116", high="125", low="114"),
    bar(11, open_="116", close="108", high="121", low="106"),
    bar(12, open_="108", close="100", high="115", low="99"),
    bar(13, open_="100", close="93", high="107", low="92"),
    bar(14, open_="93", close="88", high="100", low="87"),  # CHoCH, empty-handed
]

_CHOCH_DOWN = StructureBreak(
    kind=StructureKind.CHOCH,
    trend=Trend.BEARISH,
    level=Decimal("90"),
    time=_at(15),
    # The qualifier never reads this; the bar the broken low came from is bar 9, the same
    # place the impulse started, which is what an anchor and an origin coinciding looks like.
    level_time=_at(9),
    origin=Decimal("125"),
    origin_time=_at(9),
)


def _supply(bottom: str, top: str, index: int, *, primary: bool) -> OrderBlock:
    return OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal(top),
        bottom=Decimal(bottom),
        time=_at(index),
        confirmed_at=_at(15),
        break_kind=StructureKind.CHOCH,
        primary=primary,
    )


def _ctx(
    *,
    break_: StructureBreak | None = None,
    marked: tuple[OrderBlock, ...] = (),
    zones: tuple[TrackedZone, ...] = (),
    stopped: OrderBlock | None = None,
    won: OrderBlock | None = None,
) -> SetupContext:
    return SetupContext(
        candle=bar(20, open_="100", close="100", high="101", low="99"),
        break_=break_,
        marked=marked,
        zones=zones,
        stopped=stopped,
        won=won,
    )


def test_choch_arms_the_zone_its_break_marked() -> None:
    """The setup end to end on the machinery: the change of character confirms on bar 13 and the
    sell goes straight onto the region the leg fell from — [120, 125], sold at its bottom edge
    with the stop a tenth of the width past its top. Nothing more is waited for: waiting for a
    break in the new trend's favour is the continuation setup, not this one.

    Every bar before the choch is silence, and that includes bar 9: the BOS marks two demand
    zones, and this setup is not interested in a continuation's leavings.
    """
    strategy = StructureStrategy(qualifier=ChochQualifier(), name="choch")
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *_CHOCH_LEG])

    assert all(bar_signals == [] for bar_signals in signals[:13])
    [signal] = signals[13]
    assert signal.kind is SignalKind.ENTRY
    assert signal.side is Side.SHORT
    assert (signal.limit_price, signal.stop_loss) == (Decimal("120"), Decimal("125.50"))
    assert signal.reason == "entry.choch"


def test_a_choch_without_inefficiency_offers_no_trade() -> None:
    """The author's rule verbatim: "sem ineficiência não tem trade". The same fall through the
    same anchor, but every three-bar window overlaps — no gap, no zone, and the change of
    character goes untraded rather than inventing a region to sell from."""
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=ChochQualifier()), [*_IMPULSE, *_GAPLESS_LEG]
    )

    assert all(bar_signals == [] for bar_signals in signals)


def test_the_choch_order_fills_on_the_pullback() -> None:
    """Through `run()` with the real broker: the sell rests at 120 and fills there when the
    pullback's wick reaches the zone three bars later — not at any bar's open, and not before
    price actually came back."""
    pullback = [
        bar(14, open_="88", close="100", high="101", low="87"),
        bar(15, open_="100", close="112", high="113", low="99"),
        bar(16, open_="112", close="116", high="121", low="111"),  # reaches the 120 edge
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *_CHOCH_LEG, *pullback],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=ChochQualifier()),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    [fill] = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert (fill.time, fill.price) == (_at(16), Decimal("120"))
    assert fill.order.side is Side.SHORT
    assert fill.order.stop_loss == Decimal("125.50")


def test_the_ladder_starts_at_the_zone_nearest_to_price() -> None:
    """With `allow_secondary` on and a leg that left two zones, the single live order hangs on
    the secondary — the pullback reaches it first; an order at the primary could only fill after
    price traversed the secondary whole. With the flag off the ladder is the primary alone."""
    on = _drive_from_bullish(
        StructureStrategy(qualifier=ChochQualifier(), allow_secondary=True),
        [*_IMPULSE, *_TWO_ZONE_LEG],
    )
    [signal] = on[15]
    assert (signal.limit_price, signal.stop_loss) == (Decimal("103"), Decimal("109.60"))

    off = _drive_from_bullish(
        StructureStrategy(qualifier=ChochQualifier()), [*_IMPULSE, *_TWO_ZONE_LEG]
    )
    [signal] = off[15]
    assert (signal.limit_price, signal.stop_loss) == (Decimal("120"), Decimal("125.50"))


def test_a_stopped_rung_hands_the_order_to_the_primary() -> None:
    """The author's sequence end to end: "secundária primeiro … e após, caso tenha stopado,
    ordem na primária."

    The sell fills at the secondary's edge (103) on bar 16; bar 17 runs through the stop at
    109.60, and the very same bar the machinery re-arms on the primary, whose order then fills at
    120 on bar 18. Two trades, one per region, worked toward the leg's origin.

    Watch what the zone does here, because it is the whole reason the ladder advances on the
    reported outcome rather than on the region's own state: bar 16 closes at 96, a full width
    clear of the zone below, so the secondary is **already mitigated while the trade is still
    open** — the healthy kind of mitigation, which reads as a winner. The stop on bar 17 leaves
    no mark at all (a mitigated zone is frozen, and `flippable` is long gone), so a ladder
    reading the zone would see a win here and end. Only `SetupContext.stopped` tells the truth.
    """
    after = [
        bar(16, open_="88", close="96", high="104", low="87"),  # fills at 103, and mitigates
        bar(17, open_="96", close="112", high="113", low="95"),  # stops at 109.60 — no new mark
        bar(18, open_="112", close="116", high="121", low="111"),  # fills the primary at 120
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *_TWO_ZONE_LEG, *after],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=ChochQualifier(), allow_secondary=True),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    entries = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert [(f.time, f.price) for f in entries] == [
        (_at(16), Decimal("103")),
        (_at(18), Decimal("120")),
    ]
    [first] = result.trades  # the second trade is still open when the series ends
    assert (first.entry_price, first.exit_price) == (Decimal("103"), Decimal("109.60"))


def test_a_winning_trade_leaves_no_order_on_the_primary() -> None:
    """The other half of the author's sequence: only the stop hands the order to the primary.

    With a 1R target the sell taken at 103 exits at 96.40 on its own bar — a win. Bars 17 and 18
    then hand the machine every temptation the stopped scenario had: the same reversal, the same
    pullback through 120. Nothing may be armed and nothing may fill — after a winner the leg is
    done ("colocou a ordem e ativou o trade, a região fica inválida"; and no order waits behind
    a winner).
    """
    after = [
        bar(16, open_="88", close="96", high="104", low="87"),  # fills at 103, target at 96.40
        bar(17, open_="96", close="112", high="113", low="95"),
        bar(18, open_="112", close="116", high="121", low="111"),  # 120 is reached; no order
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *_TWO_ZONE_LEG, *after],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=ChochQualifier(), allow_secondary=True),
        broker=BacktestBroker(
            instrument=AAPL, initial_capital=Decimal(10_000), take_profit_rr=Decimal(1)
        ),
        risk=FixedRisk(volume=Decimal(1)),
    )

    entries = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert [(f.time, f.price) for f in entries] == [(_at(16), Decimal("103"))]
    [trade] = result.trades
    assert (trade.exit_price, trade.reason) == (Decimal("96.40"), "tp")


def test_a_winning_rung_ends_the_ladder() -> None:
    """A trade that ends on the trader's terms ends the ladder — no order waits behind a winner.

    The event is the machinery's outcome report, deliberately not the zone's marks: both zones
    are still alive in this context, so nothing but `won` can be doing the work.
    """
    primary = _supply("120", "125", 9, primary=True)
    secondary = _supply("103", "109", 13, primary=False)
    alive = (TrackedZone(block=primary), TrackedZone(block=secondary))
    qualifier = ChochQualifier()

    named = qualifier.qualify(_ctx(break_=_CHOCH_DOWN, marked=(primary, secondary), zones=alive))
    assert named is secondary

    assert qualifier.qualify(_ctx(zones=alive, won=secondary)) is None
    # and the ladder is over, not deferred: the primary is never offered afterwards either
    assert qualifier.qualify(_ctx(zones=alive)) is None


def test_the_ladder_advances_on_the_stop_not_on_the_zones_marks() -> None:
    """The stop report alone moves the ladder. Both zones are alive in the context — the stopped
    rung's region often *is* already dead by the time the stop hits (a trade one width in profit
    mitigated it the healthy way before reversing), which is exactly why the marks cannot be the
    signal and the outcome has to be."""
    primary = _supply("120", "125", 9, primary=True)
    secondary = _supply("103", "109", 13, primary=False)
    alive = (TrackedZone(block=primary), TrackedZone(block=secondary))
    qualifier = ChochQualifier()

    qualifier.qualify(_ctx(break_=_CHOCH_DOWN, marked=(primary, secondary), zones=alive))
    assert qualifier.qualify(_ctx(zones=alive, stopped=secondary)) is primary


def test_only_the_current_rungs_stop_advances_the_ladder() -> None:
    """A stop reported for some other region leaves the ladder where it is.

    Unreachable while one position is held at a time — the trade that stops can only be the one
    the current rung opened — so this pins a guard the composite condition hides from coverage,
    against the phase where several setups (or several instruments) share a strategy and a
    stop from elsewhere would otherwise skip a rung that never traded.
    """
    primary = _supply("120", "125", 9, primary=True)
    secondary = _supply("103", "109", 13, primary=False)
    alive = (TrackedZone(block=primary), TrackedZone(block=secondary))
    qualifier = ChochQualifier()

    qualifier.qualify(_ctx(break_=_CHOCH_DOWN, marked=(primary, secondary), zones=alive))
    assert qualifier.qualify(_ctx(zones=alive, stopped=primary)) is secondary


def test_an_aged_out_rung_passes_to_the_next() -> None:
    """A rung the tracker dropped is dead of old age: nothing watches it, the machinery would
    refuse it, and the next zone toward the origin answers instead."""
    primary = _supply("120", "125", 9, primary=True)
    secondary = _supply("103", "109", 13, primary=False)
    qualifier = ChochQualifier()

    qualifier.qualify(
        _ctx(
            break_=_CHOCH_DOWN,
            marked=(primary, secondary),
            zones=(TrackedZone(block=primary), TrackedZone(block=secondary)),
        )
    )
    named = qualifier.qualify(_ctx(zones=(TrackedZone(block=primary),)))
    assert named is primary


def test_a_rung_that_died_without_a_trade_passes_to_the_next() -> None:
    """The other way a rung can die: still in the tracker, but no longer usable.

    Not the same case as the aged-out one above, and not reachable through the stop report
    either — a zone can be spent with no trade ever taken on it. The market gaps open past both
    the resting order and its stop: the broker discards the order without a fill (ADR-0014) and
    the same bar's close mitigates the region. No fill means no outcome to report, so the ladder
    has only the zone's own state to go on.

    Reading it wrong is silent. The qualifier would keep naming a region the machinery refuses
    on every bar, the ladder would never advance, and the primary — a trade the method takes —
    simply never happens. Nothing in the run says so; there is one fewer trade than there should
    be, which is the hardest kind of wrong to notice.
    """
    primary = _supply("120", "125", 9, primary=True)
    secondary = _supply("103", "109", 13, primary=False)
    qualifier = ChochQualifier()

    qualifier.qualify(
        _ctx(
            break_=_CHOCH_DOWN,
            marked=(primary, secondary),
            zones=(TrackedZone(block=primary), TrackedZone(block=secondary)),
        )
    )
    spent = (
        TrackedZone(block=primary),
        TrackedZone(block=secondary, mitigated=True),
    )
    assert qualifier.qualify(_ctx(zones=spent)) is primary


def test_the_ladder_survives_an_order_the_gap_discarded() -> None:
    """The same rule end to end, on the market event that produces it.

    The sell rests at 103 with its stop at 109.60. The next bar opens at 112 — above both — so
    the broker discards the order rather than filling it at a price that was never available
    (ADR-0014), and that bar's close mitigates the secondary a full width clear. No trade
    happened on that region, and none ever will; the primary at 120 takes over, and the pullback
    fills it.
    """
    gap = [
        bar(16, open_="112", close="112", high="113", low="111"),  # gaps past the order and stop
        bar(17, open_="112", close="116", high="121", low="111"),  # reaches the primary at 120
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *_TWO_ZONE_LEG, *gap],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=ChochQualifier(), allow_secondary=True),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    entries = [f for f in result.fills if f.order.intent is SignalKind.ENTRY]
    assert [(f.time, f.price) for f in entries] == [(_at(17), Decimal("120"))]


def test_a_new_choch_replaces_the_ladder() -> None:
    """A contrary change of character is not a special case — it is the setup reapplied. The old
    ladder is dropped wholesale (even when the new leg marked nothing) and the new leg's zone is
    simply the next trade, in the other direction."""
    primary = _supply("120", "125", 9, primary=True)
    qualifier = ChochQualifier()
    qualifier.qualify(
        _ctx(break_=_CHOCH_DOWN, marked=(primary,), zones=(TrackedZone(block=primary),))
    )

    demand = OrderBlock(
        kind=ZoneKind.DEMAND,
        top=Decimal("95"),
        bottom=Decimal("88"),
        time=_at(17),
        confirmed_at=_at(19),
        break_kind=StructureKind.CHOCH,
        primary=True,
    )
    contrary = StructureBreak(
        kind=StructureKind.CHOCH,
        trend=Trend.BULLISH,
        level=Decimal("125"),
        time=_at(19),
        level_time=_at(9),
        origin=Decimal("87"),
        origin_time=_at(17),
    )
    named = qualifier.qualify(
        _ctx(
            break_=contrary,
            marked=(demand,),
            zones=(TrackedZone(block=primary), TrackedZone(block=demand)),
        )
    )
    assert named is demand

    # An empty-handed choch also replaces: the old rung must not survive the turn of trend.
    empty = qualifier.qualify(_ctx(break_=contrary, marked=(), zones=(TrackedZone(block=primary),)))
    assert empty is None


def test_a_bos_in_the_same_direction_also_empties_the_ladder() -> None:
    """His rule 4, and the half that used to leak. Stated by him on 05/08/2026:

        "um choch de baixa cria a zona, se depois ele fizer um bos de baixa, a entrada de choch
        morre. mesma coisa um bos de baixa, se ele faz um segundo bos a entrada do 1 morreu e só
        pode entrar pelo segundo. e assim vai"

    Only the regions of the **most recent** break are live. The continuation setup always worked
    this way — a new BOS replaces its whole ladder — but this qualifier only ever reacted to
    another CHoCH, so any number of BOS could confirm while an order still rested on a region the
    structure had left behind. Measured over 3480 AAPL H1 candles: 21 of 34 changes of character
    were followed by at least one BOS before the next one, 53 breaks in all, up to 7 stacked on a
    single choch.

    The region here is handed back **alive and present** in `context.zones`, which is the whole
    difference between this test and a vacuous one. The ladder walk already skips a rung it
    cannot find or that is no longer usable, so a scenario that withheld the zone would return
    `None` whether the ladder was emptied or not — right answer, wrong reason, and the rule would
    still be deletable. Here the only thing that can silence the qualifier is the BOS itself.

    A BOS leaves regions of its own, but they belong to the continuation setup, not to this one.
    That is why the ladder empties rather than refills.
    """
    primary = _supply("120", "125", 9, primary=True)
    alive = (TrackedZone(block=primary),)
    qualifier = ChochQualifier()
    assert qualifier.qualify(_ctx(break_=_CHOCH_DOWN, marked=(primary,), zones=alive)) is primary

    # The structure moves on in the same direction. Nothing happened to the region itself.
    assert alive[0].usable
    assert qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(), zones=alive)) is None
    # And it stays dead to this setup on later bars, not just on the bar the BOS confirmed.
    assert qualifier.qualify(_ctx(zones=alive)) is None


def test_an_outcome_and_a_new_choch_on_one_bar_settle_in_order() -> None:
    """The outcome belongs to the regime that produced the trade. A win reported on the very bar
    a contrary choch confirms must not erase the *new* leg's ladder — the old ladder is what the
    win ends, and the new zone is named as if the bar were clean."""
    old = _supply("120", "125", 9, primary=True)
    demand = OrderBlock(
        kind=ZoneKind.DEMAND,
        top=Decimal("95"),
        bottom=Decimal("88"),
        time=_at(17),
        confirmed_at=_at(19),
        break_kind=StructureKind.CHOCH,
        primary=True,
    )
    contrary = StructureBreak(
        kind=StructureKind.CHOCH,
        trend=Trend.BULLISH,
        level=Decimal("125"),
        time=_at(19),
        level_time=_at(9),
        origin=Decimal("87"),
        origin_time=_at(17),
    )
    qualifier = ChochQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN, marked=(old,), zones=(TrackedZone(block=old),)))

    named = qualifier.qualify(
        _ctx(
            break_=contrary,
            marked=(demand,),
            zones=(TrackedZone(block=old), TrackedZone(block=demand)),
            won=old,
        )
    )
    assert named is demand


# --------------------------------------------------------------------------- #
# The continuation setup                                                        #
# --------------------------------------------------------------------------- #

# Measured, not invented (probe against the real detectors). After the impulse's bootstrap BOS up
# (bar 9) and the fall through the 90 anchor to a change of character (bar 13, supply [120, 125]),
# price corrects up two bars and breaks structure downward again on bar 17 — a BOS *in favour of*
# the new bearish trend, whose leg leaves the supply zone [96, 100]. That zone is the continuation
# setup's, and the two breaks before it are not.
_CONT_LEG = [
    bar(14, open_="88", close="95", high="96", low="88"),  # correction up 1
    bar(15, open_="96", close="99", high="100", low="96"),  # correction up 2 -> armed
    bar(16, open_="99", close="92", high="100", low="91"),
    bar(
        17, open_="91", close="82", high="92", low="80"
    ),  # close 82 < 87 -> BOS down; zone [96,100]
]

_BOS_DOWN = StructureBreak(
    kind=StructureKind.BOS,
    trend=Trend.BEARISH,
    level=Decimal("87"),
    time=_at(17),
    level_time=_at(15),
    origin=Decimal("100"),
    origin_time=_at(15),
)


def _bos_zone(bottom: str, top: str, index: int, *, primary: bool) -> OrderBlock:
    """A supply zone left by a continuation's favourable break (`break_kind` BOS, not CHoCH)."""
    return OrderBlock(
        kind=ZoneKind.SUPPLY,
        top=Decimal(top),
        bottom=Decimal(bottom),
        time=_at(index),
        confirmed_at=_at(17),
        break_kind=StructureKind.BOS,
        primary=primary,
    )


def test_continuation_arms_the_bos_zone_after_a_change_of_character() -> None:
    """The setup end to end on the real detectors: arm, drop on the turn, re-arm the other way.

    The scenario reads differently than it used to, and the difference is the transcription being
    honest. Bar 9 used to be dismissed as "a bootstrap BOS — a trend with no reversal behind it",
    but a trend with nothing behind it is precisely what a fresh machine can no longer produce:
    the uptrend bar 9 continues was opened by a change of character on bar -1, so continuation is
    eligible and **must** arm there. Refusing would be the bug.

    So the sequence has three acts, and each is a rule of the setup:

    * **bar 9** — a break in favour of the standing uptrend arms the long on the demand zone
      [90, 100] its leg left, stop at 89;
    * **bar 13** — the change of character turns the bias, and a turn drops the standing ladder,
      so the resting order is withdrawn. The choch's own region belongs to the choch setup, not
      to this one, so nothing is armed to replace it;
    * **bar 17** — a break in favour of the *new* trend arms the sell on [96, 100], stop a tenth
      of the width past the top.

    That last line is the one this test was written for and it is unchanged: 96 and 100.40.
    """
    strategy = StructureStrategy(qualifier=ContinuationQualifier(), name="continuation")
    signals = _drive_from_bullish(strategy, [*_IMPULSE, *_CHOCH_LEG, *_CONT_LEG])

    [armed] = signals[9]
    assert armed.kind is SignalKind.ENTRY
    assert armed.side is Side.LONG
    assert (armed.limit_price, armed.stop_loss) == (Decimal("100"), Decimal("89"))

    # The order comes back on bar 11, and under his rule for a different reason than it used to:
    # bar 11 dips to 98, through the region's top of 100, so price *took* the region — this is
    # where a broker would have filled the order. The turn on bar 13 then has nothing left to
    # drop. That the turn drops a ladder still standing is its own scenario,
    # `test_a_turn_drops_the_standing_continuation_ladder`, where price stays clear of the region.
    [dropped] = signals[11]
    assert dropped.kind is SignalKind.CANCEL
    assert dropped.client_id == armed.client_id  # the very order bar 9 placed

    # And nothing from there to the break that confirms the new trend: the choch's own region is
    # not continuation's to trade.
    assert all(bar_signals == [] for bar_signals in signals[12:17])

    [signal] = signals[17]
    assert signal.kind is SignalKind.ENTRY
    assert signal.side is Side.SHORT
    assert (signal.limit_price, signal.stop_loss) == (Decimal("96"), Decimal("100.40"))
    assert signal.reason == "entry.continuation"


def test_continuation_ignores_a_break_with_no_change_of_character_behind_it() -> None:
    """Continuation needs the turn first, so it stays silent through a break no choch opened.

    The scenario had to be rebuilt, and the rebuild is the more honest one. It used to run the
    bullish impulse on a fresh machine and call that "a bootstrap BOS up — a trend appearing from
    nothing". The transcription makes that shape impossible: a fresh machine starts at the
    indicator's `DIR = -1`, so a rising impulse confirms nothing at all, and the test would have
    passed on a scenario where continuation was never offered anything to refuse.

    The mirrored impulse is the real instance of the case, and there is exactly one: the **first
    bearish BOS of a fresh series** is the only break that can have no change of character behind
    it, because every later break is downstream of one.

    And the silence is continuation *declining*, not the machinery having nothing to offer:
    `test_the_geometry_mirrors_for_a_supply_zone` drives this very stream on a fresh machine under
    `_Marked` and gets an entry on bar 9. The zones are there; this qualifier refuses them.
    """
    signals = _drive(StructureStrategy(qualifier=ContinuationQualifier()), _mirror(_IMPULSE))

    assert all(bar_signals == [] for bar_signals in signals)


def test_a_change_of_character_opens_continuation_but_does_not_arm_it() -> None:
    """The choch is what makes continuation eligible, yet it is not itself a continuation entry:
    the region it leaves belongs to the choch setup. The bar it confirms names nothing here, even
    though it marked a zone."""
    qualifier = ContinuationQualifier()
    zone = _supply("120", "125", 9, primary=True)

    named = qualifier.qualify(
        _ctx(break_=_CHOCH_DOWN, marked=(zone,), zones=(TrackedZone(block=zone),))
    )

    assert named is None


def test_a_favourable_break_after_the_turn_arms_its_zone() -> None:
    """The turn, then a break confirming it: a BOS after a change of character arms the region its
    leg left."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))  # the turn — now eligible

    zone = _bos_zone("96", "100", 15, primary=True)
    named = qualifier.qualify(
        _ctx(break_=_BOS_DOWN, marked=(zone,), zones=(TrackedZone(block=zone),))
    )

    assert named is zone


def test_a_break_before_any_turn_arms_nothing() -> None:
    """A bootstrap BOS — a break with no change of character before it — is an old trend running
    on, not a continuation. It arms nothing even when its leg left a zone."""
    qualifier = ContinuationQualifier()
    zone = _bos_zone("96", "100", 15, primary=True)

    named = qualifier.qualify(
        _ctx(break_=_BOS_DOWN, marked=(zone,), zones=(TrackedZone(block=zone),))
    )

    assert named is None


def test_a_continuation_break_without_inefficiency_offers_no_trade() -> None:
    """ "sem ineficiência não tem trade" holds for the continuation break too: a favourable BOS
    that left no zone names nothing, rather than inventing a region."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    assert qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(), zones=())) is None


def test_the_continuation_ladder_meets_the_nearest_zone_first() -> None:
    """The choch's ladder, reused. With two zones the pullback reaches the last-marked (nearest)
    first, so the order hangs there; `marked` arrives origin-first, so the ladder is its reverse."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    primary = _bos_zone("96", "100", 15, primary=True)
    secondary = _bos_zone("84", "88", 19, primary=False)
    named = qualifier.qualify(
        _ctx(
            break_=_BOS_DOWN,
            marked=(primary, secondary),
            zones=(TrackedZone(block=primary), TrackedZone(block=secondary)),
        )
    )

    assert named is secondary


def test_a_stopped_continuation_rung_hands_the_order_to_the_next_zone() -> None:
    """The ladder advances on the trade's outcome, exactly as the choch's does: the rung whose
    trade the stop took out passes the order to the next zone toward the origin."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    primary = _bos_zone("96", "100", 15, primary=True)
    secondary = _bos_zone("84", "88", 19, primary=False)
    zones = (TrackedZone(block=primary), TrackedZone(block=secondary))
    assert (
        qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(primary, secondary), zones=zones))
        is secondary
    )

    # The secondary's trade is stopped out: the order moves to the primary at the leg's origin.
    assert qualifier.qualify(_ctx(zones=zones, stopped=secondary)) is primary


def test_a_winning_continuation_rung_ends_the_ladder() -> None:
    """No order waits behind a winner — not even at the primary the ladder had yet to reach."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    primary = _bos_zone("96", "100", 15, primary=True)
    secondary = _bos_zone("84", "88", 19, primary=False)
    zones = (TrackedZone(block=primary), TrackedZone(block=secondary))
    assert (
        qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(primary, secondary), zones=zones))
        is secondary
    )

    assert qualifier.qualify(_ctx(zones=zones, won=secondary)) is None


def test_max_bos_one_trades_only_the_first_break_after_a_turn() -> None:
    """`max_bos=1` is the strict reading — the one BOS that confirms the new trend, nothing after
    it until the trend turns again. The second favourable break is ignored and the first leg's
    ladder is left standing."""
    qualifier = ContinuationQualifier(max_bos=1)
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    first = _bos_zone("96", "100", 15, primary=True)
    second = _bos_zone("70", "74", 21, primary=True)
    assert (
        qualifier.qualify(
            _ctx(break_=_BOS_DOWN, marked=(first,), zones=(TrackedZone(block=first),))
        )
        is first
    )

    both = (TrackedZone(block=first), TrackedZone(block=second))
    assert qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(second,), zones=both)) is first


def test_by_default_every_favourable_break_re_arms() -> None:
    """The default (`max_bos=None`) trades the pullback into each new leg: a second favourable
    break replaces the ladder with its own zone."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    first = _bos_zone("96", "100", 15, primary=True)
    second = _bos_zone("70", "74", 21, primary=True)
    assert (
        qualifier.qualify(
            _ctx(break_=_BOS_DOWN, marked=(first,), zones=(TrackedZone(block=first),))
        )
        is first
    )

    both = (TrackedZone(block=first), TrackedZone(block=second))
    assert qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(second,), zones=both)) is second


def test_a_zoneless_break_does_not_spend_a_cap_slot() -> None:
    """The cap counts trades, not breaks: a favourable BOS that left no inefficiency is a
    non-event, so with `max_bos=1` a zoneless break does not use up the one allowed trade."""
    qualifier = ContinuationQualifier(max_bos=1)
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    # A favourable break that marked nothing — it must not burn the slot.
    assert qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(), zones=())) is None

    zone = _bos_zone("96", "100", 15, primary=True)
    named = qualifier.qualify(
        _ctx(break_=_BOS_DOWN, marked=(zone,), zones=(TrackedZone(block=zone),))
    )
    assert named is zone


def test_a_new_turn_re_opens_the_cap() -> None:
    """The count resets on every change of character: after `max_bos=1` has spent its trade, a
    fresh choch drops the old ladder and lets continuation arm again on the next favourable
    break."""
    qualifier = ContinuationQualifier(max_bos=1)
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    first = _bos_zone("96", "100", 15, primary=True)
    assert (
        qualifier.qualify(
            _ctx(break_=_BOS_DOWN, marked=(first,), zones=(TrackedZone(block=first),))
        )
        is first
    )

    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))  # the count re-opens, the old ladder is dropped
    second = _bos_zone("70", "74", 21, primary=True)
    named = qualifier.qualify(
        _ctx(break_=_BOS_DOWN, marked=(second,), zones=(TrackedZone(block=second),))
    )
    assert named is second


def test_a_turn_drops_the_standing_continuation_ladder() -> None:
    """A change of character came back through the region the continuation was resting in. The old
    ladder is dropped wholesale; a bare choch arms nothing of its own here."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    zone = _bos_zone("96", "100", 15, primary=True)
    zones = (TrackedZone(block=zone),)
    assert qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(zone,), zones=zones)) is zone

    assert qualifier.qualify(_ctx(break_=_CHOCH_DOWN, zones=zones)) is None


def test_a_continuation_outcome_and_a_fresh_break_on_one_bar_settle_in_order() -> None:
    """A win reported on the very bar a fresh favourable break confirms belongs to the old leg: it
    ends the old ladder, and the new leg's zone is named as if the bar were clean."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))

    first = _bos_zone("96", "100", 15, primary=True)
    second = _bos_zone("70", "74", 21, primary=True)
    assert (
        qualifier.qualify(
            _ctx(break_=_BOS_DOWN, marked=(first,), zones=(TrackedZone(block=first),))
        )
        is first
    )

    both = (TrackedZone(block=first), TrackedZone(block=second))
    named = qualifier.qualify(_ctx(break_=_BOS_DOWN, marked=(second,), zones=both, won=first))
    assert named is second


def test_a_continuation_rung_dropped_by_the_tracker_passes_to_the_next() -> None:
    """One half of the dead-rung skip: a rung the tracker no longer holds at all — aged out of the
    window — is skipped, and the next zone toward the origin answers."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))
    primary = _bos_zone("96", "100", 15, primary=True)
    secondary = _bos_zone("84", "88", 19, primary=False)
    qualifier.qualify(
        _ctx(
            break_=_BOS_DOWN,
            marked=(primary, secondary),
            zones=(TrackedZone(block=primary), TrackedZone(block=secondary)),
        )
    )

    # The nearest rung is gone from the tracker; only the primary remains.
    assert qualifier.qualify(_ctx(zones=(TrackedZone(block=primary),))) is primary


def test_a_mitigated_continuation_rung_passes_to_the_next() -> None:
    """The other half: a rung still tracked but no longer usable — mitigated with no trade of its
    own taken — is skipped just the same."""
    qualifier = ContinuationQualifier()
    qualifier.qualify(_ctx(break_=_CHOCH_DOWN))
    primary = _bos_zone("96", "100", 15, primary=True)
    secondary = _bos_zone("84", "88", 19, primary=False)
    qualifier.qualify(
        _ctx(
            break_=_BOS_DOWN,
            marked=(primary, secondary),
            zones=(TrackedZone(block=primary), TrackedZone(block=secondary)),
        )
    )

    zones = (TrackedZone(block=primary), TrackedZone(block=secondary, mitigated=True))
    assert qualifier.qualify(_ctx(zones=zones)) is primary


def test_max_bos_below_one_is_refused() -> None:
    """A cap of zero would mean "trade nothing", which is not a continuation setup at all — it is
    almost always a mistake, so it is refused rather than silently armed to never fire."""
    with pytest.raises(ValueError, match="count of breaks"):
        ContinuationQualifier(max_bos=0)


# --------------------------------------------------------------------------- #
# The conduction: structure moves the stop, and none of it is an exit           #
# --------------------------------------------------------------------------- #


def _held(*, entry: str, stop: str, side: Side = Side.LONG) -> Position:
    """An open position with known levels, so what the conduction computes can be measured."""
    return Position(
        symbol=AAPL.symbol,
        side=side,
        volume=Decimal(1),
        entry_price=Decimal(entry),
        entry_time=_at(0),
        stop_loss=Decimal(stop),
        initial_stop_loss=Decimal(stop),
    )


def _trails(signals: list[list[Signal]]) -> list[list[Decimal | None]]:
    """The stop levels asked for on each bar. `None` is kept rather than filtered: a
    `MODIFY_STOP` carrying no level is a bug that must not read as "no signal"."""
    return [
        [s.stop_loss for s in per_bar if s.kind is SignalKind.MODIFY_STOP] for per_bar in signals
    ]


# Two more bars of trend after the impulse: a pair of corrections, then a close above the 125 top.
# `MarketStructure` confirms a second bullish BOS on bar 12, whose origin is bar 11's low of 117.
_SECOND_LEG = [
    bar(10, open_="123", close="123", high="124", low="119"),  # correction 1
    bar(11, open_="121", close="121", high="122", low="117"),  # correction 2 -> armed
    bar(12, open_="127", close="127", high="128", low="122"),  # close 127 > 125 -> BOS up
]


def test_the_first_break_in_favour_brings_the_stop_to_the_entry_price() -> None:
    """The author's rule, first half: breakeven at the first break of structure in our favour.

    Bar 9 closes 124 above the 123 top and confirms a bullish BOS — the open long's first break.
    The stop goes to 100, the entry. The multiple-of-risk rule stays out of it on purpose: risk
    is 15, so its line is at 130 and bar 9's high of 125 does not reach it. One rule at a time.
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()),
        _IMPULSE,
        position_on=frozenset({9}),
        held=_held(entry="100", stop="85"),
    )

    assert _trails(signals)[9] == [Decimal("100")]


def test_every_break_after_the_first_puts_the_stop_at_the_leg_origin() -> None:
    """The second half, and the reason `origin` is the field that matters.

    Bar 12 confirms a second bullish BOS. Its `level` is 125 — the price the move went *through* —
    and its `origin` is 117, the low the move came *from*. The stop goes to 117: behind the
    structure being ridden, at the price whose loss would turn the trend, rather than inside the
    move where an ordinary pullback would take it out.

    Bars 10 and 11 are the corrections that armed the break, and they ask for nothing. Between
    breaks the stop does not move — there is no bar-by-bar trailing in this method either.
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()),
        [*_IMPULSE, *_SECOND_LEG],
        position_on=frozenset({9, 10, 11, 12}),
        held=_held(entry="100", stop="85"),
    )

    assert _trails(signals)[9] == [Decimal("100")]
    assert _trails(signals)[10] == []
    assert _trails(signals)[11] == []
    assert _trails(signals)[12] == [Decimal("117")]


def test_a_break_against_the_open_trade_moves_nothing() -> None:
    """Only breaks in the trade's own direction conduct it. Bar 9's BOS is bullish and this
    position is short, so it is not this trade's news. Its risk line is out of reach too — entry
    100 against a stop of 115 puts twice the risk at 70, and the bar's low is 120."""
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()),
        _IMPULSE,
        position_on=frozenset({9}),
        held=_held(entry="100", stop="115", side=Side.SHORT),
    )

    assert _trails(signals)[9] == []


def test_touching_the_multiple_of_risk_brings_the_stop_to_the_entry_price() -> None:
    """The rule that does not need structure at all, and the exact boundary of its trigger.

    Entry 100 against a stop of 91 risks 9, so twice the risk sits at **118** — bar 8's high,
    exactly. Reaching the line is reaching it. Bar 8 confirms no break of structure, so the level
    asked for here can only have come from the risk rule.
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()),
        _IMPULSE,
        position_on=frozenset({8}),
        held=_held(entry="100", stop="91"),
    )

    assert _trails(signals)[8] == [Decimal("100")]


def test_switching_breakeven_off_leaves_structure_conducting_alone() -> None:
    """`None` is a setting, not a missing value: the risk rule is gone and structure is not.

    The same bar 8 that reached twice the risk above now asks for nothing, while bar 9's break
    still moves the stop. Being able to run the method without the risk rule is what makes "does
    taking this trade to breakeven early pay for itself" a question a backtest can answer.
    """
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked(), breakeven_at_r=None),
        _IMPULSE,
        position_on=frozenset({8, 9}),
        held=_held(entry="100", stop="91"),
    )

    assert _trails(signals)[8] == []
    assert _trails(signals)[9] == [Decimal("100")]


def test_the_bar_that_filled_does_not_credit_its_own_excursion() -> None:
    """A limit fills in the middle of a bar the trade did not live through, and the favourable
    extreme of that bar is usually **before** the fill.

    The ladder scenario is the measurement. The sell fills at 103 on bar 16, whose low is 87 —
    past its own twice-the-risk line of 89.80 (entry 103, stop 109.60). The order rests where
    price has to come *back* to, so the market fell to 87 while the account was flat and only
    then rose through 103 to fill. Crediting that print would arm breakeven on the fill bar, and
    bar 17 would take the trade out at 103 instead of 109.60 — a full-R loser turned into a
    scratch, in a backtest where no number looks wrong and nothing reads the future.

    So the trade must still lose its full R. A regression test with a price attached.
    """
    after = [
        bar(16, open_="88", close="96", high="104", low="87"),  # fills at 103; its low is pre-fill
        bar(17, open_="96", close="112", high="113", low="95"),  # takes the untouched stop
        bar(18, open_="112", close="116", high="121", low="111"),
    ]
    result = run(
        candles=[*BULLISH_START, *_IMPULSE, *_TWO_ZONE_LEG, *after],
        timeframe=HOUR,
        instrument=AAPL,
        strategy=StructureStrategy(qualifier=ChochQualifier(), allow_secondary=True),
        broker=BacktestBroker(instrument=AAPL, initial_capital=Decimal(10_000)),
        risk=FixedRisk(volume=Decimal(1)),
    )

    [first] = result.trades
    assert (first.entry_price, first.exit_price) == (Decimal("103"), Decimal("109.60"))
    assert first.r_multiple == Decimal(-1)


def test_a_short_is_conducted_by_the_mirror_of_the_same_rules() -> None:
    """The bearish mirror of the impulse: bar 9 confirms a bearish BOS, and the open short's
    first break in favour brings its stop to the entry price."""
    signals = _drive(
        StructureStrategy(qualifier=_Marked()),
        _mirror(_IMPULSE),
        position_on=frozenset({9}),
        held=_held(entry="100", stop="115", side=Side.SHORT),
    )

    assert _trails(signals)[9] == [Decimal("100")]


def test_the_breakeven_multiple_must_be_positive() -> None:
    """Zero is refused rather than read as "off" — `None` is how the rule is switched off. A
    multiple of zero would put the trigger on the entry price itself, arming breakeven on the
    very bar that opened the trade."""
    with pytest.raises(ValueError, match="breakeven R multiple"):
        StructureStrategy(qualifier=_Marked(), breakeven_at_r=Decimal(0))


# A third leg after `_SECOND_LEG`, so one instance can see a break of structure, then a *second*
# trade, then another break. Bar 12's gap over bar 10's high is what makes it a leg with an
# inefficiency; bar 15 breaks the 131 top it left, and its origin is 123.
_THIRD_LEG = [
    bar(13, open_="129", close="129", high="130", low="125"),  # correction 1
    bar(14, open_="127", close="127", high="128", low="123"),  # correction 2
    bar(15, open_="133", close="133", high="134", low="132"),  # close 133 > 131 -> BOS up
]

_GAPPED_SECOND_LEG = [
    bar(10, open_="123", close="123", high="124", low="119"),  # correction 1
    bar(11, open_="121", close="121", high="122", low="117"),  # correction 2
    bar(12, open_="128", close="130", high="131", low="126"),  # gaps over 124 -> BOS up
]


def test_the_count_of_breaks_belongs_to_the_trade_not_to_the_strategy() -> None:
    """A second trade's first break is *its* first, and it gets breakeven — not the origin.

    Bar 9 breaks structure with one trade open, so that trade has had its first. Bar 15 breaks
    structure again with a **different** trade open, and it must be read as that trade's first:
    the stop goes to 100, its entry. A count that survived the change of trade would send bar 15
    down the second-break branch and put the stop at the leg's origin, **123** — and on a long
    entered at 100 that is not merely a different number, it is a stop the trade has already
    passed, so `tighten` accepts it and the position goes on carrying risk the method says was
    given up two bars ago.

    The two trades are told apart by `entry_time`, which is why this cannot depend on the fill
    ever having been announced. Twice the risk stays out of it: the stop is 80, so that line sits
    at 140 and nothing in this stream reaches it.
    """
    stream = [*_IMPULSE, *_GAPPED_SECOND_LEG, *_THIRD_LEG]
    first = _held(entry="100", stop="80")
    second = Position(
        symbol=AAPL.symbol,
        side=Side.LONG,
        volume=Decimal(1),
        entry_price=Decimal(100),
        entry_time=stream[13].time,  # a different trade: same levels, later entry
        stop_loss=Decimal(80),
        initial_stop_loss=Decimal(80),
    )
    signals = _drive_from_bullish(
        StructureStrategy(qualifier=_Marked()),
        stream,
        held_by_bar={9: first, 13: second, 14: second, 15: second},
    )

    assert _trails(signals)[9] == [Decimal("100")]  # trade one's first break
    assert _trails(signals)[13] == []
    assert _trails(signals)[14] == []
    assert _trails(signals)[15] == [Decimal("100")]  # trade two's first break, not the origin


# The mirrored impulse breaks structure downward on bar 9; price then climbs back through the
# anchor that break left, which confirms a bullish CHoCH on bar 11 (level 110).
_CLIMB_BACK = [
    bar(10, open_="80", close="95", high="98", low="78"),
    bar(11, open_="95", close="115", high="118", low="94"),
]


def test_a_change_of_character_in_our_favour_is_not_a_break_in_our_favour() -> None:
    """The author's rule names the **BOS**, and reading it literally is a decision with teeth.

    Bar 11 confirms a bullish CHoCH while a long is open — structurally good news, pointing our
    way. It moves nothing. A version that counted it would not merely act one event early: it
    would spend the *first break* slot on the change of character, so the BOS that actually
    follows would take the second-break branch and hand the stop a leg origin instead of the
    entry price. Every rung of that trade's stop after it would be one step wrong.

    Twice the risk is out of reach here as well (entry 100 against a stop of 85 puts it at 130,
    and the bar's high is 118), so silence is the only correct answer.
    """
    signals = _drive(
        StructureStrategy(qualifier=_Marked()),
        [*_mirror(_IMPULSE), *_CLIMB_BACK],
        position_on=frozenset({10, 11}),
        held=_held(entry="100", stop="85"),
    )

    assert _trails(signals)[11] == []


# --------------------------------------------------------------------------- #
# An order that never reached the book                                          #
# --------------------------------------------------------------------------- #


def _drive_with_a_refusal(refused_bar: int) -> list[list[Signal]]:
    """The measured EURUSD window, with the entry armed on `refused_bar` refused by the broker.

    ⚠️ **The refusal is delivered on the bar *after* it happened**, because that is the only
    place the loop can put it (`Context.refusals`) — the context for a bar is built before the
    strategy speaks, and there is nothing to refuse until it has. Reproducing that here rather
    than handing the refusal to its own bar is the difference between testing the rule and
    testing a convenience.

    ⚠️ **The real window, not a synthetic one.** Probed at all fifteen cuts of the golden
    `ma_cross` document, that strategy arms nothing at all — it enters at market — so a scenario
    built on it could not distinguish an implementation that re-offered from one that did not.
    """
    phase = build_setup({"type": "structure_choch"})
    account = AccountState(balance=Decimal("10000"), equity=Decimal("10000"))
    out: list[list[Signal]] = []
    pending: tuple[Refusal, ...] = ()

    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(arms_a_resting_limit()):
            signals = list(
                phase.on_bar(
                    Context(
                        candle=candle,
                        instrument=EURUSD,
                        account=account,
                        refusals=pending,
                    )
                )
            )
            out.append(signals)
            pending = ()
            if index == refused_bar:
                pending = tuple(
                    Refusal(
                        client_id=signal.client_id,
                        intent=signal.kind,
                        refused_by=RefusedBy.BROKER,
                        reason=signal.reason,
                        detail="the venue would not take it",
                    )
                    for signal in signals
                    if signal.kind is SignalKind.ENTRY
                )
    return out


def test_a_zone_whose_order_was_refused_may_be_offered_again() -> None:
    """**The ghost's third door, closed.** A refused order is not resting anywhere, so the zone
    it was waiting at was never traded — and the author's rule draws the line at the fill.

    Measured on `arms_a_resting_limit` (`scratchpad/probe_reoffer.py`): the phase arms
    `demand-20240103T0200-1` on bar 61, and with the refusal observed it arms the same region
    again on bar 62 under `-2`.

    ⚠️ **The new name is the load-bearing half.** Re-offering under the old one would be refused
    a second time as a duplicate — by a broker answering an entirely different question — and
    the retry would never converge. The name changes because the region goes back through
    `_may_arm`, which is the same door a withdrawal leaves it at.
    """
    signals = _drive_with_a_refusal(refused_bar=61)

    [armed] = signals[61]
    [re_armed] = signals[62]

    assert armed.kind is SignalKind.ENTRY
    assert armed.client_id == "demand-20240103T0200-1"
    assert re_armed.kind is SignalKind.ENTRY, "the refused zone was never offered again"
    assert re_armed.client_id == "demand-20240103T0200-2", "it was re-offered under the old name"
    assert re_armed.limit_price == armed.limit_price, "a different level; this is another trade"


def test_a_refused_order_is_forgotten_rather_than_withdrawn() -> None:
    """⚠️ **A refusal is not a cancellation, and emitting one is the bug seen from the far side.**
    `_withdraw` takes back an order that is resting; a refused order never reached a book, so a
    cancel for it is a round trip the venue can only answer "no" to — under a name it has never
    heard of.

    Measured on the same window: today the phase carries the refused name from bar 61 to bar
    109 and then sends exactly that phantom cancel. With the refusal observed, bar 62 emits an
    entry and **nothing else**, and the cancel on 109 names the order that actually exists.
    """
    signals = _drive_with_a_refusal(refused_bar=61)

    kinds = [signal.kind for signal in signals[62]]
    assert SignalKind.CANCEL not in kinds, "a cancel was sent for an order the venue never accepted"

    [cancel] = signals[109]
    assert cancel.kind is SignalKind.CANCEL
    assert cancel.client_id == "demand-20240103T0200-2", (
        "the withdrawal still names the refused order rather than the one that replaced it"
    )


def test_a_refusal_naming_an_order_it_no_longer_holds_is_ignored() -> None:
    """⚠️ **Written because the obvious implementation passed every other test here.** A version
    that forgot its armed order on *any* refusal — rather than on one naming that order —
    survived the two above, because in both of them the only refusal in flight is the one for
    the order being held. The scenario could not tell the two apart, so it proved neither.

    A refusal for a name the phase is no longer holding is not news: it has already been acted
    upon, and the order the phase *is* holding is a different one that may be resting perfectly
    well. Forgetting it would place a second limit on the same zone — under a third name — while
    the second one sits in the book.

    The stale refusal here names `-1`, the order refused on bar 61, and is delivered on bar 63,
    by which time bar 62 has already re-armed the region as `-2`.
    """
    phase = build_setup({"type": "structure_choch"})
    account = AccountState(balance=Decimal("10000"), equity=Decimal("10000"))
    stale = Refusal(
        client_id="demand-20240103T0200-1",
        intent=SignalKind.ENTRY,
        refused_by=RefusedBy.BROKER,
        reason="entry.choch",
        detail="a refusal for an order that was dealt with two bars ago",
    )

    out: list[list[Signal]] = []
    pending: tuple[Refusal, ...] = ()
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(arms_a_resting_limit()):
            out.append(
                list(
                    phase.on_bar(
                        Context(
                            candle=candle,
                            instrument=EURUSD,
                            account=account,
                            refusals=pending,
                        )
                    )
                )
            )
            # Bar 61's order is refused; bar 63 is handed that same refusal a second time.
            pending = (stale,) if index in (61, 62) else ()

    assert out[62], "the fixture is broken: bar 62 did not re-arm, so bar 63 protects nothing"
    assert out[62][0].client_id == "demand-20240103T0200-2", "bar 62 armed something unexpected"
    assert out[63] == [], (
        "the stale refusal forgot an order that was resting; the zone would now carry two limits"
    )


def test_a_refusal_born_in_the_loop_reaches_the_setup_that_armed_the_order() -> None:
    """**The seam, end to end, with nothing fabricated.**

    ⚠️ **Written because the two tests above cannot prove this and looked like they could.**
    They build the `Refusal` by hand from the signal, so they exercise `_observe_refusal`
    against a refusal the test itself named — which is true of the object and says nothing about
    the loop that has to produce it. Measured: setting `client_id=None` at all four gates in
    `loop.py` left every test in this file and in `test_loop.py` green, and put the ghost back
    in full — the phase carried the dead name for 48 bars and sent a cancel for an order the
    venue never accepted.

    So here the loop is real, the strategy is real, the broker refuses for real, and the only
    thing standing in for anything is *which* order gets refused.

    The market is EURUSD, 2024-01-03: a bullish CHoCH confirms and a demand zone is armed at
    1.03053. The venue will not take it — the shape of AutoTrading being off, which is the case
    the transport in PR-304-B-D-2 makes reachable.
    """

    class RefusesTheFirstOrder(BacktestBroker):
        """A real broker that declines exactly once, then behaves.

        Subclassed rather than faked: the refusal has to travel the same path a real one does,
        through `_step`, and a double large enough to do that would be a second broker.
        """

        def __init__(self) -> None:
            super().__init__(instrument=EURUSD, initial_capital=Decimal("10000"))
            self.refused: list[str | None] = []

        def submit(self, order: OrderRequest) -> OrderResult:
            if not self.refused:
                self.refused.append(order.client_id)
                return OrderResult(order=order, accepted=False, reason="the venue would not")
            return super().submit(order)

    broker = RefusesTheFirstOrder()
    for _outcome in iter_run(
        candles=iter(arms_a_resting_limit()),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=build_setup({"type": "structure_choch"}),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal("1")),
    ):
        pass

    assert broker.refused == ["demand-20240103T0200-1"], (
        "the broker refused something other than the armed limit; the scenario has drifted"
    )
    submitted = [order.client_id for order in broker.submitted]
    assert "demand-20240103T0200-2" in submitted, (
        "the refused zone was never offered again — the refusal did not reach the setup, or "
        "reached it without the name of the order it refused"
    )
    assert "demand-20240103T0200-1" not in submitted, "the refused order counted as submitted"


def test_a_refused_order_does_not_spend_its_zone_because_a_position_is_open() -> None:
    """⚠️ **The one state where the order of the two observers decides the answer**, and it is
    not reachable in a backtest — a position there can only come from an order that was
    submitted. In live it is the case `_observe_fill`'s own docstring names: a reconnect that
    swallows the fill, or a position opened outside this session.

    Both are true at once here: the armed order was refused *and* a position is open. If the
    fill is read first its fallback wins, and a zone whose order the venue never accepted is
    marked traded — spent for ever, on a trade that does not exist. Reading the refusal first
    forgets the name, and the fill observer correctly finds nothing of its own to claim.
    """
    phase = build_setup({"type": "structure_choch"})
    account = AccountState(balance=Decimal("10000"), equity=Decimal("10000"))
    candles = arms_a_resting_limit()

    armed: Signal | None = None
    re_armed: list[str | None] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles[:70]):
            refusals: tuple[Refusal, ...] = ()
            position = None
            if index == 62 and armed is not None:
                refusals = (
                    Refusal(
                        client_id=armed.client_id,
                        intent=SignalKind.ENTRY,
                        refused_by=RefusedBy.BROKER,
                        reason=armed.reason,
                        detail="the venue would not take it",
                    ),
                )
                position = Position(
                    symbol=EURUSD.symbol,
                    side=Side.LONG,
                    volume=Decimal(1),
                    entry_price=candle.open,
                    entry_time=candle.time,
                )
            signals = list(
                phase.on_bar(
                    Context(
                        candle=candle,
                        instrument=EURUSD,
                        account=account,
                        position=position,
                        refusals=refusals,
                    )
                )
            )
            for signal in signals:
                if signal.kind is SignalKind.ENTRY:
                    if index > 62:
                        re_armed.append(signal.client_id)
                    else:
                        armed = signal

    assert armed is not None, "the fixture is broken: nothing was ever armed"
    assert armed.client_id == "demand-20240103T0200-1", "the scenario has drifted"

    # ⚠️ **Asked of the machinery, not of `_traded`.** "The zone was spent" is only observable
    # as "it is never offered again" — reaching into the private set would let the assertion
    # pass against a phase that recorded the right thing and acted on the wrong one.
    assert re_armed, (
        "the zone was never offered again: a refused order was counted as the trade that "
        "spends a region, because a position happened to be open on the same bar"
    )
    assert re_armed[0] == "demand-20240103T0200-2", "it was re-offered under an unexpected name"


# --------------------------------------------------------------------------- #
# The cap on how many times one zone may be offered (PR-304-B-D-2c)             #
# --------------------------------------------------------------------------- #


class _RefusesEverything(BacktestBroker):
    """A permanent refusal, through the `BROKER` gate — which exists in a backtest too.

    ⚠️ That is the point of using this gate rather than a live one: the cap is a rule about the
    method, so the scenario that measures it must not need a venue.
    """

    def submit(self, order: OrderRequest) -> OrderResult:
        return OrderResult(order=order, accepted=False, reason="permanently no")


def _refusals_per_zone(broker: BacktestBroker) -> dict[str, int]:
    counted: dict[str, int] = {}
    for outcome in iter_run(
        candles=arms_a_resting_limit(),
        timeframe=HOUR,
        instrument=EURUSD,
        strategy=build_setup({"type": "structure_choch"}),
        broker=broker,
        risk=PercentRiskManager(percent=Decimal(1)),
    ):
        for refusal in outcome.refusals:
            if refusal.client_id is None:
                continue
            zone = refusal.client_id.rsplit("-", 1)[0]
            counted[zone] = counted.get(zone, 0) + 1
    return counted


def test_a_zone_stops_being_offered_after_three_refusals() -> None:
    """The measured loop, capped. ⚠️ **Both zones are asserted, and that is what separates a cap
    per zone from a cap per run.**

    Measured on `arms_a_resting_limit` against a permanent refusal *without* the cap: the demand
    zone is re-armed on every bar it stands — **48** in a row, bars 61 to 108 — and the supply
    zone twice, 50 in all. A single counter for the whole strategy would come back as three
    refusals total and satisfy any assertion made about one zone alone.

    The supply zone at **2** is the other half: it is under the cap, so it is untouched. A cap
    that fired early would show it at 1, and a test that only looked at the capped zone would not
    notice.
    """
    counted = _refusals_per_zone(
        _RefusesEverything(
            instrument=EURUSD, initial_capital=Decimal(10_000), cost_model=NoCostModel()
        )
    )

    # ⚠️ **The literal three, not `MAX_ARMING_ATTEMPTS`.** Written against the constant, this
    # assertion follows the rule wherever it goes: a mutant raising the cap to four changes the
    # code and the expectation together and survives — measured. The number is Guilherme's
    # decision (see the constant's docstring), so changing it has to be a deliberate act that
    # edits a test, not a one-character edit nothing notices.
    assert counted == {
        "demand-20240103T0200": 3,
        "supply-20240104T1600": 2,
    }, f"the cap did not bite per zone: {counted}"
    assert MAX_ARMING_ATTEMPTS == 3, "the constant and the measured behaviour disagree"


def test_the_cap_is_what_stops_it_and_not_the_market(monkeypatch: pytest.MonkeyPatch) -> None:
    """⚠️ **The vacuity check, and the test above is worth little without it.**

    Three refusals could just as easily be a window where the zone stopped standing on its own —
    and that reading would go on passing with the cap deleted. Lifting the cap out of reach
    restores the measured **48**, so the number above is the rule doing something rather than the
    market running out of bars.
    """
    monkeypatch.setattr(setups, "MAX_ARMING_ATTEMPTS", 10_000)

    lifted = _refusals_per_zone(
        _RefusesEverything(
            instrument=EURUSD, initial_capital=Decimal(10_000), cost_model=NoCostModel()
        )
    )

    assert lifted["demand-20240103T0200"] == 48, (
        "the zone stopped being armed for a reason other than the cap; the golden above proves "
        "nothing about the rule"
    )
    assert sum(lifted.values()) == 50


def _entries_when_refused_on(bars: set[int]) -> list[tuple[int, str | None]]:
    """Every entry the phase emits when the name it is *currently holding* is refused on `bars`.

    ⚠️ **Refuses the held name, not the one emitted on that bar**, which is the ADR-0024 path and
    the only one that can produce refusals with healthy gaps between them: `submit` already
    answered `accepted`, and the verdict arrives later from another process. `_drive_with_a_refusal`
    refuses on the bar of the arming, so every refusal it makes is consecutive with the last —
    a scenario in which "three in a row" and "three ever" cannot be told apart.
    """
    phase = build_setup({"type": "structure_choch"})
    account = AccountState(balance=Decimal("10000"), equity=Decimal("10000"))
    entries: list[tuple[int, str | None]] = []
    pending: tuple[Refusal, ...] = ()
    held: str | None = None

    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(arms_a_resting_limit()):
            signals = list(
                phase.on_bar(
                    Context(candle=candle, instrument=EURUSD, account=account, refusals=pending)
                )
            )
            for signal in signals:
                if signal.kind is SignalKind.ENTRY:
                    entries.append((index, signal.client_id))
                    held = signal.client_id
            pending = ()
            if index in bars and held is not None:
                pending = (
                    Refusal(
                        client_id=held,
                        intent=SignalKind.ENTRY,
                        refused_by=RefusedBy.EXECUTOR,
                        reason="entry.choch",
                        detail="volume 0.11 is above the cap of 0.10",
                    ),
                )
    return entries


def test_refusals_with_healthy_resting_between_them_do_not_retire_a_zone() -> None:
    """⚠️ **The cap is on refusals *in a row*, and this is the only test that says so.**

    Counted cumulatively — which is what the first version of this rule did — three one-minute
    hiccups spread across thirty-nine hours retire a setup exactly as a forty-five-minute outage
    would. That is not the rule that was chosen, and it is not the cost that was quoted for it.

    Measured on `arms_a_resting_limit`, refusing the held name on bars 61, 80 and 100 — the order
    rests untouched for eighteen and nineteen bars in between:

    * consecutive reading (this one): armed again as `-2`, `-3`, `-4`; the zone survives
    * cumulative reading: the third refusal retires it and `-4` never exists

    ⚠️ The mutant that restores the cumulative reading is one line, and it **survived all 806
    engine tests** before this existed — the golden below refuses on every bar, and in that
    scenario the two rules are the same rule.
    """
    scattered = _entries_when_refused_on({61, 80, 100})

    names = [name for _bar, name in scattered if name and name.startswith("demand-")]
    assert names == [
        "demand-20240103T0200-1",
        "demand-20240103T0200-2",
        "demand-20240103T0200-3",
        "demand-20240103T0200-4",
    ], f"a zone that rested healthily between refusals was retired anyway: {names}"


def test_three_refusals_in_a_row_do_retire_it() -> None:
    """The other arm of the same fixture, and it is what makes the one above mean something.

    Refused on 61, 62 and 63 — nothing rests in between — the zone stops being offered. Without
    this, "the zone survived" could be read as a cap that never fires at all.
    """
    consecutive = _entries_when_refused_on({61, 62, 63})

    names = [name for _bar, name in consecutive if name and name.startswith("demand-")]
    assert names == [
        "demand-20240103T0200-1",
        "demand-20240103T0200-2",
        "demand-20240103T0200-3",
    ], f"the cap did not retire a zone refused three times running: {names}"
