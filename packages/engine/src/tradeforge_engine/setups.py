"""The entry machinery the structure setups share.

Four setups — flip, choch, continuation, grab — disagree about exactly one thing: **which zone is
worth trading**. After that they are the same machine. Price comes back to the region, the
position opens at its near edge, the stop sits just outside the far edge, and the target is a
multiple of that risk. Writing the second half once is what makes the four cheap; writing it four
times is what makes them drift apart, and a method whose four setups disagree about where the
stop goes is four methods.

So a setup here **is** a qualifier: given a bar, whatever break it produced and the zones that
break revealed, name the zone to trade or say nothing. `SetupQualifier` is the entire seam.

**The order goes on the book at the event that configures the setup.** A break of structure
confirms and the region it left is already worth an order; the method is operated with a pending
order sitting in the zone, so the backtest places one there too. Deliberately *not* "wait until
price has visited the region and left again": the first return to a zone is the cleanest touch it
will ever get, and a machine that waits for the second one watches the trade it was built for go
past.

**What this module does not do.** It never closes a position. The exits are the broker's — the
target at a multiple of risk (`take_profit_rr`) and the stop — and the strategy reaches them only
to *tighten*, never to leave. The trade ends at a level, always.

**The stop is conducted by structure.** The first break of structure in the trade's favour brings
the stop to the entry price; every break after it brings the stop to that break's `origin` — the
low the up-move came from, which is the level whose loss would turn the trend (`StructureBreak`).
So the stop walks up behind the structure it is riding, and sits exactly where being wrong would
be *proven*, not merely feared. Touching a multiple of the initial risk brings it to the entry
price too, and the two rules run at once with the tighter one winning (`conduction.py`).
"""

import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

from tradeforge_engine.conduction import StructuralTrail, breakeven_candidate, tighten
from tradeforge_engine.domain import (
    ZERO,
    Candle,
    Context,
    Money,
    Position,
    Side,
    Signal,
    SignalKind,
    SnapshotLevel,
    SnapshotRegion,
)
from tradeforge_engine.structure import (
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    StructureBreak,
    StructureKind,
    TrackedZone,
    ZoneKind,
)

logger = logging.getLogger(__name__)

DEFAULT_STOP_BUFFER = Decimal("0.1")
"""How far past the zone's far edge the stop sits, as a fraction of the zone's own width.

The author's numbers: a supply zone of [90, 100] is sold from 90 with the stop at 101, and a
demand zone of [90, 100] is bought at 100 with the stop at 89. The zone is ten wide, so the stop
clears it by one — the region is where price is expected to turn, and a stop *on* its edge is
stopped by the noise of the turn itself.
"""


@dataclass(frozen=True, slots=True)
class SetupContext:
    """Everything a qualifier is allowed to see on one bar.

    The same anti-lookahead shape as `Context`: one candle, and only what this bar revealed.

    `break_` is what `MarketStructure` returned for this candle and `marked` are the zones that
    break left behind — together they are the choch and continuation setups' whole input. `zones`
    is every order block still being tracked, with what price has since done to each, because the
    flip setup does not qualify on a break at all: it qualifies when a *zone* is taken out.
    """

    candle: Candle
    break_: StructureBreak | None
    marked: tuple[OrderBlock, ...]
    zones: tuple[TrackedZone, ...]

    stopped: OrderBlock | None = None
    """The zone whose trade the stop ended on this bar, if any.

    The author's ladder rule — "stopou → ordem na primária" — is a condition on the *trade's*
    outcome, and the zone's own marks cannot carry it: a trade one width in profit has already
    mitigated its zone the healthy way while still open, and a mitigated zone is frozen, so the
    stop that later takes the trade out leaves no new mark to read. The machinery watches the
    fills (ADR-0015), so it reports the outcome itself.
    """

    won: OrderBlock | None = None
    """The zone whose trade ended in profit on this bar — the target, or any exit that is not
    the stop."""


class SetupQualifier(Protocol):
    """A setup, reduced to the only question that distinguishes it from the others.

    Return the zone to arm, or `None`. Returning a zone is a decision to trade it: the machinery
    withdraws whatever order was resting and puts a new one on the region named here.

    Qualifiers are stateful by design — continuation has to remember a change of character before
    the break that confirms it can qualify anything — so `qualify` is called on **every** bar,
    including bars that produced no break.
    """

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        """Name the zone this bar qualified, or `None`."""
        ...


