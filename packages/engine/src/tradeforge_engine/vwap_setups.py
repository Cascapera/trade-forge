"""The formation both VWAP triggers stand on: where the anchor goes, and how long it lives.

Chapter 11.2 offers two entries — the author calls them **FFFD** and **Botinha** — and they
disagree about exactly one thing: what turns a formation into an order. Everything that happens
before that is one machine, and this is it. Writing the second half once is what keeps two
triggers from becoming two methods, the same argument `setups.py` already makes for its four
qualifiers.

**The region is the territory, not the entry.** The three `ZoneEntryPoint` values all answer
"where does the order rest *on the zone*". These triggers answer something else entirely: the zone
says which stretch of price is worth watching, and the entry comes from a VWAP anchored inside it.
So this is neither a fourth entry point nor a `SetupQualifier` — a qualifier sees one bar and
answers yes or no, and a formation is state crossing many bars, which is ADR-0019's own reasoning
applied one level up.

**Two lines from one anchor.** `AnchoredVWAP` already draws a band: one anchor, one volume, priced
three ways — `hlc3` (the central line), `high` and `low`. The author's *botinha* is that band's
outer line on the side the trade is entered from: `low` under a demand region, `high` over a
supply one. No new indicator. Two instances of the one that existed and had no callers.

**What kills a region here is not the engine's usual rule, and the difference is forced.**
`TrackedZone.mitigated` says a region is spent at the first wick to its entry edge, and
`StructureStrategy._may_arm` refuses a mitigated region. That rule cannot hold for these triggers:
**price entering the region _is_ the formation**, so a zone burned by the touch would burn on the
very bar the setup begins, and neither trigger could ever exist. His rule, given on 2026-09-04:
the region survives being visited, and dies only two ways here — a wick through its far edge, or
the trigger's window running out. (The third death, the fill, is ADR-0015 and belongs to the
strategy, not to this object.)

⚠️ **The death is the region's, not the break's.** One break of structure can leave a primary zone
and secondaries. A formation that expires on the secondary kills *that* region; the primary is
untouched and may still arm its own, for a strategy that trades secondaries at all
(`allow_secondary`). Nothing here reaches across zones.

**Silence when the series carries no volume, and a line of log that says so.** `AnchoredVWAP`
skips a bar with no volume rather than dividing by zero, so a series without the volume the setup
asked for produces `value() is None` forever — no error, no order, and a report reading *zero
trades*, which a human reads as "the strategy found nothing" instead of "the indicator never
existed". Measured across the 144 series on disk: `tick_volume` is present on every bar,
`real_volume` only on the one stock. So `volume="real"` anywhere else is permanent silence. The
decision (his, 2026-09-03) is that silence is the right behaviour — no VWAP, no order, no trade —
and the only thing owed is that it be *findable*, which is the warning `lines()` emits once.
"""

import logging
from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from tradeforge_engine.domain import ZERO, Candle, Money, Side, to_tick
from tradeforge_engine.errors import EngineError
from tradeforge_engine.structure import OrderBlock, ZoneKind
from tradeforge_engine.vwap import AnchoredVWAP

logger = logging.getLogger(__name__)

DEFAULT_BARS_TO_TRIGGER = 7
"""How many bars the trigger has, counted from the bar that confirmed the anchor.

The author's number, and the clock starts at the **confirming** bar rather than at the anchor:
*"após a barra de alta ele tem 7 barras para acionar"*. The two differ whenever the reaction ran
more than one bar — in his own example the anchor is bar 2 and the confirmation bar 3, so an
anchor-based clock would give the trigger six bars, not seven.

⚠️ **A count of bars, so its meaning changes with the timeframe.** Seven bars is under two hours
on M15 and seven days on D1, and nothing here re-decides that. It is injectable for exactly that
reason, and because a rule that can only be exercised by building a seven-bar fixture is a rule
whose off-by-one hides.
"""


class FormationState(StrEnum):
    """How far along the formation is, and the two ways it can be over.

    ⚠️ **`ANCHORED` with `lines()` returning `None` is the volume silence**, and that pair is the
    only way to tell it apart from "not anchored yet", which returns `None` too. Two different
    statements sharing one return value is a smell this names rather than hides: the state is the
    second half of the answer, and the warning is logged only for the first case.
    """

    WATCHING = "watching"
    """The region is marked and price has not reached it. Nothing is being accumulated."""

    FORMING = "forming"
    """Price is in the region and the reaction is running. The anchor candidate moves with every
    new extreme; no bar has closed in the trade's direction yet."""

    ANCHORED = "anchored"
    """The confirming bar closed, both lines are running, and the window is counting down."""

    DEAD = "dead"
    """Either a wick broke the far edge, or the window ran out. The region goes with it."""


