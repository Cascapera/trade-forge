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

from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    Candle,
    Context,
    Fill,
    OrderRequest,
    Position,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.loop import ENGINE_CONTEXT, run
from tradeforge_engine.setups import SetupContext, StructureStrategy, _to_tick
from tradeforge_engine.structure import OrderBlock, StructureKind, ZoneKind
from tradeforge_engine.testing import AAPL, HOUR, START, FixedRisk, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()


def _at(index: int) -> dt.datetime:
    return START + index * HOUR


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
    bar(8, open_="116", close="116", high="118", low="112"),  # pause
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
    index: int = -1

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.index += 1
        if context.marked:
            self.block = context.marked[0]
        return self.block if self.index == self.at else None


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
    index: int = -1

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.index += 1
        if self.block is None and context.marked:
            self.block = context.marked[0]
        return self.block if self.index == self.at else None


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
    index: int = -1

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.index += 1
        if self.block is None and context.marked:
            self.block = context.marked[0]
        return self.block if self.index >= self.at else None


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
    index: int = -1
    seen: list[OrderBlock] = field(default_factory=list)

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        self.index += 1
        self.seen.extend(context.marked)
        pick = self.picks.get(self.index)
        return self.seen[pick] if pick is not None else None


def _drive(
    strategy: StructureStrategy,
    candles: list[Candle],
    *,
    position_on: frozenset[int] = frozenset(),
) -> list[list[Signal]]:
    """Feed candles one at a time and collect the signals each bar produced.

    `position_on` names the bars where a fake position is open, standing in for the broker having
    filled the order — the strategy reads `context.position`, not the fill. Given as a set of bars
    rather than "from here on" because the interesting case is a trade that *ends*: the machinery
    has to still be right on the bars after the stop closed it.
    """
    out: list[list[Signal]] = []
    with localcontext(ENGINE_CONTEXT):
        for index, candle in enumerate(candles):
            position = None
            if index in position_on:
                position = Position(
                    symbol=AAPL.symbol,
                    side=Side.LONG,
                    volume=Decimal(1),
                    entry_price=candle.open,
                    entry_time=candle.time,
                )
            context = Context(candle=candle, instrument=AAPL, account=_ACCOUNT, position=position)
            out.append(list(strategy.on_bar(context)))
    return out


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
    signals = _drive(strategy, _IMPULSE)

    assert [len(bar_signals) for bar_signals in signals] == [0] * 9 + [1]
    [signal] = signals[9]
    assert signal.kind is SignalKind.ENTRY
    assert signal.side is Side.LONG
    assert signal.limit_price == Decimal("100")
    assert signal.stop_loss == Decimal("89")
    assert signal.reason == "entry.test"
    assert signal.client_id is not None  # it has to be nameable to be withdrawable


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

    [signal] = _drive(strategy, candles)[9]
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


def test_only_the_primary_zone_reaches_the_qualifier_by_default() -> None:
    """The impulse marks two zones. By default a setup is offered only the primary — the first
    gap event of the move — and the secondary is not its business to refuse."""
    qualifier = _Marked()
    _drive(StructureStrategy(qualifier=qualifier), _IMPULSE)

    marked = qualifier.seen[9].marked
    assert [(zone.time, zone.primary) for zone in marked] == [(_at(3), True)]


def test_allow_secondary_offers_both_zones() -> None:
    """Turned on, the same impulse offers both, primary first — the flag the author asked for."""
    qualifier = _Marked()
    _drive(StructureStrategy(qualifier=qualifier, allow_secondary=True), _IMPULSE)

    marked = qualifier.seen[9].marked
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
    signals = _drive(strategy, [*_IMPULSE, *second])

    first_id = signals[9][0].client_id
    kinds = [(s.kind, s.client_id) for s in signals[14]]
    assert kinds[0] == (SignalKind.CANCEL, first_id)  # the old order, withdrawn first
    assert kinds[1][0] is SignalKind.ENTRY
    assert kinds[1][1] != first_id  # a filled or withdrawn name is never reused