class ChochQualifier:
    """The choch setup: trade the zone the change of character left behind.

    A change of character confirms — price closes through the anchor and the trend inverts — and
    the leg that broke it left a region on the way. Price comes back, touches it, and the trade
    is taken in the *new* trend's direction. Deliberately nothing more is waited for: a setup
    that also wants a break in the new trend's favour is the continuation setup, not this one.
    And a leg that left no inefficiency marks no zone, so it offers no trade (the author's rule:
    "sem ineficiência não tem trade").

    **The ladder** (the author's rule for legs that leave more than one zone). The pullback
    meets the zones in reverse order of marking — the last secondary is the nearest, the primary
    at the leg's origin is the farthest — so the order hangs on the *nearest* zone first. What
    moves the ladder is the **trade's outcome**, not the zone's marks: a rung whose trade the
    stop took out hands the order to the next zone toward the origin ("stopou a venda → ordem na
    primária"), and a rung whose trade won ends the ladder — no order waits behind a winner. The
    outcome has to come from the machinery (`SetupContext.stopped` / `won`), because the zone
    cannot tell the two apart: a trade one width in profit has already mitigated its zone the
    healthy way while still open, and the stop that later ends it leaves no new mark. A rung
    that dies with no trade taken — aged out, or spent before price ever reached the order — is
    simply skipped. With `allow_secondary` off the machinery offers only the primary, and the
    ladder is a single rung.

    A new change of character replaces the ladder wholesale, whichever direction it points. That
    is not a special case, it is the setup reapplied: a contrary choch had to come through the
    old region to happen (stopping whatever was resting there — the machinery already withdrew
    or filled it), and the zones *its* leg left are simply the next trade.
    """

    def __init__(self) -> None:
        self._ladder: list[OrderBlock] = []

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        """Name the ladder's current rung, advancing on the outcome the machinery reported."""
        # The outcome belongs to the regime that produced the trade, so it is settled before a
        # new change of character replaces the ladder on this same bar.
        if context.won is not None:
            self._ladder = []
        elif context.stopped is not None and self._ladder and self._ladder[0] == context.stopped:
            self._ladder.pop(0)

        break_ = context.break_
        if break_ is not None and break_.kind is StructureKind.CHOCH:
            # Nearest zone first, the leg's origin last. `marked` arrives in marking order —
            # origin first — so the ladder is its reverse.
            self._ladder = list(reversed(context.marked))

        while self._ladder:
            rung = self._ladder[0]
            tracked = next((zone for zone in context.zones if zone.block == rung), None)
            if tracked is not None and tracked.usable:
                return rung
            # Dead with no trade taken — aged out of the tracker, or spent before price ever
            # came back to the order. The next zone toward the origin answers; the machinery
            # would refuse this one anyway.
            self._ladder.pop(0)
        return None


