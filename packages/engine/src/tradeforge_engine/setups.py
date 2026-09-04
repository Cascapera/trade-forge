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
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum
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
    ZoneMark,
    to_tick,
)
from tradeforge_engine.errors import EngineError
from tradeforge_engine.structure import (
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    StructureBreak,
    StructureKind,
    TrackedZone,
    ZoneKind,
)
from tradeforge_engine.vwap_setups import (
    DEFAULT_BARS_TO_TRIGGER,
    DEFAULT_ENTRY_MARGIN,
    BotinhaTrigger,
    FormationState,
    VwapFormation,
)

logger = logging.getLogger(__name__)

DEFAULT_STOP_BUFFER = Decimal("0.1")
"""How far past the zone's far edge the stop sits, as a fraction of the zone's own width.

The author's numbers: a supply zone of [90, 100] is sold from 90 with the stop at 101, and a
demand zone of [90, 100] is bought at 100 with the stop at 89. The zone is ten wide, so the stop
clears it by one — the region is where price is expected to turn, and a stop *on* its edge is
stopped by the noise of the turn itself.
"""


class ZoneEntryPoint(StrEnum):
    """Which of the author's activations this setup uses on a marked region.

    Chapter 11.4 lists three ways to activate an entry *inside* the region and rates them; this
    carries two of those, and the third is deliberately absent. `RETURN_PASS` is not one of them
    at all — it is chapter 11.5, and it enters on the way back **out**.

    ⚠️ **The name is now wider than "where the order rests".** Two of these values answer that
    question; the third answers *when the order is placed and which way price has to cross it*.
    Read as geometry alone it would be a lie — a `RETURN_PASS` order does not rest at a point
    inside the region, it waits above it.

    * `EDGE` — the order on the near edge, the stop past the far one. His model 1, and what this
      engine did exclusively until now. He notes its cost in as many words: *"o tamanho do stop
      acaba sendo maior, equivalente ao tamanho total da região"*. It is the default here for a
      duller reason than merit — changing it would silently move every backtest already recorded.
    * `MIDPOINT` — the order at 50% of the region, the stop still past the far edge. His model 3,
      *"que considero o mais vantajoso"*: roughly half the stop, so the same risk buys more size.
      Its cost is his too — *"muitas vezes o preço não chega aos 50% e acaba indo em direção ao
      nosso alvo sem nos ativar, deixando-nos na pedra"*.

    **The body limit — his model 2 — is not here, and its absence is a data fact rather than an
    opinion.** `OrderBlock` carries `top` and `bottom` only, and those are the marking candle's
    high and low. The body needs that candle's open and close, which the region has never
    recorded; adding it reaches the detector, the persisted snapshot and the API's `Zone`. Both
    values here are arithmetic on the two numbers the region already carries.

    ⚠️ **This is not a free dial to widen.** A fraction was considered and refused: at anything
    approaching the far edge the entry meets the stop, risk goes to the buffer alone, and position
    sizing divides by very nearly nothing. Two named points are two the author has traded.

    `RETURN_PASS` — his chapter 11.5, *"passagem na volta"*, and the odd one out in every way
    that matters:

    * **Nothing is placed when the zone is armed.** The region is marked and named, and the book
      stays empty. What arms the *order* is price coming back and reaching the 50% — his words,
      *"atinge o 50% onde o gatilho anterior ativaria o trade"*, which is why the level is the
      same arithmetic `MIDPOINT` uses rather than a second opinion about where half is.
    * **The order is a stop, not a limit** (ADR-0016). It goes on the near edge plus the same
      buffer the stop already uses — a demand [90, 100] triggers at 101 — and it fills only if
      price *resumes the move that made the region* and passes back out through it. The two
      entries above are bought on the way in; this one is bought on the way out.
    * **Its stop and its cancel are the same level.** 89 for that region: reach it and the order
      is taken back rather than filled, so the setup never opens a trade that would already be
      stopped. On the other two, cancel and stop are unrelated numbers.

    So it is the widest stop of the three — 12 against the edge's 11 and the midpoint's 6 — and
    that is the price of what it buys: it is the only one of the three that does not need price
    to turn while sitting inside the region. It needs price to have already turned.
    """

    EDGE = "edge"
    MIDPOINT = "midpoint"
    # The suppression on the next line is for a false positive: the name ends in PASS, which the
    # security lint reads as a credential. It is a *passage* of price through a region, and the
    # string is a DSL value the schema publishes.
    RETURN_PASS = "return_pass"  # noqa: S105
    BOTINHA = "botinha"


ABANDON_AT_ZONE_HEIGHTS = Decimal(1)
"""How far past the near edge price may run, in multiples of the region's own height, before a
resting order is abandoned.

**The author's rule, given on 2026-09-02, and it exists because `MIDPOINT` broke a coincidence.**
While the order rested on the near edge, the touch that mitigated the region and the touch that
filled the order were the same event, so "the order lives as long as the region does" needed no
second thought. An order at 50% can be left behind: price reaches into the region, turns at 98
without ever trading 95, and walks away. His answer, in his numbers — region [90, 100], height
ten, order at 95: *"quando o preço atingir 110 a ordem deve ser retirada e a entrada neste ponto
deve ser abortada"*. One height above the near edge for a demand region, one below it for supply.

⚠️ **Measured on the region's own scale, not in ticks and not in bars.** A ten-point region and a
hundred-point region are not the same distance from being wrong, and a clock says nothing about
either. The region supplies its own unit, which is the same reasoning `DEFAULT_STOP_BUFFER`
already runs on.

⚠️⚠️ **The clock starts when price enters the region, never when the order is armed** — his
correction, and the first version of this got it backwards. A demand region is *created* by an
upward impulse, so at the moment it is marked price is already leaving it: the level one height
above the top is reached almost at once, and abandoning there would retire every order before
price had any chance to come back. Measured, not reasoned — counting from the arming bar broke 9
of the engine's own tests, all of them edge entries that never got to fill. The rule is about a
region that was *visited and left*, which is precisely the gap `MIDPOINT` opens and the reason
`EDGE` never needed it: on the edge, being visited and being filled are the same event.
"""


