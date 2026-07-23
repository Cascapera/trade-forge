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

**What this module does not do.** It never closes a position. The stop and the target are the
broker's protective exits (`take_profit_rr`), armed at the fill and left alone. Trailing the stop
— breakeven at the first break in favour, then behind confirmed swings — is a real part of the
method and is deliberately not here: moving the stop of an open position is a new verb on the
`Broker` protocol, so it gets its own ADR and its own review.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from typing import Protocol

from tradeforge_engine.domain import (
    ZERO,
    Candle,
    Context,
    Money,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.structure import (
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    StructureBreak,
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
    ) -> None:
        if stop_buffer < ZERO:
            raise ValueError(f"stop buffer is a fraction of the zone width, got {stop_buffer}")

        self._qualifier = qualifier
        self._name = name
        self._allow_secondary = allow_secondary
        self._stop_buffer = stop_buffer

        self._structure = MarketStructure()
        self._blocks = OrderBlockDetector()
        self._armed: _Armed | None = None
        self._armed_count = 0
        # Zones whose order became a trade. Membership only — never iterated — so it cannot
        # leak set ordering into a result (`AGENTS.md §5.2`).
        self._traded: set[OrderBlock] = set()

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        candle = context.candle
        break_ = self._structure.update(candle)
        marked = self._blocks.update(candle, break_)

        self._observe_fill(context)

        if context.position is not None:
            # It is the broker's stop and target that end a trade — nothing rests and nothing
            # may be armed while this phase's one position is open. Any name still armed here
            # never reached the book (`_observe_fill` already forgot a placed one), and holding
            # it would leave it going stale behind a position it knows nothing about.
            self._armed = None
            return ()

        signals: list[Signal] = []

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
                    )
                )

        return tuple(signals)

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
            self._armed = None

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


def _to_tick(price: Money, tick: Money, rounding: str) -> Money:
    """Snap a computed level onto the instrument's price grid.

    A stop is a price someone has to be able to place. Ten percent of a zone's width is not
    generally a multiple of the tick, and a stop at 1.094375 on a five-digit pair is a level that
    does not exist — it would fill in the backtest and be rejected by the venue.
    """
    return (price / tick).to_integral_value(rounding=rounding) * tick