class ContinuationQualifier:
    """The continuation setup: trade the zone a break *in the trend's favour* leaves behind.

    Where the choch trades the reversal itself, continuation trades what comes after it. A change
    of character turns the trend; then the market resumes that new trend and **breaks structure in
    its favour** (a BOS), and the leg of that break leaves a region. Price pulls back into it and
    the trade is taken with the trend. Two things therefore have to have happened, in order: the
    turn, and then a break confirming it — a BOS with no change of character before it is just an
    old trend continuing, which this setup deliberately leaves to no one. And, as everywhere in the
    method, a leg that left no inefficiency marks no zone and offers no trade ("sem ineficiência
    não tem trade").

    A BOS always continues the trend in force — that is what distinguishes it from a choch — so
    "in the trend's favour" needs no direction check: *any* BOS is in favour of the trend it
    breaks. The only question is whether that trend was **born of a change of character**. So the
    qualifier arms on a BOS once a choch has been seen, and never before — the very first break in
    a fresh dataset is a bootstrap BOS with no reversal behind it, and it is not a continuation.

    **How many BOS per reversal** (`max_bos`). A trend that a choch opened will usually break
    structure in its favour more than once as it runs, and each such break leaves its own leg's
    zones — the classic continuation trades the pullback into *each* new leg. That is the default
    (`max_bos=None`). Set `max_bos=n` to trade only the first *n* breaks after each change of
    character: `1` is the strict reading, "the BOS that confirms the new trend", and nothing after
    it until the trend turns again. Only a break that actually leaves a zone counts toward the cap
    — a BOS with no inefficiency is a non-event, and letting it burn a slot would silently cost a
    configured trade. The count resets on every change of character.

    **The ladder** is the choch's, unchanged: with more than one zone the pullback meets them
    nearest-first, the order hangs on the nearest, a stopped rung hands the order to the next
    toward the origin, a winning rung ends the ladder, and a rung that dies untraded is skipped.
    `allow_secondary` off leaves a single rung, the primary at the leg's origin. See
    `ChochQualifier` for why the outcome has to come from the machinery and not the zone's marks.

    A change of character replaces the ladder wholesale and re-opens the count, whichever way it
    points: the reversal came back through the region the continuation was resting in, and the
    zones its own next BOS leaves are simply the next trades. The choch that turns the trend is not
    itself a continuation entry — its region is the choch setup's to trade, not this one's.
    """

    def __init__(self, *, max_bos: int | None = None) -> None:
        if max_bos is not None and max_bos < 1:
            raise ValueError(f"max_bos is a count of breaks to trade, got {max_bos}")
        self._max_bos = max_bos
        self._ladder: list[OrderBlock] = []
        # Breaks armed since the last change of character; `None` until the first choch is seen,
        # which is the "not eligible yet" state — a bootstrap BOS has no reversal behind it.
        self._since_choch: int | None = None

    def qualify(self, context: SetupContext) -> OrderBlock | None:
        """Name the ladder's current rung, arming on a favourable break once a choch has opened
        the trend and the cap still allows it."""
        # The outcome belongs to the regime that produced the trade, settled before a new break
        # touches the ladder on this same bar (see `ChochQualifier.qualify`).
        if context.won is not None:
            self._ladder = []
        elif context.stopped is not None and self._ladder and self._ladder[0] == context.stopped:
            self._ladder.pop(0)

        break_ = context.break_
        if break_ is not None and break_.kind is StructureKind.CHOCH:
            # The turn cancels the continuation context and re-opens the count. The choch's own
            # region is not ours to trade.
            self._since_choch = 0
            self._ladder = []
        elif (
            break_ is not None
            # Only a BOS can reach here — the branch above consumed the CHoCH, and those are the
            # only two kinds — so this test is redundant *today*. It is kept as a deliberate guard,
            # mirroring the choch qualifier's positive discriminator: were a third kind of break
            # ever added, the safe default is for continuation to ignore it rather than silently
            # treat it as a favourable break. No test can kill removing it while two kinds exist.
            and break_.kind is StructureKind.BOS
            and self._since_choch is not None
            and context.marked
            and (self._max_bos is None or self._since_choch < self._max_bos)
        ):
            # A favourable break that leaves zones, within the cap: it counts, and its leg's zones
            # become the new ladder — nearest zone first, the leg's origin last.
            self._since_choch += 1
            self._ladder = list(reversed(context.marked))

        while self._ladder:
            rung = self._ladder[0]
            tracked = next((zone for zone in context.zones if zone.block == rung), None)
            if tracked is not None and tracked.usable:
                return rung
            # Dead with no trade taken — aged out, or spent before price returned. The next zone
            # toward the origin answers; the machinery would refuse this one anyway.
            self._ladder.pop(0)
        return None


@dataclass(frozen=True, slots=True)
class ZoneEntry:
    """Where an order rests on a zone, and where its stop goes."""

    side: Side
    limit_price: Money
    stop_loss: Money


@dataclass(slots=True)
class _Armed:
    """The one zone currently holding an order, and whether that order reached the book."""

    block: OrderBlock
    client_id: str
    placed: bool
    confirmed_by: StructureBreak | None = None
    """The break that revealed this zone, kept for the entry's picture.

    Kept here rather than looked up later because it cannot be looked up later: the qualifier
    names a zone on the bar the break confirms, and the order may not be placeable for many bars
    after that — by which time `MarketStructure` has moved on and the level this trade was built
    on is gone from every object still in scope. The zone remembers what revealed it, or nothing
    downstream can say why the zone was worth entering."""