MAX_ARMING_ATTEMPTS = 3
"""How many times **in a row** one zone may be armed and turned away before it stops being offered.

⚠️ **In a row, and the word is load-bearing.** An order that is re-armed and then rests in the
book without being refused clears the streak — whatever was turning it away has stopped. Counted
cumulatively instead, three one-minute hiccups spread across thirty-nine hours would retire a
setup exactly as a forty-five-minute outage would; measured on `arms_a_resting_limit`, refusals on
bars 61, 80 and 100 with healthy resting in between burned the zone. That is a different rule from
the one chosen, and the cost quoted below is the cost of *this* one.

⚠️ **A rule about the method, not a safeguard**, and that decides where it lives and who chose
it. The refusals a strategy sees are not only the live ones — `SIZING`, `RISK` and `BROKER` fire
in a backtest too — so this number changes what the strategy does everywhere, not only against a
venue. It is enforced in `_may_arm`, the single chokepoint for *may this region be traded at
all*, beside the rules that say a zone is spent or a region no longer stands.

**Measured, which is why a cap exists at all.** On `arms_a_resting_limit` against a permanent
refusal, the zone is re-armed on **every bar it stands** — 48 in a row (bars 61 to 108), 50 across
the 175. Each attempt puts an order on the wire, writes an `order_audit` row, and mints a fresh
`client_id`; and a re-armed order re-stamps `confirmed_by` from a bar with no break in it, so the
`EntrySnapshot` of whichever attempt finally enters stops drawing the structure that justified it.
The loop does not merely make noise — it degrades the record of the trade it eventually opens.

**Three, chosen by Guilherme on 2026-08-31, with the cost stated rather than hidden.** Three
attempts is three orders instead of forty-eight. Against a *transient* outage — the executor
down, a session that stopped beating — a stoppage lasting longer than three bars burns the zone
over an infrastructure problem that later cleared. Taken knowingly: a setup that stale is usually
not the setup any more.

⚠️ **The cap counts bars; the tolerance it buys is measured in clock time, and the two are only
the same on one timeframe.** Three bars is about 45 minutes on M15 — the timeframe this was
chosen for — but three *minutes* on M1 and three *days* on D1. So the same number is a forgiving
rule on a slow chart and an unforgiving one on a fast chart, and a strategy moved to M1 inherits a
tolerance nobody re-decided. Named here because the fixture that proves the rule runs on **H1**,
where the refusals on bars 61 and 100 are thirty-nine hours apart: reading the test as "45
minutes" would be reading the wrong clock.

⚠️ **A count, deliberately, and not a judgement about *why*.** The plan of record was to cap only
refusals that will not clear on their own, told apart by `RefusedBy` — and a live session on
2026-08-31 falsified that premise. The executor's volume cap arrives as `EXECUTOR`, documented as
*"conditions that change without anybody changing the order"*, and is perfectly permanent: same
capital, same stop, 0.11 lots every time. The other candidate — comparing the repeated `detail` —
builds policy on free text that `Refusal` reserves for humans by name. A count needs neither, and
is deterministic, which this project's second invariant requires of anything a backtest depends
on.
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
        if break_ is not None:
            # **Any** break ends this ladder, not only another change of character. The author's
            # rule: "um choch de baixa cria a zona, se depois ele fizer um bos de baixa, a entrada
            # de choch morre" — the structure moved on, and a region belonging to the break before
            # last is not one he would still sell. A BOS leaves regions of its own but they are
            # the continuation setup's, not this one's, so the ladder empties rather than refills.
            #
            # This was the hole: only a CHoCH used to touch the ladder, so any number of BOS could
            # confirm while an order still rested on a superseded region. Measured over 3480 AAPL
            # H1 candles, 21 of 34 changes of character were followed by at least one BOS before
            # the next one — 53 breaks in all, up to 7 stacked on a single choch.
            self._ladder = (
                list(reversed(context.marked)) if break_.kind is StructureKind.CHOCH else []
            )

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
    """Where an order waits on a zone, and where its stop goes.

    Exactly one of `limit_price`/`stop_price` is set, mirroring the rule `Signal` already
    enforces one layer down: a limit rests on the side price has to come *back* to, a stop on
    the side it has to break *through*, and a price carrying both meanings is a bug rather than
    a richer order. Checked here as well as there so the setup layer fails at the line that
    built the wrong thing, not four frames later.
    """

    side: Side
    stop_loss: Money
    limit_price: Money | None = None
    stop_price: Money | None = None

    def __post_init__(self) -> None:
        if (self.limit_price is None) == (self.stop_price is None):
            raise ValueError("a zone entry waits at a limit or a stop, not both and not neither")


class ZoneActivation(Protocol):
    """How a marked region turns into a resting order, and what takes that order back.

    The setups already share one seam — a `SetupQualifier` answering *which zone is worth
    trading*. This is the second, and it answers *how that zone becomes an order*. It was three
    `if` statements on a `ZoneEntryPoint` for as long as every answer was arithmetic on the
    region's two edges; chapter 11.2 needs one that is a different **shape** rather than a
    different formula — state that crosses bars, and a level that moves while the order rests —
    and a fourth branch on an enum is how a class ends up with four methods that disagree.

    **Two questions, and they are asked at different moments.** `entry_for` is asked while the
    zone holds no order; `expired` is asked on every bar the zone is armed, *placed or not*,
    because a region can die while the book is still empty. Keeping them on one object is what
    stops the pair from drifting: `RETURN_PASS` computes its cancel level with the very call
    `entry_for` uses for the stop, and written on two objects those would agree on every zone
    whose buffer lands on the grid and part company on the first one that does not.

    ⚠️ **`stop_buffer` belongs to the activation, not to the strategy.** It is a number about
    *where the order and its stop go*, which is exactly this seam's subject; leaving it on the
    strategy would mean the one knob every implementation reads is owned by the object that no
    longer knows what any of them do with it.
    """

    def entry_for(self, block: OrderBlock, *, context: Context) -> ZoneEntry | None:
        """The order this zone should rest on this bar, or `None` if it should rest none.

        `None` is not always a refusal. For the two limit entries it means the region cannot
        carry an order at all — no width, or a stop through zero — and will not on any later bar.
        For a triggered entry it is the ordinary state: the region is armed, the book is empty,
        and the same question is asked again next bar.
        """
        ...

    def expired(self, block: OrderBlock, *, context: Context, zones: Sequence[TrackedZone]) -> bool:
        """Has this zone's order stopped making sense on this bar?

        Asked on every bar the zone is armed, whether or not an order ever reached the book —
        which is why it takes the tracked zones rather than reading a mitigation flag off
        anything: the answer for a limit waiting inside the region depends on whether price has
        *been* there, and that fact lives on the detector.
        """
        ...

    def observe(self, block: OrderBlock, *, context: Context) -> None:
        """Fold this bar into whatever state the activation keeps for this zone.

        A no-op for every activation that is a pure function of the region and the bar in front
        of it, which is three of the four. It exists because the fourth is not: a formation is
        state crossing bars, and something has to feed it.
        """
        ...

    def spent(self, block: OrderBlock) -> bool:
        """Has this activation finished with this region for good?

        False for the three that leave a withdrawn region free to be named again — only a *fill*
        spends those (ADR-0015). True for an activation whose own machinery can end: his rule for
        the VWAP triggers is that a window running out takes the region with it.
        """
        ...

    @property
    def spent_by_mitigation(self) -> bool:
        """Does the first touch of the entry edge retire the region for this activation?

        True for the three that wait *on* the region: the touch is the event they were placed
        for. False where entering the region is not the trade but the *start* of the setup —
        retiring the zone there would retire it on the very bar the formation begins.
        """
        ...


def _zone_stop(block: OrderBlock, context: Context, stop_buffer: Decimal) -> Money | None:
    """The stop every activation shares, or `None` for a region that can carry no order.

    Past the edge the trade is *not* travelling towards: below a demand region, above a supply
    one. `_beyond_edge` owns the rounding — away from the zone, so the stop never ends up nearer
    it than the buffer says.

    Two regions answer `None`, and neither will ever answer differently on a later bar:

    * **No width.** The two edges are one price, so the stop would land on the entry and the
      trade would carry no risk at all — which is not a free trade, it is a division by zero in
      position sizing.
    * **A stop at or below zero.** A zone more than ten times as tall as its own floor pushes the
      buffer past nothing, and a stop at a non-positive price is not a wide stop — it is *no
      stop*, because `low <= stop` can never be true. Nothing downstream catches it: neither
      `Signal` nor the broker's protective arming asks whether a stop is reachable. Unreachable
      on a currency pair, reachable on a crypto flash crash.
    """
    size = block.top - block.bottom
    if size <= ZERO:
        logger.debug("zone at %s has no width; nothing to arm", block.time)
        return None
    demand = block.kind is ZoneKind.DEMAND
    stop = _beyond_edge(block, context.instrument.tick_size, size * stop_buffer, upward=not demand)
    if stop <= ZERO:
        logger.debug("zone at %s would need a stop at %s; nothing to arm", block.time, stop)
        return None
    return stop


def _side_of(block: OrderBlock) -> Side:
    return Side.LONG if block.kind is ZoneKind.DEMAND else Side.SHORT


def _limit_entry(
    block: OrderBlock, context: Context, stop_buffer: Decimal, *, at: Money
) -> ZoneEntry | None:
    """A limit resting at `at`, with the shared stop — or `None` for a region that can carry no
    order at all.

    ⚠️ **One function because two activations must refuse the same regions.** `EDGE` and
    `MIDPOINT` differ in exactly one expression, the level; everything else about them — the
    side, the stop, and which zones are impossible — is the same rule. Written twice, the two
    copies of "give up on a zone with no width" are two places to keep in step, and the suite
    would still be green with one of them deleted. That is not hypothetical: extracting this
    seam produced exactly that duplication first, and the coverage report is what caught it.
    """
    stop = _zone_stop(block, context, stop_buffer)
    if stop is None:
        return None
    return ZoneEntry(side=_side_of(block), limit_price=at, stop_loss=stop)


@dataclass(frozen=True, slots=True)
class EdgeActivation:
    """His model 1: the order on the near edge, the stop past the far one.

    The near edge is the side price has to come back to, so it is where the order rests — a
    demand zone is bought at its **top**, a supply zone sold at its **bottom**. He notes the cost
    in as many words: *"o tamanho do stop acaba sendo maior, equivalente ao tamanho total da
    região"*.
    """

    stop_buffer: Decimal = DEFAULT_STOP_BUFFER

    def entry_for(self, block: OrderBlock, *, context: Context) -> ZoneEntry | None:
        near = block.top if block.kind is ZoneKind.DEMAND else block.bottom
        return _limit_entry(block, context, self.stop_buffer, at=near)

    def expired(self, block: OrderBlock, *, context: Context, zones: Sequence[TrackedZone]) -> bool:
        return _ran_away(block, context.candle, zones)

    def observe(self, block: OrderBlock, *, context: Context) -> None:
        """Nothing to fold: this activation reads the region and the bar in front of it."""

    def spent(self, block: OrderBlock) -> bool:  # noqa: ARG002 — the question is the protocol's
        """Never. A withdrawn order leaves its region free to be named again; only a fill spends
        it, and that is the strategy's book to keep (ADR-0015)."""
        return False

    @property
    def spent_by_mitigation(self) -> bool:
        """Yes: this order waits *on* the region, so the touch is the event it was placed for."""
        return True