def test_naming_the_same_zone_again_does_not_churn_the_order() -> None:
    """A qualifier that keeps pointing at the zone it already qualified is not a new setup.

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
    signals = _drive(StructureStrategy(qualifier=_Sticky()), [*_IMPULSE, *quiet])

    assert len(signals[9]) == 1  # armed once
    assert signals[9][0].kind is SignalKind.ENTRY
    # and the three bars of the qualifier saying the same thing again produce nothing at all
    assert (signals[10], signals[11], signals[12]) == ([], [], [])


def test_repeating_a_zone_armed_but_not_yet_placed_emits_nothing() -> None:
    """The repeat guard in the window before the order reaches the book.

    An unfilled zone is never in `_traded` — only a fill puts it there — so the armed-zone guard
    is what refuses a qualifier repeating itself, in both windows. This test pins the earlier
    one: between qualifying a zone and placing its order there is a real gap — price is still
    inside the region, so no order can rest there yet. A qualifier repeating itself there would
    withdraw an order that was never placed and re-arm under a fresh name, every bar, until
    price finally cleared the zone.

    Bars 10 and 11 both close inside [90, 100] with the setup naming the zone on each; bar 12
    closes clear and the order finally goes out — once, at the level it always would have.
    """
    inside = [
        bar(10, open_="124", close="96", high="125", low="95"),  # qualified here, inside the zone
        bar(11, open_="96", close="97", high="98", low="95"),  # still inside: the repeat
        bar(12, open_="97", close="103", high="104", low="96"),  # clear of it at last
    ]
    signals = _drive(StructureStrategy(qualifier=_StickyFrom(at=10)), [*_IMPULSE, *inside])

    assert (signals[10], signals[11]) == ([], [])  # nothing while price is inside the zone
    assert [s.kind for s in signals[12]] == [SignalKind.ENTRY]  # and exactly one order after


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
    signals = _drive(StructureStrategy(qualifier=_Once()), [*_IMPULSE, *through])

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
    signals = _drive(StructureStrategy(qualifier=_Marked()), [*_IMPULSE, *quiet])

    assert signals[10] == []
    assert signals[11] == []


def test_nothing_is_armed_while_a_position_is_open() -> None:
    """This phase holds one position at a time, and the trade is the broker's to end.

    A position open means our order filled. Arming another zone would submit an entry the broker
    refuses, and withdrawing the filled order would be a cancel for something that no longer
    rests — noise either way.
    """
    signals = _drive(StructureStrategy(qualifier=_Marked()), _IMPULSE, position_on=frozenset({9}))

    assert all(bar_signals == [] for bar_signals in signals)


def test_an_order_waits_for_price_to_clear_the_zone_before_it_is_placed() -> None:
    """A buy limit has to rest below the market. While price is still inside the region the level
    is above it, and `Signal` refuses that as the sign error it usually is (ADR-0014).

    Nothing is lost by waiting: the zone stays armed and the order goes out on the first bar that
    closes clear of it. Here the setup qualifies on bar 10, which closes at 96 — inside [90, 100]
    — and the entry appears on bar 11, at the same level it always would have rested at.
    """
    inside = [
        bar(10, open_="124", close="96", high="125", low="95"),  # closes inside the zone
        bar(11, open_="96", close="101", high="102", low="95"),  # closes clear of it again
    ]
    signals = _drive(StructureStrategy(qualifier=_OnBar(at=10), name="test"), [*_IMPULSE, *inside])

    assert signals[9] == []  # the qualifier said nothing on the bar that marked the zone
    assert signals[10] == []  # qualified, but price is inside it — no order can rest there yet
    [signal] = signals[11]
    assert signal.kind is SignalKind.ENTRY
    assert (signal.limit_price, signal.stop_loss) == (Decimal("100"), Decimal("89"))


def test_the_wait_mirrors_for_a_sell_limit() -> None:
    """The short side of the same rule, and it is not symmetric by accident: a sell limit rests
    *above* the market, so what defers it is price still being **below** the zone's bottom edge.

    The mirrored scenario reflects to supply [100, 110]. Bar 10 closes at 104, inside it, so no
    order can rest there yet; bar 11 closes at 99, clear below, and the order goes out at 100.
    """
    inside = [
        bar(10, open_="124", close="96", high="125", low="95"),
        bar(11, open_="96", close="101", high="102", low="95"),
    ]
    signals = _drive(
        StructureStrategy(qualifier=_OnBar(at=10), name="test"), _mirror([*_IMPULSE, *inside])
    )

    assert signals[10] == []
    [signal] = signals[11]
    assert signal.side is Side.SHORT
    assert (signal.limit_price, signal.stop_loss) == (Decimal("100"), Decimal("111"))


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
    signals = _drive(StructureStrategy(qualifier=_Fixed(foreign)), [*_IMPULSE, *quiet])

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
    signals = _drive(StructureStrategy(qualifier=_Remembers(at=12)), [*_IMPULSE, *spent])

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
    signals = _drive(strategy, [*_IMPULSE, *after], position_on=frozenset({10}))

    assert signals[9][0].kind is SignalKind.ENTRY  # armed once, on the qualifying bar
    assert (signals[11], signals[12]) == ([], [])  # and never again, though the zone still stands
    assert strategy._blocks.zones[0].usable  # the zone really did survive the trade


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
    signals = _drive(strategy, [*_IMPULSE, *descent])
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
    assert strategy._blocks.zones[0].usable  # refused as traded, not as dead


def test_an_invisible_fill_does_not_leave_a_phantom_armed_order() -> None:
    """The discriminator for the fill observation itself, on a *different* zone qualifying next.

    The sibling test above re-names the traded zone, and that cannot tell the observation apart
    from its absence: an unobserved fill leaves `_armed` pointing at the zone with `placed=True`,
    and the repeat guard refuses the re-arm with the same silence `_traded` would — right answer,
    wrong reason. The difference only shows when another zone qualifies after the invisible fill:

    * observed (correct): the armed name was forgotten with the fill, so zone B arms with
      **exactly one ENTRY and no CANCEL** — nothing rests to withdraw;
    * unobserved: the phantom is withdrawn first (a cancel for an order the trade consumed), and
      worse, the traded zone A never entered `_traded` — so when the setup names A again, it
      re-arms and the martingale is back through the supersession door.

    Geometry, chosen so zone A is still alive to answer step (b): the marking candle is dug to 80,
    making A [80, 100] — twenty wide, so mitigation needs a close at 120 and the closes here stay
    under it — and zone B is the same impulse's secondary at [110, 117], reachable while A lives.
    The stop sits at 78, so the one-bar round trip needs a low of 77: a flash-crash bar, which is
    what it takes for entry and stop to die together while both regions survive on the close.
    """
    candles = [*_IMPULSE]
    candles[3] = bar(3, open_="99", close="99", high="100", low="80")  # zone A becomes [80, 100]
    strategy = StructureStrategy(
        qualifier=_Script(picks={9: 0, 13: 1, 14: 0}), allow_secondary=True
    )
    descent = [
        bar(10, open_="124", close="118", high="125", low="116"),
        bar(11, open_="118", close="112", high="119", low="111"),
    ]
    signals = _drive(strategy, [*candles, *descent])
    [entry] = signals[9]
    assert (entry.limit_price, entry.stop_loss) == (Decimal("100"), Decimal("78"))

    wick_out = bar(12, open_="112", close="112", high="113", low="77")  # fills 100, stops 78
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
        b_bar = bar(13, open_="112", close="118", high="119", low="111")  # zone B is named
        b_signals = strategy.on_bar(Context(candle=b_bar, instrument=AAPL, account=_ACCOUNT))
        a_again = bar(14, open_="118", close="119", high="120", low="117")  # zone A named again
        a_signals = strategy.on_bar(Context(candle=a_again, instrument=AAPL, account=_ACCOUNT))

    assert during == ()
    [b_entry] = b_signals  # exactly one signal: no cancel for the consumed phantom
    assert b_entry.kind is SignalKind.ENTRY
    assert b_entry.limit_price == Decimal("117")  # the secondary zone's top
    assert a_signals == ()  # the traded zone is spent; zone B's order stays where it is
    assert strategy._blocks.zones[0].usable  # and A was refused as traded, not as dead


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
    signals = _drive(
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
    signals = _drive(
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
    off = _drive(StructureStrategy(qualifier=_FromTracker(index=1)), _IMPULSE)
    assert all(bar_signals == [] for bar_signals in off)

    on = _drive(StructureStrategy(qualifier=_FromTracker(index=1), allow_secondary=True), _IMPULSE)
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
    signals = _drive(StructureStrategy(qualifier=_Marked()), candles)

    assert all(bar_signals == [] for bar_signals in signals)


def test_a_zone_with_no_width_arms_nothing() -> None:
    """Both edges at one price: the stop would land on the entry and the trade would carry no
    risk at all — which is not a free trade, it is a division by zero in position sizing."""
    candles = [*_IMPULSE]
    # A marking candle with no range: high == low, so top == bottom.
    candles[3] = bar(3, open_="100", close="100", high="100", low="100")
    signals = _drive(StructureStrategy(qualifier=_Marked()), candles)

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
        candles=[*_IMPULSE, *pullback],
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
        candles=[*_IMPULSE, *pullback],
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