@dataclass(frozen=True, slots=True)
class VwapLines:
    """The two lines at one bar's close: the band's centre, and the author's *botinha*.

    Deliberately **not** carrying the entry or the stop. Those are arithmetic on these two
    numbers, they differ between the two triggers, and putting them here would make this object
    claim to know which trigger is reading it.
    """

    vwap: Money
    """The `hlc3` line — the band's centre, and what he means by "a VWAP"."""

    botinha: Money
    """The band's outer line on the side the trade enters from: `low` under a demand region,
    `high` over a supply one. Below `vwap` on a long, above it on a short."""


class VwapFormation:
    """One region's reaction, its anchor, and the two lines that come off it.

    Fed every closed bar from the moment the region is marked. It places no order and emits no
    signal: it answers *where the anchor is*, *what the two lines read*, and *is this still
    alive*. The triggers that turn those three answers into an order are separate.
    """

    __slots__ = (
        "_anchor",
        "_bars_since_confirmation",
        "_bars_to_trigger",
        "_block",
        "_botinha",
        "_last_bar",
        "_pending",
        "_state",
        "_typical",
        "_volume",
        "_warned_silent",
    )

    def __init__(
        self,
        block: OrderBlock,
        *,
        bars_to_trigger: int = DEFAULT_BARS_TO_TRIGGER,
        volume: str = "auto",
    ) -> None:
        if bars_to_trigger < 1:
            raise EngineError(f"a trigger window needs at least one bar, got {bars_to_trigger}")
        self._block = block
        self._bars_to_trigger = bars_to_trigger
        self._volume = volume
        self._state = FormationState.WATCHING
        self._pending: list[Candle] = []
        self._anchor: Candle | None = None
        self._last_bar: Candle | None = None
        # ⚠️ **Both lines are built here, before there is an anchor to give them.** Not because
        # anything reads them yet — nothing does until `_anchor_here` feeds them — but because
        # building them is what *validates* the volume source, and `AnchoredVWAP` owns that list.
        # Deferred to the anchoring bar, `volume="tik"` would construct happily and raise in the
        # middle of a backtest, thousands of bars after the document that asked for it. The window
        # already fails at construction one line above; two knobs on one object with opposite
        # failure policies is the kind of difference nobody remembers at three in the morning.
        source = "low" if block.kind is ZoneKind.DEMAND else "high"
        self._typical = AnchoredVWAP(source="hlc3", volume=volume)
        self._botinha = AnchoredVWAP(source=source, volume=volume)
        self._bars_since_confirmation = 0
        self._warned_silent = False

    @property
    def block(self) -> OrderBlock:
        return self._block

    @property
    def side(self) -> Side:
        """The trade this formation would produce, read off the region rather than passed in."""
        return Side.LONG if self._block.kind is ZoneKind.DEMAND else Side.SHORT

    @property
    def state(self) -> FormationState:
        return self._state

    @property
    def anchor(self) -> Candle | None:
        """The bar both lines are anchored on, or `None` before the confirmation fixed it."""
        return self._anchor

    @property
    def last_bar(self) -> Candle | None:
        """The most recent bar this formation was shown, or `None` before the first one.

        Published so that a trigger comparing the lines against a price cannot be handed the
        wrong bar. `lines()` reflects the accumulation through exactly this candle, so the close
        a level is measured against has to be this candle's — hand it the *next* bar and the
        comparison silently becomes a level decided on N judged against a price from N+1, which
        is lookahead with no symptom at all. Reading it from here rather than accepting it as an
        argument is what makes that mistake unrepresentable instead of merely documented.
        """
        return self._last_bar

    @property
    def bars_left(self) -> int:
        """Bars still available to the trigger. Zero unless the formation is anchored."""
        if self._state is not FormationState.ANCHORED:
            return 0
        return self._bars_to_trigger - self._bars_since_confirmation

    def update(self, candle: Candle) -> None:
        """Advance the formation by one closed bar."""
        # Recorded before anything else can return, because it means "the last bar this formation
        # was shown" and that is true even of a bar it did nothing with. A trigger reading the
        # lines has to compare them against *this* bar's close and no other — see `last_bar`.
        self._last_bar = candle
        if self._state is FormationState.DEAD:
            return

        # ⚠️ **The break is read before anything else, and it outranks every other reading of the
        # bar.** His rule is unconditional — *"romper o 90 em qualquer ponto encerra tudo"* — so a
        # bar that breaks the far edge is not also allowed to move the anchor, confirm a
        # formation, or spend a bar of the window on its way out.
        if self._broke_out(candle):
            logger.debug(
                "formation on the %s at %s died: a wick broke its far edge",
                self._block.kind.value,
                self._block.time,
            )
            self._state = FormationState.DEAD
            return

        if self._state is FormationState.ANCHORED:
            self._accumulate(candle)
            self._bars_since_confirmation += 1
            if self._bars_since_confirmation >= self._bars_to_trigger:
                logger.debug(
                    "formation anchored at %s died: %d bars passed with no entry",
                    self._anchor.time if self._anchor else None,
                    self._bars_to_trigger,
                )
                self._state = FormationState.DEAD
            return

        if self._entered(candle):
            self._offer(candle)
        elif self._state is FormationState.FORMING:
            # Not a candidate — it never reached the region — but it is still a bar between the
            # candidate anchor and whatever confirms, so the accumulation must see it.
            self._pending.append(candle)

        if self._state is FormationState.FORMING and self._confirms(candle):
            self._anchor_here()

    def lines(self) -> VwapLines | None:
        """Both lines at the last bar fed, or `None` when there are none to give.

        ⚠️ **Only an `ANCHORED` formation has lines, and a dead one is not "the last reading".**
        Both averages survive the bar that kills the formation — the objects are still there and
        still hold a perfectly valid number — so a caller that reads `lines()` without checking
        `state` would price an entry off a formation whose window ran out seven bars ago. The
        check belongs here rather than in every trigger that will read this: fail closed once, in
        the object that knows, instead of relying on two future call sites to remember.

        `None` therefore means one of two things, and `state` is what tells them apart: there is
        no live formation, or there is one and the series carries no volume. Only the second is a
        problem, and only the second logs.
        """
        if self._state is not FormationState.ANCHORED:
            return None
        typical, botinha = self._typical.value(), self._botinha.value()
        if typical is None or botinha is None:
            self._warn_silent()
            return None
        return VwapLines(vwap=typical, botinha=botinha)

    def _warn_silent(self) -> None:
        """Say once, out loud, that this setup is mute for want of volume rather than of a signal.

        Once per formation: the condition is a property of the series, so a run that hits it hits
        it on every bar of every region, and a message per bar would bury the first one. A
        `warning` rather than a `debug` because nothing else in the run will look wrong — the
        backtest completes, and reports no trades.
        """
        if self._warned_silent:
            return
        self._warned_silent = True
        logger.warning(
            "VWAP formation on the %s at %s has no value: no %s volume on these bars, so this "
            "setup will place no order and take no trade",
            self._block.kind.value,
            self._block.time,
            self._volume,
        )

    def _entered(self, candle: Candle) -> bool:
        """Did this bar reach into the region?

        His indicator's rule verbatim, the same one `TrackedZone` reads as mitigation —
        `ob.bull ? low <= ob.topo : high >= ob.fundo`. What differs here is only the consequence:
        entering starts a formation instead of spending the region.
        """
        if self._block.kind is ZoneKind.DEMAND:
            return candle.low <= self._block.top
        return candle.high >= self._block.bottom

    def _broke_out(self, candle: Candle) -> bool:
        """Did a wick go past the far edge — 90 under a demand [90, 100]?

        **Wick, not close**, his answer on 2026-09-04 to that exact question.

        ⚠️ **Strictly past, so a wick that stops *on* the edge does not kill.** Touching 90 and
        breaking 90 are different events, and every stop in this engine already sits a buffer
        *past* an edge for the same reason: a level is where price is expected to turn, and the
        noise of the turn reaches the level itself. How it fails if this is the wrong call: a
        formation survives a bar that pierced to exactly the edge, and the trade it later takes
        is one he would have skipped — visible in a backtest, not silent.
        """
        if self._block.kind is ZoneKind.DEMAND:
            return candle.low < self._block.bottom
        return candle.high > self._block.top

    def _confirms(self, candle: Candle) -> bool:
        """Is this the bar that ends the reaction — up under a demand region, down over supply?"""
        if self._block.kind is ZoneKind.DEMAND:
            return candle.close > candle.open
        return candle.close < candle.open

    def _beyond(self, candle: Candle, incumbent: Candle) -> bool:
        """Does `candle` reach further into the region than the current candidate?

        ⚠️ **Strictly further, so a tie keeps the earlier bar.** Two bars sharing the low are the
        same price reached twice, and the first one reached it; keeping the earlier also gives the
        longer accumulation, which is the more conservative of the two readings.
        """
        if self._block.kind is ZoneKind.DEMAND:
            return candle.low < incumbent.low
        return candle.high > incumbent.high

    def _offer(self, candle: Candle) -> None:
        """Take a bar that reached the region as a possible anchor.

        A new extreme **replaces** the candidate and everything after it, because the accumulation
        starts at the anchor: bars before it are not part of this VWAP, and keeping them would
        make the anchor a label rather than a starting point. That is also what bounds this list —
        it can only ever hold the bars since the deepest point of the reaction so far.
        """
        if self._state is FormationState.WATCHING:
            self._state = FormationState.FORMING
            self._pending = [candle]
            return
        if self._beyond(candle, self._pending[0]):
            self._pending = [candle]
        else:
            self._pending.append(candle)

    def _anchor_here(self) -> None:
        """Fix the anchor and run every bar since it through both lines.

        ⚠️ **The anchor bar is in its own average.** His answer on 2026-09-04, asked directly, and
        it moves the number rather than tidying it: on his own example the two readings are
        96.5222 and 96.6333 at the same bar. It is also the reading that makes the anchor mean
        something — the bar that made the extreme is the one whose traders are being watched.

        ⚠️ **The confirming bar may be the anchor.** If the bar that ends the reaction is itself
        the one that reached deepest, that is where the anchor goes — his correction, and the
        opposite of what "the low before the confirmation" would give. Nothing special is needed
        for it: `_offer` runs on this bar before `_confirms` is asked.

        Runs at most once per formation: the only caller reaches it from `FORMING`, and it leaves
        the formation `ANCHORED`, where `update` returns before ever asking again. That is why the
        two averages can be fed from empty here without being reset first.
        """
        anchor = self._pending[0]
        for candle in self._pending:
            self._accumulate(candle)
        self._anchor = anchor
        self._pending = []
        self._bars_since_confirmation = 0
        self._state = FormationState.ANCHORED
        logger.debug("formation anchored on the bar at %s", anchor.time)

    def _accumulate(self, candle: Candle) -> None:
        self._typical.update(candle)
        self._botinha.update(candle)