@dataclass(frozen=True, slots=True)
class MidpointActivation:
    """His model 3, *"que considero o mais vantajoso"*: the order at 50%, the stop unchanged.

    Roughly half the stop, so the same risk buys more size. Its cost is his too — *"muitas vezes
    o preço não chega aos 50% e acaba indo em direção ao nosso alvo sem nos ativar, deixando-nos
    na pedra"*.

    The level is rounded so the tick never hands the trade a price better than the region
    actually offers: a buy up, a sell down. Same doctrine as the stop, which is rounded away from
    the region for the same reason — rounding is not allowed to flatter anything.
    """

    stop_buffer: Decimal = DEFAULT_STOP_BUFFER

    def entry_for(self, block: OrderBlock, *, context: Context) -> ZoneEntry | None:
        half = _midpoint(block, context.instrument.tick_size, _side_of(block))
        return _limit_entry(block, context, self.stop_buffer, at=half)

    def expired(self, block: OrderBlock, *, context: Context, zones: Sequence[TrackedZone]) -> bool:
        return _ran_away(block, context.candle, zones)

    def observe(self, block: OrderBlock, *, context: Context) -> None:
        """Nothing to fold: this activation reads the region and the bar in front of it."""

    def spent(self, block: OrderBlock) -> bool:  # noqa: ARG002 — the question is the protocol's
        """Never. A withdrawn order leaves its region free to be named again; only a fill spends
        it, and that is the strategy's book to keep (ADR-0015)."""
        return False

    @property
    def spent_by_mitigation(self) -> bool:
        """Yes: this order waits *on* the region, so the touch is the event it was placed for."""
        return True


@dataclass(frozen=True, slots=True)
class ReturnPassActivation:
    """His chapter 11.5, *"passagem na volta"* — the only one that waits **outside** the region.

    Nothing is placed when the zone is armed. What arms the *order* is price coming back and
    reaching the 50% — his words, *"atinge o 50% onde o gatilho anterior ativaria o trade"* —
    and the order is then a **stop**, not a limit (ADR-0016), on the near edge plus the same
    buffer the stop already uses. It fills only if price resumes the move that made the region.

    **Its stop and its cancel are the same level**, which is why both come from one
    `_beyond_edge` call rather than two expressions for the same number: reach 89 on a [90, 100]
    demand and the order is taken back rather than filled, so the setup never opens a trade that
    would already be stopped.
    """

    stop_buffer: Decimal = DEFAULT_STOP_BUFFER

    def entry_for(self, block: OrderBlock, *, context: Context) -> ZoneEntry | None:
        stop = _zone_stop(block, context, self.stop_buffer)
        if stop is None:
            return None
        tick = context.instrument.tick_size
        if not _reached_midpoint(block, context.candle, tick):
            return None
        side = _side_of(block)
        buffer = (block.top - block.bottom) * self.stop_buffer
        # ⚠️ **The non-positive check is on the *trigger* here, not only on the stop.** For the
        # two limit entries only a demand zone could push a level through zero, because only its
        # stop sits below the region; a supply zone's stop is above it and safe by construction.
        # This entry point inverts that — a supply zone's *order* is the level below the region —
        # so the far edge of a very tall zone near zero can produce a sell stop at or below
        # nothing, which `Signal` refuses outright.
        trigger = _beyond_edge(block, tick, buffer, upward=side is Side.LONG)
        if trigger <= ZERO:
            logger.debug("zone at %s would trigger at %s; nothing to arm", block.time, trigger)
            return None
        return ZoneEntry(side=side, stop_price=trigger, stop_loss=stop)

    def expired(
        self,
        block: OrderBlock,
        *,
        context: Context,
        zones: Sequence[TrackedZone],  # noqa: ARG002
    ) -> bool:
        """Price reaching the level this order's own stop would occupy.

        ⚠️ `_ran_away` cannot serve here, and that is why this method exists at all: it fires
        when price *leaves* the region upward, and leaving upward is this order's fill.

        No `size <= ZERO` guard, unlike `entry_for`, and the asymmetry is the point: a zone with
        no width can never place an order, so the only question left about it is when to stop
        holding the name. Giving it up on the first bar that reaches its collapsed edge is the
        better of the two answers, and it costs nothing — `_release` sends no cancel for an order
        that never reached the book.

        ⚠️ **Asked on every bar an order is armed, placed or not.** The region can break downward
        while the book is still empty, waiting for a 50% touch that a collapsing market delivers
        on the way to somewhere far below. Answering only for placed orders would leave the name
        armed on a region that is gone, and the next bar that clipped the midpoint would place an
        order into the wreck.
        """
        tick = context.instrument.tick_size
        buffer = (block.top - block.bottom) * self.stop_buffer
        candle = context.candle
        if block.kind is ZoneKind.DEMAND:
            return candle.low <= _beyond_edge(block, tick, buffer, upward=False)
        return candle.high >= _beyond_edge(block, tick, buffer, upward=True)

    def observe(self, block: OrderBlock, *, context: Context) -> None:
        """Nothing to fold: this activation reads the region and the bar in front of it."""

    def spent(self, block: OrderBlock) -> bool:  # noqa: ARG002 — the question is the protocol's
        """Never. A withdrawn order leaves its region free to be named again; only a fill spends
        it, and that is the strategy's book to keep (ADR-0015)."""
        return False

    @property
    def spent_by_mitigation(self) -> bool:
        """Yes: this order waits *on* the region, so the touch is the event it was placed for."""
        return True