class StructureStrategy:
    """A `Strategy` that arms one limit order on the zone its qualifier names.

    **One live order at a time.** A newly qualified zone replaces whatever was resting: the old
    order is withdrawn and a new one goes on the new region. Several simultaneous pending orders
    would be defensible — the method leaves several valid zones behind — but they turn "why did
    this backtest take that trade" into a question about which of four orders won a race, and the
    honest answer would be *arrival order in a list*. One order is auditable.

    **The order's life is the zone's life.** There is no separate expiry. While the zone still
    stands the order waits; the bar the zone is spent — closed through, or driven a full width
    clear of, or simply aged out of the tracker — the order is withdrawn. Only the strategy can
    know that, which is why `Broker.cancel` exists and why the broker is never told about zones
    (`AGENTS.md §5.4`).

    **One trade per zone, ever.** A region whose order filled is finished here, whether the
    stop, the target, or the same bar ended the trade. Without this the machine martingales: a
    zone survives being traded — a wick down through a demand zone only marks it *flipped*, and
    mitigation wants a close beyond it — so a stateful qualifier still pointing at the region
    re-arms it the bar after the stop, and buys the same level into the same downtrend until the
    zone finally breaks. The backtest then reports a setup that averages down, and the equity
    curve blames the setup rather than this class.

    The line is drawn at the **fill**, deliberately — the author's rule: placing the order and
    *activating the trade* is what spends a region. A zone whose order was withdrawn untouched,
    superseded by a newer zone before price ever came back, was never traded; if the setup names
    it again it may be armed again. What cannot happen twice is the trade. The fill is observed
    from `Context.fills` (ADR-0015), with the open position as a fallback — never inferred from
    the strategy's own bookkeeping, because the one fill that matters most is the one whose
    position opened and died inside a single bar, invisible to both.
    """

    def __init__(
        self,
        *,
        qualifier: SetupQualifier,
        name: str = "structure",
        allow_secondary: bool = False,
        stop_buffer: Decimal = DEFAULT_STOP_BUFFER,
        breakeven_at_r: Decimal | None = Decimal(2),
    ) -> None:
        if stop_buffer < ZERO:
            raise ValueError(f"stop buffer is a fraction of the zone width, got {stop_buffer}")
        if breakeven_at_r is not None and breakeven_at_r <= ZERO:
            raise ValueError(f"breakeven R multiple must be positive, got {breakeven_at_r}")

        self._qualifier = qualifier
        self._name = name
        self._allow_secondary = allow_secondary
        self._stop_buffer = stop_buffer
        self._breakeven_at_r = breakeven_at_r

        self._structure = MarketStructure()
        self._blocks = OrderBlockDetector()
        self._armed: _Armed | None = None
        self._armed_count = 0
        # Zones whose order became a trade. Membership only — never iterated — so it cannot
        # leak set ordering into a result (`AGENTS.md §5.2`).
        self._traded: set[OrderBlock] = set()
        # The zone whose trade is currently open (or ended on a bar not yet reported). This is
        # what lets the machinery tell the qualifier *how* a trade ended — see
        # `SetupContext.stopped`.
        self._filled: OrderBlock | None = None
        # The structural half of the conduction — breakeven on the first break in the trade's
        # favour, leg origins after it — and the count of breaks that rule needs, keyed to the
        # trade rather than reset by an event. See `StructuralTrail`.
        self._trail = StructuralTrail()
        # The bar a fill was observed on. The breakeven rule reads the bar's extremes, and on this
        # one bar those extremes are not the trade's — see `_conduct`.
        self._fill_bar: datetime | None = None

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        candle = context.candle
        break_ = self._structure.update(candle)
        marked = self._blocks.update(candle, break_)

        self._observe_fill(context)

        position = context.position
        if position is not None:
            # Nothing rests and nothing may be armed while this phase's one position is open. Any
            # name still armed here never reached the book (`_observe_fill` already forgot a
            # placed one), and holding it would leave it going stale behind a position it knows
            # nothing about.
            #
            # What this bar can still owe is a *tightening*. It is the broker's stop and target
            # that end the trade — the strategy never closes one — but the stop is the strategy's
            # to move, and the break of structure that moves it arrives on exactly the bars this
            # branch used to return empty from.
            self._armed = None
            conducted = self._conduct(position, context, break_)
            return () if conducted is None else (conducted,)

        signals: list[Signal] = []
        stopped, won = self._trade_outcome(context)

        # A zone that no longer stands takes its order with it, before anything else this bar:
        # the order was only ever an expression of that region's validity.
        if self._armed is not None and not self._still_standing(self._armed.block):
            signals.append(self._withdraw(self._armed, candle))
            self._armed = None

        candidates = tuple(block for block in marked if block.primary or self._allow_secondary)
        chosen = self._qualifier.qualify(
            SetupContext(
                candle=candle,
                break_=break_,
                marked=candidates,
                zones=self._blocks.zones,
                stopped=stopped,
                won=won,
            )
        )
        if chosen is not None and self._may_arm(chosen):
            if self._armed is not None:
                signals.append(self._withdraw(self._armed, candle))
            self._armed_count += 1
            self._armed = _Armed(
                block=chosen,
                client_id=f"{chosen.kind.value}-{chosen.time:%Y%m%dT%H%M}-{self._armed_count}",
                placed=False,
                confirmed_by=break_,
            )

        if self._armed is not None and not self._armed.placed:
            entry = self._entry_for(self._armed.block, context)
            if entry is not None:
                self._armed.placed = True
                signals.append(
                    Signal(
                        kind=SignalKind.ENTRY,
                        side=entry.side,
                        reference_price=candle.close,
                        stop_loss=entry.stop_loss,
                        reason=f"entry.{self._name}",
                        limit_price=entry.limit_price,
                        client_id=self._armed.client_id,
                        # The region the order is waiting at, twice over and on purpose. The
                        # scalars are what a later "does this only work on narrow zones?"
                        # aggregates; the rectangle is what gets drawn — and it needs a left
                        # edge in time, which a scalar cannot carry. Both are read off the same
                        # block one line apart, so they cannot drift.
                        context={
                            "zone_top": self._armed.block.top,
                            "zone_bottom": self._armed.block.bottom,
                        },
                        regions=(
                            SnapshotRegion(
                                label="zone",
                                top=self._armed.block.top,
                                bottom=self._armed.block.bottom,
                                # The bar the zone is *drawn on* — the candle before the gap —
                                # not `confirmed_at`, the later bar whose break revealed it. A
                                # rectangle starting at the confirmation would show the zone
                                # as younger than it is, and hide the impulse that made it.
                                from_time=self._armed.block.time,
                            ),
                        ),
                        # The structure that broke, drawn from the bar that set it to the bar
                        # that crossed it. It is the event that made the zone worth anything,
                        # and it is otherwise absent from the record: the bars would show price
                        # crossing a price, with nothing saying which price mattered.
                        levels=_structure_levels(self._armed.confirmed_by),
                    )
                )

        return tuple(signals)

    def _conduct(
        self, position: Position, context: Context, break_: StructureBreak | None
    ) -> Signal | None:
        """The open trade's stop, moved by whichever rule is tighter — or `None` to leave it.

        Two rules, and **neither of them closes the position**:

        * **The first break of structure in the trade's favour** brings the stop to the entry
          price. Every break after it brings the stop to that break's `origin`. That rule and its
          bookkeeping live in `StructuralTrail`, shared with the Ponto Contínuo — which enters
          nothing like this setup and conducts exactly like it.
        * **Touching a multiple of the initial risk** brings it to the entry price as well.

        The two run at once and the tighter wins, which no code here decides — `tighten` simply
        never asks for a level that fails to improve on the one in force (ADR-0018).

        **The breakeven rule sits out the bar that filled, and this is a correctness fix rather
        than caution.** It reads the bar's extremes, and a *limit* entry fills in the middle of a
        bar the trade did not live through: the order rests where price has to come back to, so
        the excursion in our favour happens **before** the fill. Measured on the ladder golden, a
        sell filling at 103 sat on a bar whose low was 87 — past its own 2R line of 89.80 — and
        crediting that print turned a full-R loser into a scratch. No future data is read, which is
        exactly what makes it dangerous: every backtest gets quietly better and no number looks
        wrong. The swing family does not need this guard, because a *stop* entry fills going
        through its level in the direction of travel, so the bar's favourable extreme is
        necessarily after the fill.

        The structural rule keeps running on that bar: a break of structure is confirmed by the
        **close**, which is after any fill inside it.
        """
        candle = context.candle
        candidates: list[Money] = []

        structural = self._trail.candidate(position=position, break_=break_)
        if structural is not None:
            candidates.append(structural)

        if candle.time != self._fill_bar:
            breakeven = breakeven_candidate(
                position=position,
                side=position.side,
                candle=candle,
                multiple=self._breakeven_at_r,
            )
            if breakeven is not None:
                candidates.append(breakeven)

        return tighten(
            position=position,
            side=position.side,
            candle=candle,
            candidates=candidates,
            reason=f"trail.{self._name}",
        )

    def _observe_fill(self, context: Context) -> None:
        """Notice the armed order becoming a trade, and spend its zone for good.

        The fill is the event that spends a region — an order withdrawn before it filled leaves
        its zone tradeable again (see the class docstring) — so the fill has to be *observed*,
        not deduced. Two signs, either one enough. The bar's own fills carrying the armed name
        is the only sign that survives the trade that opens and dies inside one bar: the broker
        fills before the strategy runs (`loop.py`), so a limit taken and stopped out by the same
        bar leaves no open position for `context.position` to show — and a zone often survives
        exactly that bar, a wick through only marks it flipped. The open position is the
        fallback for a fill the strategy was never shown, which against a live terminal a
        reconnect can swallow while the position is plainly there.

        Forgetting `_armed` here is part of the observation: the order is not resting any more,
        and keeping the name would produce a cancel for an order the trade already consumed.
        """
        armed = self._armed
        if armed is None or not armed.placed:
            return
        filled = any(fill.order.client_id == armed.client_id for fill in context.fills)
        if filled or context.position is not None:
            self._traded.add(armed.block)
            self._filled = armed.block
            self._armed = None
            self._fill_bar = context.candle.time

    def _trade_outcome(self, context: Context) -> tuple[OrderBlock | None, OrderBlock | None]:
        """`(stopped, won)` — the zone whose trade ended on this bar, by how it ended.

        The broker's protective exits stamp their reason on the order (`"sl"` / `"tp"`), and the
        exit fill arrives in `Context.fills` like any other. Anything that is not the stop ends
        the trade on the trader's terms, so it counts as won — the distinction the ladder needs
        is only "did the market throw the trade out through the far side".

        This phase holds one position at a time, so the exit necessarily belongs to the zone in
        `_filled`; the name is consumed with the report, and a bar with no exit reports nothing.
        """
        block = self._filled
        if block is None:
            return None, None
        for fill in context.fills:
            if fill.order.intent is SignalKind.EXIT:
                self._filled = None
                if fill.order.reason == "sl":
                    return block, None
                return None, block
        return None, None

    def _may_arm(self, block: OrderBlock) -> bool:
        """May an order be put on this zone at all?

        The single chokepoint, and it is deliberately not spread across the call sites that feed
        it. A qualifier can name any zone it can see, including one it read out of
        `SetupContext.zones` — which is how the flip setup works and which no filter upstream
        touches. Every rule about *whether a region may be traded* is therefore enforced here,
        once, on the zone actually about to be armed.

        Four refusals, in the order they are cheapest to answer:

        * **The zone already armed.** Re-naming it is not a new setup; acting on the repeat would
          withdraw a resting order and put an identical one back a bar later, moving the fill to
          whichever bar the qualifier last repeated itself.
        * **A secondary zone while `allow_secondary` is off.** The flag is a rule about which
          regions may be traded, so it has to bite where the trade is decided.
        * **A zone that no longer stands** — mitigated, or dropped by the tracker. Checking this
          only at the top of the bar would be a bar too late: the broker fills before the strategy
          runs (`loop.py`), so an order armed on a dead zone fills before its cancel is ever sent.
        * **A zone that has already given its trade.** See the class docstring: without it the
          machine re-buys a level it was just stopped out of. Only a *fill* puts a zone in
          `_traded` — an order withdrawn untouched leaves its region free to be named again.
        """
        if self._armed is not None and block == self._armed.block:
            return False
        if not block.primary and not self._allow_secondary:
            return False
        if block in self._traded:
            return False
        return self._still_standing(block)

    # ----------------------------------------------------------------------- #
    # Zone geometry                                                            #
    # ----------------------------------------------------------------------- #

    def _entry_for(self, block: OrderBlock, context: Context) -> ZoneEntry | None:
        """The order this zone would place on this bar, or `None` if it cannot place one yet.

        The near edge is the side price has to come back to, so it is where the order rests: a
        demand zone is bought at its **top**, a supply zone sold at its **bottom**. The stop sits
        past the far edge by a fraction of the zone's own width — the region is where price is
        expected to turn, and a stop level *on* the edge is taken out by the turn itself.

        Two bars where nothing is placed, both returning `None` rather than raising:

        * **Price is not clear of the zone yet.** A buy limit has to rest *below* the market; with
          price still inside the region, the level is above it, and `Signal` refuses that as the
          sign error it usually is (ADR-0014). Nothing is lost by waiting — the zone stays armed
          and the order goes out on the first bar that closes clear of it.
        * **The zone has no width.** Its two edges are one price, so the stop would land on the
          entry and the trade would carry no risk at all — which is not a free trade, it is a
          division by zero in position sizing.
        * **The stop would land at or below zero.** A zone more than ten times as tall as its own
          floor pushes the buffer past nothing, and a stop at a non-positive price is not a wide
          stop — it is *no stop*, because `low <= stop` can never be true. Nothing downstream
          catches it: neither `Signal` nor the broker's protective arming asks whether a stop is
          reachable. Unreachable on a currency pair, reachable on a crypto flash crash.
        """
        size = block.top - block.bottom
        if size <= ZERO:
            logger.debug("zone at %s has no width; nothing to arm", block.time)
            return None

        close = context.candle.close
        tick = context.instrument.tick_size
        buffer = size * self._stop_buffer

        if block.kind is ZoneKind.DEMAND:
            if close < block.top:
                return None
            # Rounded *away* from the entry, so the stop never ends up nearer the zone than the
            # buffer says. Rounding to nearest would sometimes shave it back onto the edge, which
            # is the one place the buffer exists to keep it off.
            stop = _to_tick(block.bottom - buffer, tick, ROUND_FLOOR)
            if stop <= ZERO:
                logger.debug("zone at %s would need a stop at %s; nothing to arm", block.time, stop)
                return None
            return ZoneEntry(side=Side.LONG, limit_price=block.top, stop_loss=stop)

        if close > block.bottom:
            return None
        return ZoneEntry(
            side=Side.SHORT,
            limit_price=block.bottom,
            stop_loss=_to_tick(block.top + buffer, tick, ROUND_CEILING),
        )

    # ----------------------------------------------------------------------- #
    # Order lifetime                                                           #
    # ----------------------------------------------------------------------- #

    def _still_standing(self, block: OrderBlock) -> bool:
        """Is this zone still one the tracker holds and still usable?

        A zone the detector has dropped is as dead as a mitigated one — it aged out of the window
        the method looks back over, and an order left resting on it would fill off a region
        nothing is watching any more.
        """
        for tracked in self._blocks.zones:
            if tracked.block == block:
                return tracked.usable
        return False

    def _withdraw(self, armed: _Armed, candle: Candle) -> Signal:
        """Take back a named order. Harmless if it never reached the book — a cancel for an order
        the broker does not hold is answered `False`, not raised, because in live that is a race
        (the fill was in flight) rather than a bug."""
        logger.debug("withdrawing %s at %s", armed.client_id, candle.time)
        return Signal(
            kind=SignalKind.CANCEL,
            side=Side.LONG if armed.block.kind is ZoneKind.DEMAND else Side.SHORT,
            reference_price=candle.close,
            reason=f"cancel.{self._name}",
            client_id=armed.client_id,
        )


def _structure_levels(break_: StructureBreak | None) -> tuple[SnapshotLevel, ...]:
    """The broken structure as a drawable segment, or nothing when there is none to draw.

    Labelled by kind — `choch` or `bos` — because the two mean opposite things about trend and a
    reader deciding whether an entry was justified is asking which one this was. The price is
    the level that was crossed: on a bearish break the low that gave way, on a bullish one the
    high, which is exactly the line the author draws by hand.

    `None` on a zone armed with no break in hand. That is a qualifier's prerogative — one may
    name a zone from the tracker on a quiet bar — and a missing line is the honest picture of it.
    """
    if break_ is None:
        return ()
    return (
        SnapshotLevel(
            label=break_.kind.value,
            price=break_.level,
            from_time=break_.level_time,
            to_time=break_.time,
        ),
    )


def _to_tick(price: Money, tick: Money, rounding: str) -> Money:
    """Snap a computed level onto the instrument's price grid.

    A stop is a price someone has to be able to place. Ten percent of a zone's width is not
    generally a multiple of the tick, and a stop at 1.094375 on a five-digit pair is a level that
    does not exist — it would fill in the backtest and be rejected by the venue.
    """
    return (price / tick).to_integral_value(rounding=rounding) * tick