DEFAULT_ENTRY_MARGIN = Decimal("0.10")
"""How far above the *botinha* the order rests, as a fraction of the gap between the two lines.

His number, and his own example fixes it: with the centre at 100 and the *botinha* at 90 the buy
goes at **91**. So the order is not *on* the lower line, it is a tenth of the band above it — the
band is where he expects the reaction, and an order on its very edge is filled by the noise of
the reaction rather than by the reaction.

⚠️ **The margin and the stop are read off the same gap, and that makes the risk ten times the
margin.** Entry at `botinha + 10%·d` with the stop a whole `d` below it means the trade risks ten
times the distance it waited for. That is his design, not an artefact: the band is the evidence,
so being wrong means the whole band failed, not that the order missed by a tenth.
"""


@dataclass(frozen=True, slots=True)
class BotinhaOrder:
    """Where the order rests and where it dies, both already on the instrument's price grid.

    `risk` is derived from these two rather than from the arithmetic that produced them, and the
    difference is not cosmetic: both levels are snapped to the tick, in *opposite* directions, so
    the unrounded `d` and the distance between the placed levels are different numbers. Sizing,
    the ledger and any later "what did this setup risk?" must all read the gap that was actually
    placed — see the ledger's own rule that a total is derived from the parts as recorded.
    """

    side: Side
    limit_price: Money
    stop_loss: Money

    @property
    def risk(self) -> Money:
        """The distance the position is sized against: the levels as placed, not as computed."""
        return abs(self.limit_price - self.stop_loss)