@dataclass(slots=True)
class BotinhaActivation:
    """His chapter 11.2 *Botinha*: the order rides the lower line of an anchored VWAP band.

    The first activation that is not arithmetic on the region's two edges. The zone only says
    which stretch of price is worth watching; the entry comes from a formation *inside* it — the
    reaction's extreme anchors two VWAPs, a bar closing in the trade's direction confirms them,
    and the order then rests a tenth of the band above the lower one for seven bars.

    **Three things it needs that the other three do not**, and each is a member of this protocol
    for exactly this one implementation:

    * **It is fed.** `observe` folds every bar into the formation. The other three are pure
      functions of the region and the bar in front of them and do nothing with it.
    * **It spends the region.** A formation whose window ran out, or whose region was broken by a
      wick, takes that region with it — his rule, and the reason `spent` exists. The other three
      leave a region free to be named again after an untouched withdrawal.
    * **Mitigation does not spend it.** Price entering the region *is* the formation, so a zone
      retired by its first touch would be retired on the very bar the setup begins.

    ⚠️ **The region's stop buffer says nothing here.** Every other activation puts its stop a
    fraction of the region's width past the far edge; this one takes it from the band —
    `entry - d`, where `d` is the gap between the two lines. So the knob is not passed to this
    class rather than being passed and ignored, which is the difference between a parameter that
    does nothing and a parameter that is not part of the rule.

    ⚠️ **One formation at a time, matching the one resting order.** The strategy arms one zone at
    a time and this follows it: naming a new zone starts a new formation and drops the old one.
    Regions already spent are remembered by `spent`, which is membership-only — never iterated —
    so it cannot leak set ordering into a result (`AGENTS.md §5.2`).
    """

    bars_to_trigger: int = DEFAULT_BARS_TO_TRIGGER
    volume: str = "auto"
    margin: Decimal = DEFAULT_ENTRY_MARGIN

    _formation: VwapFormation | None = field(default=None, init=False, repr=False)
    _watching: OrderBlock | None = field(default=None, init=False, repr=False)
    _seen: datetime | None = field(default=None, init=False, repr=False)
    _spent: set[OrderBlock] = field(default_factory=set, init=False, repr=False)

    @property
    def spent_by_mitigation(self) -> bool:
        return False

    def spent(self, block: OrderBlock) -> bool:
        return block in self._spent

    def _formation_for(self, block: OrderBlock) -> VwapFormation:
        """This zone's formation, started if the zone is new to us.

        ⚠️ **It always returns one, and that is what keeps the field out of `Optional`.** Both
        public methods reach the formation through here, so there is no order in which one of
        them can find nothing — and therefore no "this cannot happen" branch to write, leave
        untested, and have a reviewer wonder about. The alternative was a guard no call order
        could reach, which is a comment wearing an `if`.
        """
        if self._watching != block:
            self._formation = VwapFormation(
                block, bars_to_trigger=self.bars_to_trigger, volume=self.volume
            )
            self._watching = block
            self._seen = None
        assert self._formation is not None  # noqa: S101 — assigned above; this is for mypy
        return self._formation

    def observe(self, block: OrderBlock, *, context: Context) -> None:
        """Fold this bar into the region's formation, starting one if this zone is new.

        ⚠️ **Idempotent per bar, and it has to be.** The strategy asks this before deciding
        whether a resting order has expired and again after arming, because a zone named on this
        very bar has no formation at the first of those points and an order to price at the
        second. Feeding twice would spend two bars of a seven-bar window on one candle and move
        both lines with a bar counted double — so the bar's own time is what says it has been
        seen. Candle times are unique within a run by construction.
        """
        formation = self._formation_for(block)
        candle = context.candle
        if self._seen == candle.time:
            return
        self._seen = candle.time
        formation.update(candle)
        if formation.state is FormationState.DEAD:
            # His rule, and it is the region that dies rather than the break that revealed it: a
            # formation expiring on a secondary zone leaves the primary free to arm its own.
            logger.debug("botinha formation on the zone at %s is over; spending it", block.time)
            self._spent.add(block)

    def entry_for(self, block: OrderBlock, *, context: Context) -> ZoneEntry | None:
        """Where the order should rest at this bar's close, or `None` if none should.

        ⚠️ **The level moves, and that is what makes this the first activation whose order is
        re-priced.** Both lines are cumulative, so every close shifts them and with them the
        entry and the stop. The strategy compares this answer with what is resting and replaces
        the order when the two differ — which is the mechanism the protocol already settled on
        for a resting order's price: *"not a resting order's planned stop; that one is cancelled
        and re-placed"* (ADR-0018). The three older activations return a constant here, so the
        comparison never fires for them and nothing about them changes.
        """
        order = BotinhaTrigger(margin=self.margin).order_for(
            self._formation_for(block), tick=context.instrument.tick_size
        )
        if order is None:
            return None
        return ZoneEntry(side=order.side, limit_price=order.limit_price, stop_loss=order.stop_loss)

    def expired(
        self,
        block: OrderBlock,
        *,
        context: Context,  # noqa: ARG002 — the formation already folded this bar in
        zones: Sequence[TrackedZone],  # noqa: ARG002
    ) -> bool:
        """The formation ending is what takes the order back, and there is no second rule.

        `_ran_away` cannot serve: it fires when price leaves a region it visited, and this order
        lives *because* price is inside the region working on a reaction. The window running out
        and the far edge breaking are both the formation's own deaths, already counted by
        `observe`, so this asks `spent` rather than reading the same set a second time. "Has
        this activation finished with the region" and "should its order go" are one question
        here, and writing the membership test twice would let one of the two be changed without
        the other, with the suite still green — each is reachable through a different door.
        """
        return self.spent(block)


def activation_for(entry_point: ZoneEntryPoint, *, stop_buffer: Decimal) -> ZoneActivation:
    """The activation a `ZoneEntryPoint` names.

    ⚠️ **A chain rather than a mapping, because the constructors stopped agreeing.** The three
    region-arithmetic activations take the zone's stop buffer; the botinha takes none of it — its
    stop comes from the band rather than from the region, so handing it that number would be a
    parameter that does nothing. A table keyed to one shared signature would have to pretend
    otherwise.

    Loud rather than defaulting, the same doctrine as `build_indicator` and the cost-model
    builder: a value this engine does not know raises instead of quietly running the edge entry.
    The enum makes that unreachable today — which is the argument for keeping the raise, not for
    dropping it, because this chain and the enum are two lists a new member has to be added to
    and only one of them is checked by the type system.
    """
    if entry_point is ZoneEntryPoint.EDGE:
        return EdgeActivation(stop_buffer=stop_buffer)
    if entry_point is ZoneEntryPoint.MIDPOINT:
        return MidpointActivation(stop_buffer=stop_buffer)
    if entry_point is ZoneEntryPoint.RETURN_PASS:
        return ReturnPassActivation(stop_buffer=stop_buffer)
    if entry_point is ZoneEntryPoint.BOTINHA:
        return BotinhaActivation()
    raise EngineError(  # pragma: no cover - unreachable while the enum and the chain agree
        f"no activation for entry point {entry_point!r}"
    )


def _mitigated(block: OrderBlock, zones: Sequence[TrackedZone]) -> bool:
    """Has price already come back and taken this region's near edge?

    The detector's own mark, read rather than recomputed — his indicator owns that rule
    (`low <= top` for demand, `high >= bottom` for supply) and a second copy of it here would be
    one to keep in step.

    ⚠️ **False for a region the tracker has dropped**, which is deliberate but never load
    bearing: the strategy asks `_tracked` first at the one call site, and a dropped region takes
    its order with it regardless of what this would have said.
    """
    return any(tracked.block == block and tracked.mitigated for tracked in zones)


def _ran_away(block: OrderBlock, candle: Candle, zones: Sequence[TrackedZone]) -> bool:
    """Has price cleared the region by a full height, abandoning the order?

    The author's rule (`ABANDON_AT_ZONE_HEIGHTS`), in his own example: a demand region [90, 100]
    with the order at 95, price returns and touches 100, stops at 98 without ever trading 95, and
    turns back up. The order is abandoned when price reaches 110 — one height above the near
    edge. The supply mirror abandons at 80.

    ⚠️ **Two conditions, and the first is the one that is easy to lose.** Price must have
    *entered* the region, and then cleared it by a height. An order waiting on a region price has
    never come back to is not abandoned however far the market travels — it is still waiting for
    exactly the event it was placed for.

    ⚠️ **Read off the bar's extreme, not its close.** The order is a level in the book and the
    market reaching a level is a wick, not a settlement — the same reading his mitigation rule
    uses. A close-based test would keep an order alive through a bar that traded a full height
    clear of it and came back.

    ⚠️ **The withdrawal it triggers takes effect on the next bar, and that is correct rather than
    late.** `loop.py` fills before the strategy runs, so an order the market reached on this same
    bar is already a trade by the time this is asked — and it should be: the abandonment rule is
    about price *never coming to us*, so a bar that traded through the limit filled it, exactly
    as a resting order at a broker would have.
    """
    # ⚠️ Nothing is abandoned before the region has been visited, and this guard is the whole rule
    # rather than an optimisation. See `ABANDON_AT_ZONE_HEIGHTS`: counting from the arming bar
    # abandons every order while price is still walking away from the impulse that marked the
    # zone.
    if not _mitigated(block, zones):
        return False
    height = block.top - block.bottom
    clearance = height * ABANDON_AT_ZONE_HEIGHTS
    if block.kind is ZoneKind.DEMAND:
        return candle.high >= block.top + clearance
    return candle.low <= block.bottom - clearance


def _reached_midpoint(block: OrderBlock, candle: Candle, tick: Money) -> bool:
    """Has price come back into the region as far as its 50% on this bar?

    The trigger for `RETURN_PASS`, and it is deliberately **the same level `MIDPOINT` rests at**
    rather than a second opinion about where half of a region is — his own sentence joins them:
    *"atinge o 50% onde o gatilho anterior ativaria o trade"*. Sharing `_midpoint` is what keeps
    that true: two expressions computing half a region would agree on [90, 100] and diverge on
    the first zone whose midpoint falls between ticks, and the backtest would report a
    `RETURN_PASS` that armed a bar earlier than the `MIDPOINT` it was defined against, with
    nothing to say why.

    ⚠️ **Read off the bar's extreme, not its close** — the same reading `_ran_away` and his
    mitigation rule use. A touch of the 50% is price *trading* there; a bar that dipped to the
    midpoint and closed back at the edge reached it, and a close-based test would ignore the wick
    that is the entire event this setup waits for.
    """
    level = _midpoint(block, tick, Side.LONG if block.kind is ZoneKind.DEMAND else Side.SHORT)
    if block.kind is ZoneKind.DEMAND:
        return candle.low <= level
    return candle.high >= level