@dataclass(frozen=True, slots=True)
class BotinhaTrigger:
    """His second entry of chapter 11.2: a limit between the two lines, riding the lower one.

    **It arms the moment the formation anchors.** Unlike `FFFD`, nothing else has to happen — no
    bar has to close on the wrong side of anything. The confirming bar closes, both lines exist,
    and the order belongs on the book.

    **The order chases.** Both lines are cumulative, so every bar that closes moves them, and with
    them `d`, the entry and the stop. This object recomputes; turning a moved level into a cancel
    and a fresh order is the strategy's job, because the protocol already decided how that is
    done — *"not a resting order's planned stop; that one is cancelled and re-placed"* (ADR-0018).

    **It answers `None` for four different reasons, and all four are the same answer: no order
    rests here this bar.** No lines at all, the two lines coincident, a level that would fall at
    or through zero, or an entry that has come out at or above the market. The formation stays
    alive through every one of them — it is the *level* that is unusable on this bar, not the
    setup — and the next bar recomputes from scratch.

    ⚠️ **It reads the bar off the formation rather than accepting one.** The market-side guard
    only means anything when the close it compares against is the same bar the lines were
    accumulated through; an argument would leave a caller free to hand over the next one, and the
    guard would then judge a level decided on N against a price from N+1 — lookahead with no
    symptom at all. The pairing was a documented obligation for exactly one review round before
    it became an impossible mistake instead, which is the difference between a comment and a
    design.
    """

    margin: Decimal = DEFAULT_ENTRY_MARGIN

    def __post_init__(self) -> None:
        # Zero would rest the order exactly on the botinha, and one would rest it on the centre
        # line with a stop a whole band below — the two ends where the entry stops being an entry
        # and starts being one of the two lines it was measured from.
        if not (ZERO < self.margin < Decimal(1)):
            raise EngineError(
                f"the entry margin must lie strictly between 0 and 1, got {self.margin}"
            )

    def order_for(self, formation: VwapFormation, *, tick: Money) -> BotinhaOrder | None:
        """Where the order should rest at this bar's close, or `None` if none should.

        ⚠️ **The bar is read off the formation, not passed in, and that is the whole point.** The
        market-side guard below compares a level against a close, and the two are only comparable
        when the close is the same bar the lines were accumulated through. Taking it as an
        argument left a caller free to hand over the *next* bar — a loop that had already
        advanced, or one keeping "the current candle" in a field — and the guard would then judge
        a level decided on N against a price from N+1 with no symptom whatsoever. Read from the
        formation, that mistake stops being a thing a reviewer has to notice.

        ⚠️ **The rounding is directional and it costs the trade, both times.** The entry is
        rounded *away* from the price it hoped for — up for a buy, down for a sell — so snapping
        to the grid never hands the trade a better fill than the formula offered; the stop is
        rounded *away from the entry*, so it is never nearer than a whole `d`. It is the rule
        `_midpoint` and `_beyond_edge` already run on, and following it here costs up to two ticks
        of extra risk rather than silently shaving the stop.

        ⚠️ **The stop cannot be zero-width, and nothing here has to check it.** The entry is on
        the grid and `d` is positive, so `entry - d` is strictly below the entry and floors to at
        least one tick under it. Were it ever equal, the risk manager returns no size rather than
        dividing by nothing — so the failure mode is a skipped trade, not a division by zero.
        """
        lines = formation.lines()
        candle = formation.last_bar
        # The second half of this test is for the type checker, not for a state that happens:
        # `lines()` only answers when the formation is anchored, and it cannot anchor without
        # having been fed, so a formation with lines always has a last bar. Written as one `or`
        # rather than an `assert` because the honest answer to both is the same — nothing rests
        # here — and an assertion would turn a state this object already handles into a crash.
        if lines is None or candle is None:
            return None

        distance = abs(lines.vwap - lines.botinha)
        if distance <= ZERO:
            # The two lines have met. It happens on a run of bars with no range at all, and there
            # is no band left to measure an entry against — the entry, the stop and the botinha
            # would be one price.
            return None

        side = formation.side
        offset = distance * self.margin
        if side is Side.LONG:
            entry = to_tick(lines.botinha + offset, tick, ROUND_CEILING)
            stop = to_tick(entry - distance, tick, ROUND_FLOOR)
        else:
            entry = to_tick(lines.botinha - offset, tick, ROUND_FLOOR)
            stop = to_tick(entry + distance, tick, ROUND_CEILING)

        # ⚠️ A tall band near zero pushes a long's stop — or a short's entry — at or through
        # nothing. `Signal` refuses a non-positive level outright, so this is caught either way;
        # it is caught *here* so the setup goes quiet rather than raising out of a backtest.
        if entry <= ZERO or stop <= ZERO:
            logger.debug("botinha level at or below zero on the %s; no order", side.value)
            return None

        # ⚠️ **A limit that has come out on the wrong side of the market is not a limit.** A buy
        # limit at or above the close is filled at once at whatever the market offers, which is
        # the market order this entry exists to avoid: the whole point is to wait for price to
        # come *back* to the band. It is reachable — a bar whose low drags the botinha up while
        # closing near that low can put the recomputed entry above its own close — so it fails
        # closed for this bar rather than being argued away.
        crossed = entry >= candle.close if side is Side.LONG else entry <= candle.close
        if crossed:
            logger.debug(
                "botinha entry %s is already through the close %s; no order this bar",
                entry,
                candle.close,
            )
            return None

        return BotinhaOrder(side=side, limit_price=entry, stop_loss=stop)


__all__ = [
    "DEFAULT_BARS_TO_TRIGGER",
    "DEFAULT_ENTRY_MARGIN",
    "BotinhaOrder",
    "BotinhaTrigger",
    "FormationState",
    "VwapFormation",
    "VwapLines",
]