@dataclass(slots=True)
class _Armed:
    """The one zone currently holding an order, and whether that order reached the book."""

    block: OrderBlock
    client_id: str
    placed: bool
    entry: ZoneEntry | None = None
    """The order currently resting, so a level that moved can be told from one that did not.

    `None` until something is placed. Three of the four activations return the same level on
    every bar, so this never changes for them and no order is ever re-priced; the botinha's
    moves with its two lines, and comparing against it is the whole of the chase."""

    stem: str = ""
    """The name this zone was first armed under, kept so a re-price derives from it.

    ⚠️ Appending to `client_id` instead would compound: seven bars of chasing turn one name into
    `...-r1-r2-r3-r4-r5-r6`, which is unreadable in an audit row and grows without bound. The
    revision is a number, so the name is the stem plus that number and nothing else."""

    revision: int = 0
    """How many times this zone's order has been re-priced.

    ⚠️ **It exists because a name cannot be reused.** The broker refuses a `client_id` that is
    already resting or has already filled, so a replacement needs its own — and the suffix only
    appears from the first re-price, so every order that is placed once keeps exactly the id it
    had before any of this existed."""
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

    **The order outlives the region being touched, and dies when the market leaves.** It used to
    be simpler — the order's life *was* the zone's life — and that worked only because the entry
    sat on the near edge, where the wick that mitigates a region and the wick that fills the order
    are the same wick. An order at the midpoint is not reached by that wick, so retiring it there
    would make `ZoneEntryPoint.MIDPOINT` a setting that can never produce a trade. Two things end
    a standing order now: price clears the region by a full height (`ABANDON_AT_ZONE_HEIGHTS`), or
    the tracker drops the region and nothing is watching it any more. Only the strategy can know
    either, which is why `Broker.cancel` exists and why the broker is never told about zones
    (`AGENTS.md §5.4`).

    **Mitigation still retires the region, just not the order.** `_may_arm` refuses a mitigated
    zone exactly as before, so a region is still offered once and there is still no path that
    re-arms one. What survives the touch is *this* order, not the right to place another.

    **One trade per zone, ever.** A region whose order filled is finished here, whether the stop,
    the target, or the same bar ended the trade. Without this the machine martingales: a stateful
    qualifier still pointing at the region re-arms it the bar after the stop and buys the same
    level into the same downtrend until the zone finally breaks. The backtest then reports a setup
    that averages down, and the equity curve blames the setup rather than this class.

    **That guard is still redundant, and is kept anyway — read this before deleting it.**
    It stays unreachable under both entry points, but the argument is no longer the one that used
    to be written here. On `EDGE` the fill *is* the touch that retires the region, so the region
    is dead on the fill's own bar. On `MIDPOINT` the two come apart — the touch retires the region
    at the near edge, the fill happens later and deeper — and the region is then dead *earlier*
    than the fill rather than at the same instant. Either way it is already retired when
    `_may_arm` asks, and `_still_standing` refuses it without `_traded` ever being consulted.
    Verified by mutation: deleting the `_traded` check breaks no test, and there is no path where
    only it catches. Two reasons it stays. It states a **different** invariant — one trade per
    region, independent of how regions die — and the rule it currently duplicates is exactly the
    one that just changed under it; and it fails safe, refusing a trade, where the branch this PR
    *did* remove (`_entry_for`'s wait) failed silent, returning `None` and hiding the breakage.

    An earlier version of this paragraph justified the guard with "a zone survives being traded —
    a wick down only marks it flipped, and mitigation wants a close beyond it". Both halves
    described machinery this PR deleted. Wrong documentation on a correctness path becomes wrong
    code again, so it is recorded here rather than quietly corrected.

    The line is drawn at the **fill**, deliberately — the author's rule: placing the order and
    *activating the trade* is what spends a region. A zone whose order was withdrawn untouched,
    superseded by a newer zone before price ever came back, was never traded; if the setup names
    it again it may be armed again. What cannot happen twice is the trade. The fill is observed
    from `Context.fills` (ADR-0015), with the open position as a fallback — never inferred from
    the strategy's own bookkeeping, because the one fill that matters most is the one whose
    position opened and died inside a single bar, invisible to both.
    """

    def __init__(  # noqa: PLR0913 — one qualifier and five knobs, every one keyword-only
        self,
        *,
        qualifier: SetupQualifier,
        name: str = "structure",
        allow_secondary: bool = False,
        stop_buffer: Decimal = DEFAULT_STOP_BUFFER,
        entry_point: ZoneEntryPoint = ZoneEntryPoint.EDGE,
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
        # The second seam: *how* a named zone becomes an order. Built from the entry point
        # rather than branched on later, so a new way of entering is a new implementation
        # instead of a fourth `if` in two methods that would then have to agree.
        self._activation = activation_for(entry_point, stop_buffer=stop_buffer)
        # Where the order rests inside the region. `EDGE` is the default so that adding this
        # parameter changes no recorded result — see `ZoneEntryPoint`.
        self._entry_point = entry_point
        self._breakeven_at_r = breakeven_at_r

        self._structure = MarketStructure()
        self._blocks = OrderBlockDetector()
        self._armed: _Armed | None = None
        self._armed_count = 0
        # Zones whose order became a trade. Membership only — never iterated — so it cannot
        # leak set ordering into a result (`AGENTS.md §5.2`).
        self._traded: set[OrderBlock] = set()
        # How many times each zone has been armed and turned away. Read by `_may_arm` and never
        # iterated, for the same reason `_traded` is not.
        self._refused: dict[OrderBlock, int] = {}
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

    def zones(self) -> Sequence[ZoneMark]:
        """The regions the detector is still holding, as records — see `protocols.Zoned`.

        Translated here rather than handed over: `TrackedZone` is the detector's live
        bookkeeping, and a reader holding it could advance the very machinery it is describing.
        `mitigated_at` is `None` while a region still stands, which is what tells a chart to
        extend the rectangle to its own edge instead of closing it somewhere arbitrary.

        ⚠️ Bounded by the detector's own `_MAX_ZONES`, so a long enough run has marked regions
        this no longer returns. Faithful rather than complete, and faithful is the right one: it
        is exactly the set the setup itself could still have traded.
        """
        return tuple(
            ZoneMark(
                kind=str(tracked.block.kind),
                top=tracked.block.top,
                bottom=tracked.block.bottom,
                from_time=tracked.block.time,
                confirmed_at=tracked.block.confirmed_at,
                mitigated_at=tracked.mitigated_at,
                primary=tracked.block.primary,
            )
            for tracked in self._blocks.zones
        )

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        candle = context.candle
        break_ = self._structure.update(candle)
        marked = self._blocks.update(candle, break_)

        # ⚠️ **The refusal is read first, and one state makes it matter.** `_observe_fill`
        # falls back to "a position is open" as a sign that the armed order became a trade —
        # which it has to, for the fill a live session never sees. But a *refused* order and
        # an open position can coexist: the position is somebody else's, or one this session
        # reconnected into. Reading the fill first would then spend a zone whose order the
        # venue never took, and stamp `_fill_bar` for a trade that does not exist.
        #
        # ⚠️ **The knock-on, so it is not a surprise later:** on that bar `_fill_bar` is now
        # *not* stamped, so `_conduct` may evaluate a breakeven candidate it used to suppress.
        # Benign, and the reason is the guard's own: `_fill_bar` exists so the excursion from
        # *before this phase's own limit filled* is not credited to it — and in this state the
        # phase had no fill at all. The position on the bar belongs to somebody else.
        self._observe_refusal(context)
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

        # ⚠️ **Mitigation no longer takes the order with it, and that is this rule's whole
        # point.** A region is mitigated by the first wick to its near edge; an order resting at
        # the midpoint has not been reached by that wick, and retiring it there would mean the
        # `MIDPOINT` entry could never fill at all. What ends a standing order now is one of two
        # things: the market ran away from the region (`_ran_away`), or the tracker dropped the
        # region entirely and nothing is watching it any more.
        #
        # Arming is unchanged — `_may_arm` still refuses a mitigated region — so the region is
        # still offered exactly once. What survives mitigation is *this* order, not the right to
        # place another.
        #
        # ⚠️ **`RETURN_PASS` ends on a different event, and it is his rule rather than a variant
        # of this one.** `_ran_away` asks whether price left the region on the side the order was
        # never going to be reached from — the right question for a limit waiting *inside*. A
        # return-pass order waits *above* the region, so price leaving upward is its fill, not its
        # abandonment. What kills it is price going the other way to the level its own stop would
        # have sat at: reach 89 on a [90, 100] demand and the order is taken back rather than
        # filled, so the setup never opens a trade that is already stopped.
        # ⚠️ **Fed before it is questioned.** An activation that keeps state has to see this bar
        # before anything asks whether its order still makes sense, because for the botinha the
        # answer *is* the state: the window it counts and the region-break it watches are both
        # folded in here. It is idempotent per bar, so the second call after arming is free.
        if self._armed is not None:
            self._activation.observe(self._armed.block, context=context)

        if self._armed is not None and (
            not self._tracked(self._armed.block)
            or self._activation.expired(
                self._armed.block, context=context, zones=self._blocks.zones
            )
        ):
            signals.extend(self._release(self._armed, candle))
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
                signals.extend(self._release(self._armed, candle))
            self._armed_count += 1
            stem = f"{chosen.kind.value}-{chosen.time:%Y%m%dT%H%M}-{self._armed_count}"
            self._armed = _Armed(
                block=chosen,
                client_id=stem,
                stem=stem,
                placed=False,
                confirmed_by=break_,
            )

        if self._armed is not None:
            # Fed again, for the zone that may have been armed a few lines above: on its first
            # bar it has had no `observe` yet, and a formation with no bars in it prices nothing.
            self._activation.observe(self._armed.block, context=context)
            entry = self._activation.entry_for(self._armed.block, context=context)
            # ⚠️ **A level that has not moved is not a new order.** Three activations answer the
            # same thing on every bar, so this compares equal and nothing is sent — which is why
            # asking them every bar instead of once changes nothing they do. The botinha answers
            # differently as its band shifts, and the difference is the chase: the order resting
            # at the old level is withdrawn and a new one placed at the new one, which is the
            # mechanism the protocol already chose for a resting order's price (ADR-0018).
            if entry is not None and entry == self._armed.entry:
                entry = None
            # ⚠️ **The bar that did the whole move by itself.** A `RETURN_PASS` trigger is a wick
            # into the region, and the same bar can wick in and close back out past the level the
            # order would have waited at — 95 touched and 103 closed, on a [90, 100] demand. The
            # order cannot be placed there: a buy stop at 101 with the market at 103 is already
            # triggered, so it would reach the book as a silent market order and fill at the next
            # open, which ADR-0016 says in as many words. The passage happened inside the bar we
            # only learned about at its close, and it happened without us.
            #
            # ⚠️ **The zone is given up rather than kept waiting**, and that is the honest of the
            # two. Keeping it armed would place the order on some later bar that dips back to the
            # 50% — entering on a break of a level price has already broken, which is a different
            # trade from the one the method describes and would be recorded as the same one.
            if entry is not None and _already_through(entry, candle):
                logger.debug(
                    "zone at %s passed its trigger inside the trigger bar; giving it up",
                    self._armed.block.time,
                )
                signals.extend(self._release(self._armed, candle))
                self._armed = None
                entry = None
            if entry is not None and self._armed is not None:
                if self._armed.placed:
                    # The old order goes back before the new one goes on, and the replacement
                    # carries its own name: the broker refuses a `client_id` that is already
                    # resting, so re-using it would have the venue reject the very order the
                    # re-price exists to send.
                    signals.append(self._withdraw(self._armed, candle))
                    self._armed.revision += 1
                    self._armed.client_id = f"{self._armed.stem}-r{self._armed.revision}"
                self._armed.placed = True
                self._armed.entry = entry
                signals.append(
                    Signal(
                        kind=SignalKind.ENTRY,
                        side=entry.side,
                        reference_price=candle.close,
                        stop_loss=entry.stop_loss,
                        reason=f"entry.{self._name}",
                        limit_price=entry.limit_price,
                        stop_price=entry.stop_price,
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

    def _observe_refusal(self, context: Context) -> None:
        """Stop believing in an order that never reached the book.

        A refusal arrives one bar after the order was asked for — it cannot arrive sooner, see
        `Context.refusals` — and what it means here is narrow and specific: the name this phase
        is holding does not exist anywhere. Not at the broker, not in a book, nowhere.

        ⚠️ **Forgotten, not withdrawn, and the difference is the whole method.** `_withdraw`
        emits a CANCEL because it takes back an order that *is* resting; there is nothing to
        take back here, and a cancel for a name the venue never heard of is a round trip that
        can only be answered "no". Measured on the window in `testing.arms_a_resting_limit`:
        today the phase holds a refused name from bar 61 all the way to bar 109, and then sends
        exactly that phantom cancel.

        ⚠️ **Only a refusal counts against the cap; a withdrawal does not.** `_withdraw` is this
        strategy taking its own order back because it changed its mind — a different zone
        qualified, or the region stopped standing. Nothing turned the order away, so nothing
        about the venue or the account has been learned, and spending an attempt on it would
        retire zones that are working exactly as intended.

        ⚠️ **Unobservable in the measured window, and kept deliberately** — the same standing as
        the `_traded` check in `_may_arm`, which says so in as many words. Probed on
        `arms_a_resting_limit`: each zone is withdrawn at most once (the cancel on bar 109) and
        never re-armed afterwards, so three withdrawals on one region cannot be reached and the
        mutant that counts them **survives the suite**. Recorded rather than deleted on the
        strength of a green run: the day a qualifier flips between two zones repeatedly, counting
        withdrawals would silently retire both.

        ⚠️ **The zone is not spent.** Only a fill spends a region (the class docstring, and the
        author's rule) — a refused order was never a trade, so the region goes back to being
        offerable and the qualifier may name it again. When it does, `_may_arm` mints a **new**
        `client_id` off `_armed_count`, which matters more than it looks: re-offering under the
        old name would be refused a second time as a duplicate, by a broker answering a
        different question, and the retry would never converge.

        ⚠️ **Matched by name, and only against the name currently held.** By the time a refusal
        lands, the phase may have moved on — armed a different zone, or dropped this one when
        its region stopped standing. A refusal for a name it is no longer holding is already
        acted upon, and clearing `_armed` on it would forget an order that really is resting.
        """
        armed = self._armed
        if armed is None or not armed.placed:
            return
        if not any(refusal.client_id == armed.client_id for refusal in context.refusals):
            # ⚠️ **The streak is broken by an order that simply rested**, and this line is the
            # difference between two rules that are easy to confuse. The cap is on refusals *in a
            # row*: a zone whose order was turned away, re-armed, and then sat healthily in the
            # book for eighteen bars has not spent an attempt — whatever refused it went away.
            #
            # Counted cumulatively instead, three one-minute hiccups spread over thirty-nine
            # hours retire a setup exactly as a forty-five-minute outage would, which is not the
            # rule that was chosen and not the cost that was quoted for it. Measured on
            # `arms_a_resting_limit`: refusals on bars 61, 80 and 100 — resting fine in between —
            # burned the zone under the cumulative reading.
            self._refused.pop(armed.block, None)
            return

        self._refused[armed.block] = self._refused.get(armed.block, 0) + 1
        logger.debug(
            "%s never reached the book; attempt %d of %d in a row on this zone",
            armed.client_id,
            self._refused[armed.block],
            MAX_ARMING_ATTEMPTS,
        )
        self._armed = None

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

    def _may_arm(  # noqa: PLR0911 — one flat refusal per rule is the point of the chokepoint
        self, block: OrderBlock
    ) -> bool:
        """May an order be put on this zone at all?

        The single chokepoint, and it is deliberately not spread across the call sites that feed
        it. A qualifier can name any zone it can see, including one it read out of
        `SetupContext.zones` — which is how the flip setup works and which no filter upstream
        touches. Every rule about *whether a region may be traded* is therefore enforced here,
        once, on the zone actually about to be armed.

        Five refusals, in the order they are cheapest to answer:

        * **The zone already armed.** Re-naming it is not a new setup; acting on the repeat would
          withdraw a resting order and put an identical one back a bar later, moving the fill to
          whichever bar the qualifier last repeated itself.
        * **A secondary zone while `allow_secondary` is off.** The flag is a rule about which
          regions may be traded, so it has to bite where the trade is decided.
        * **A zone that no longer stands** — mitigated, or dropped by the tracker. Checking this
          only at the top of the bar would be a bar too late: the broker fills before the strategy
          runs (`loop.py`), so an order armed on a dead zone fills before its cancel is ever sent.
        * **A zone turned away `MAX_ARMING_ATTEMPTS` times in a row.** The cap on re-offering,
          and it belongs here rather than beside the refusal that increments it, for the reason
          the rest of this list exists: every rule about whether a region may be traded is
          enforced at one chokepoint.
        * **A zone the activation has finished with.** Only the VWAP triggers ever say yes:
          his rule is that a formation whose window ran out takes its region with it, which is a
          second way for a zone to die beside the fill below.

        * **A zone that has already given its trade.** Only a *fill* puts a zone in `_traded` —
          an order withdrawn untouched leaves its region free to be named again. Under his
          mitigation rule this check is currently unreachable, because the fill and the touch that
          retires the region are the same event and the region is dead by the time this runs; it
          is kept deliberately, and the class docstring says why. Do not delete it on the strength
          of a green suite alone.
        """
        if self._armed is not None and block == self._armed.block:
            return False
        if not block.primary and not self._allow_secondary:
            return False
        if block in self._traded:
            return False
        if self._refused.get(block, 0) >= MAX_ARMING_ATTEMPTS:
            return False
        # ⚠️ **Subsumed today, and kept because of how it would fail.** A spent region is also a
        # region whose formation is dead, and a dead formation prices nothing — so deleting this
        # line leaves the suite green: the zone is re-armed on every bar the qualifier offers it
        # and quietly places no order. The cost is invisible rather than absent. Each silent
        # re-arm mints a fresh `client_id` and advances the counter every later order is named
        # from, which is the audit noise `MAX_ARMING_ATTEMPTS` exists to stop.
        #
        # And the masking is an accident of one line elsewhere: `_formation_for` reuses the dead
        # formation because the block has not changed. Anyone who later makes it start a fresh
        # one on a re-arm — a reasonable thing to want — hands the region a second seven-bar
        # window, and his rule that the window spends the region is gone with no test to say so.
        # The rule belongs at this chokepoint; the silence is a coincidence.

        if self._activation.spent(block):
            return False
        # ⚠️ **Whether the touch retires the region is the activation's answer, not this
        # method's.** For the three that wait *on* the region it is: the wick that mitigates a
        # zone is the event their order was placed for. For a formation it is the opposite —
        # price entering the region is where the setup *begins* — so retiring the zone there
        # would retire it on the very bar the reaction starts, and neither VWAP trigger could
        # exist at all. What still holds for both is being dropped by the detector: a region
        # nothing is watching any more takes whatever was waiting on it.
        if self._activation.spent_by_mitigation:
            return self._still_standing(block)
        return self._tracked(block)

    # ----------------------------------------------------------------------- #
    # Order lifetime                                                           #
    # ----------------------------------------------------------------------- #

    def _still_standing(self, block: OrderBlock) -> bool:
        """May a *new* order be put on this zone — is it still held, and still unmitigated?

        ⚠️ **This answers the arming question only.** It used to answer both, because under the
        edge entry the two were the same question: the wick that mitigated a region was the wick
        that filled the order resting on its edge, so "may it be armed" and "should the order
        stay" always agreed. They no longer do. A standing order's life is `_tracked` plus
        `_ran_away`; this is what `_may_arm` asks before minting a new one.

        A zone the detector has dropped is as dead as a mitigated one — it aged out of the window
        the method looks back over.
        """
        for tracked in self._blocks.zones:
            if tracked.block == block:
                return tracked.usable
        return False

    def _tracked(self, block: OrderBlock) -> bool:
        """Is the detector still holding this region at all?

        Deliberately *not* `_still_standing`: this asks only whether anything is still watching
        the region, not whether it has been touched. A region that aged out of the tracker's
        window takes its order with it — nothing downstream would notice the region breaking, and
        an order left resting there fills off a level no longer being maintained.
        """
        return any(tracked.block == block for tracked in self._blocks.zones)

    def _release(self, armed: _Armed, candle: Candle) -> tuple[Signal, ...]:
        """Give up an armed zone: a cancel if its order reached the book, silence if it never did.

        ⚠️ **The silence is new, and `RETURN_PASS` is why.** A cancel naming an order the broker
        never received is answered `False` rather than raised, which is why every withdrawal used
        to be sent unconditionally — under the two limit entries the order is placed on the arming
        bar, so an unplaced zone being given up was a rarity, and the tolerated no-op covered it.

        A return-pass zone is armed with **nothing on the book at all**, for as many bars as price
        takes to come back, and most of them never get an order. Sending a cancel for each one
        turns a rare, meaningful race — *the fill was in flight* — into steady noise on the one
        channel where that race is reported, and in live each is a refusal recorded against an id
        the venue can only say it has never heard of.

        Backtests do not move: the broker answered `False` to exactly these cancels before.
        """
        if not armed.placed:
            logger.debug("giving up %s, which never reached the book", armed.client_id)
            return ()
        return (self._withdraw(armed, candle),)

    def _withdraw(self, armed: _Armed, candle: Candle) -> Signal:
        """Take back a named order that reached the book. Still harmless if it did not — a cancel
        for an order the broker does not hold is answered `False`, not raised, because in live
        that is a race (the fill was in flight) rather than a bug. `_release` is the caller that
        decides which of the two this is."""
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


def _beyond_edge(block: OrderBlock, tick: Money, buffer: Money, *, upward: bool) -> Money:
    """One buffer clear of a region's edge, on the price grid, rounded away from the region.

    ⚠️ **One function because three callers must never disagree.** The same two levels are asked
    for under three names: the stop of every entry (a buffer past the edge the trade is *not*
    travelling towards), the `RETURN_PASS` trigger (a buffer past the edge it is), and the level
    that takes a `RETURN_PASS` order back — which *is* that order's stop, and is why this one
    matters most. Written twice, the cancel and the stop agree on every zone whose buffer lands
    on the grid and part company on the first one that does not, and nothing downstream would
    report it: the backtest simply loses a trade whose stop was never touched and files it as a
    cancellation.

    The rounding is directional for the reason it is everywhere else in this file, and the two
    directions are one rule — *away from the region*. A level above is rounded up and a level
    below is rounded down, so snapping to the grid never brings a stop nearer the zone than the
    buffer says, nor hands a breakout a level easier to break than the region actually offers.
    """
    if upward:
        return to_tick(block.top + buffer, tick, ROUND_CEILING)
    return to_tick(block.bottom - buffer, tick, ROUND_FLOOR)


def _midpoint(block: OrderBlock, tick: Money, side: Side) -> Money:
    """Half of a region, on the price grid, rounded so it never flatters the buyer or seller.

    ⚠️ **One function because two callers must never disagree.** `MIDPOINT` rests an order here;
    `RETURN_PASS` uses the very same level as the trigger that places its order, on the author's
    own wording — *"atinge o 50% onde o gatilho anterior ativaria o trade"*. Written twice they
    would agree on every round number and part company on the first zone whose half falls between
    ticks, and nothing downstream would report the disagreement: one backtest would simply arm a
    bar earlier than the other and call it a different model.

    The rounding is directional for the reason it is everywhere else in this file — a buy is
    rounded up and a sell down, so snapping to the grid never hands the trade a better price than
    the region actually offers.
    """
    half = block.bottom + (block.top - block.bottom) / 2
    return to_tick(half, tick, ROUND_CEILING if side is Side.LONG else ROUND_FLOOR)


def _already_through(entry: ZoneEntry, candle: Candle) -> bool:
    """Would this order reach the book already triggered?

    Only a stop entry can be, and only on the bar that placed it: a buy stop is a level *above*
    the market, so a close already above it is an order with nothing left to wait for. `Signal`
    refuses that as a sign error (ADR-0016) and is right to — an already-triggered stop is a
    market order wearing a level's clothes, filling at the next open against a price the trade
    was never sized for. Asked here so the setup declines to build one, rather than raising four
    frames down where the bar that explains it is gone.

    A limit entry is answered `False` without looking: it rests on the side price has to come
    back to, and the invariant that keeps it there is argued at `_entry_for`.
    """
    if entry.stop_price is None:
        return False
    if entry.side is Side.LONG:
        return candle.close >= entry.stop_price
    return candle.close <= entry.stop_price
