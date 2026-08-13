"""The swing family: enter on the break of the candle the average has just qualified.

The structure setups (`setups.py`) enter on a **pullback** — price comes back to a zone and a
limit order fills at its edge. The swing setups (Larry Williams / Stormer — 9.1, 9.2, 9.3, 9.4,
Ponto Contínuo) enter on the **breakout**, the opposite direction: an average qualifies a bar, and
the order rests a tick past that bar's edge waiting for price to break through it. That is a stop
order (ADR-0016), the geometric mirror of the limit — so this is a different machine from the one
in `setups.py`, and it lives in its own module for exactly that reason.

**Two setups live here and they qualify the bar differently.** For the MME9 breakout the event is a
bar *closing across* the average. For the Ponto Contínuo it is a bar that *touches* the average and
closes back on the trend's side, after price has corrected twice — a pullback setup by shape, still
entered on the breakout of its own high. The shared part is the geometry of the order they leave
behind, which is `_breakout_entry`, and nothing else: the two hold different state, watch different
averages, and conduct their trades by different rules.

**"The average turned" is a closed bar, not a slope.** The author's rule, confirmed: a bar that
closes *above* the MME9 has turned it up; a bar that closes *below* has turned it down. There is
no minimum slope and no look at the average's own direction — the close relative to the average is
the whole signal. So there is no new "rising"/"falling" operator here; the turn is `close > ema`,
which the engine already knows how to compute.

**The reference candle is the setup.** Its high is where a long enters, its low is where the long
is protected — the whole bar is the trade. Risk is the bar's own range, and the position is sized
against it (ADR-0016 sizes from `stop_price`, not the close that decided the order).

**One trade per turn.** A turn begins when price crosses the average; while the order has not
filled, each further bar on the setup's side becomes the new reference and the trigger follows it,
up or down — the author's "vale sempre a última barra". Once the order fills, that turn is spent:
no second entry until price closes back across the average and crosses again. Without this the
setup re-enters every bar of a trend it already owns. The boundary is read on **every** bar, open
trade or not: a turn that begins while the previous trade is still running is a real turn, and the
first bar to find the account flat may arm it.

**The trade is conducted, and a close back across the average is not an exit.** That reading is
the obvious one and it is wrong: a bar closing back under the MME9 does not leave the trade, it
*tightens the stop* to just past that bar's far side. If price never takes that low and climbs
back over the average, the stop stays exactly where it was put — it does not resume following.
The other event is touching twice the risk, which brings the stop to the entry price.

Both rules are live from the fill and the tighter one wins, which needs no arbitration: the engine
refuses to loosen a stop (ADR-0018), so the strategy only ever asks for the tighter level. Between
events the stop does not move at all — there is no bar-by-bar trailing here.

So the position always ends *at a level*, never at market: whichever comes first, the conducted
stop or the broker's take-profit (`take_profit_rr`, ADR-0018 is what made the stop side possible).
The author trades this setup against a target of **five times the risk**, but that number is the
broker's knob and not the strategy's — the target belongs to the account, the same way it does for
the structure setups, and hard-coding it here would give every run of this module an opinion the
engine deliberately keeps outside it.
"""

import logging
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

from tradeforge_engine.conduction import StructuralTrail, breakeven_candidate, tighten
from tradeforge_engine.domain import (
    SNAPSHOT_BARS_BEFORE,
    ZERO,
    Candle,
    Context,
    InstrumentSpec,
    Money,
    Side,
    Signal,
    SignalKind,
    SnapshotPoint,
    SnapshotSeries,
)
from tradeforge_engine.indicators import EMA, SMA
from tradeforge_engine.protocols import Indicator
from tradeforge_engine.structure import MarketStructure, StructureBreak

logger = logging.getLogger(__name__)

AverageKind = Literal["EMA", "SMA"]
"""Which mean the Ponto Contínuo measures the pullback against — exponential or arithmetic."""


@dataclass(frozen=True, slots=True)
class _Breakout:
    """Where the stop order rests, and where its protective stop goes."""

    stop_price: Money
    stop_loss: Money


@dataclass(slots=True)
class _Armed:
    """The reference candle currently holding an order, and its name."""

    reference: Candle
    client_id: str


def _breakout_entry(
    candle: Candle, instrument: InstrumentSpec, *, side: Side, buffer_ticks: Decimal
) -> _Breakout | None:
    """The stop order a reference candle places, or `None` if it would carry no risk.

    The whole bar is the trade: a long breaks the **high** and is protected at the **low**, and the
    short mirrors it. The buffer, in ticks, pushes the protective stop past the candle's edge — a
    stop sitting *on* the low is taken out by the noise of the very bar that set it. Zero is the
    literal rule of both setups here, and the knob exists because a real desk usually wants a tick.

    Shared by the two setups in this module because it is the one thing they agree on. What each
    does *not* share is which bar gets here: the MME9 sends the bar that closed across the average,
    the Ponto Contínuo the bar that touched it and closed back. That difference is the method; this
    function is the plumbing.
    """
    if candle.high <= candle.low:
        logger.debug("reference bar at %s has no range; nothing to arm", candle.time)
        return None

    buffer = buffer_ticks * instrument.tick_size
    if side is Side.LONG:
        stop_loss = candle.low - buffer
        if stop_loss <= ZERO:
            logger.debug("reference bar at %s would need a stop <= 0; nothing", candle.time)
            return None
        return _Breakout(stop_price=candle.high, stop_loss=stop_loss)
    return _Breakout(stop_price=candle.low, stop_loss=candle.high + buffer)


class _AverageTrail:
    """The last readings of an average, kept so an entry can carry its own curve.

    A moving average is not a level. Recording only the number it held at the decision draws it
    as a horizontal line, which loses everything the method is about — where price came back to
    it, at what angle, how far it had run away first.

    **The strategy has to be the one keeping it.** An average carries the whole run's history:
    an EMA seeded thousands of bars ago is not the EMA of the last fifty, so a reader
    recomputing one from the snapshot's window would draw a curve that never passes through the
    value the entry was actually judged against. Plausible, and wrong.

    Bounded to the same span as the snapshot window (`SNAPSHOT_BARS_BEFORE`) — the same argument
    as the loop's buffer, and the reason the constant lives in `domain` rather than in the loop:
    a strategy sizing its buffer against the event loop's private constant would be backwards.

    Warming-up bars are skipped rather than recorded as holes. An average has no value until its
    period has filled, and the window can reach back before that, so the curve simply begins
    where the indicator did.
    """

    def __init__(self, label: str = "average") -> None:
        self._label = label
        self._points: deque[SnapshotPoint] = deque(maxlen=SNAPSHOT_BARS_BEFORE + 1)

    def record(self, candle: Candle, value: Money | None) -> None:
        """Fold this bar's reading in. Call on **every** bar, before any early return.

        Called conditionally, the buffer would skip the bars that emitted nothing and the curve
        would be drawn compressed — the points still landing on their true times, so the shape
        would be wrong without a single one of them being in the wrong place.
        """
        if value is None:
            return
        self._points.append(SnapshotPoint(time=candle.time, value=value))

    def series(self) -> tuple[SnapshotSeries, ...]:
        """The curve as a snapshot series, or nothing at all while the average is warming up."""
        if not self._points:
            return ()
        return (SnapshotSeries(label=self._label, points=tuple(self._points)),)


def _withdraw(armed: _Armed, candle: Candle, *, side: Side, name: str) -> Signal:
    """Take back a named order. Harmless if it never reached the book — a cancel for an order the
    broker does not hold is answered `False`, not raised (a live race, not a bug)."""
    logger.debug("withdrawing %s at %s", armed.client_id, candle.time)
    return Signal(
        kind=SignalKind.CANCEL,
        side=side,
        reference_price=candle.close,
        reason=f"cancel.{name}",
        client_id=armed.client_id,
    )


class Mme9BreakoutStrategy:
    """Enter on the break of the candle that closed across the MME9 (ADR-0016).

    A directional setup: one instance trades one side. `side=LONG` arms on a bar that closes
    *above* the average and enters on the break of its high; `side=SHORT` mirrors it below. The
    two-sided version is a composition of two of these, and is left for later — a single-side
    instance is what a golden test can read.

    **One live order at a time**, like the structure machine, and for the same reason: several
    resting orders would turn "why did this backtest take that trade" into a question about which
    of them won a race in a list. A new reference replaces whatever was resting.

    **The order's life is the turn's life.** While price stays on the setup's side the order
    waits; the bar price closes back across the average the order is withdrawn — the setup is
    gone, and only the strategy can know that, which is why `Broker.cancel` exists. The cancel is
    applied the same bar it is decided, before the next bar can fill (loop step 3), so a withdrawn
    order cannot be taken on a break the setup no longer wanted.

    **`breakeven_at_r` is the only rule here that is a number.** The author trades "2x1" — touch
    twice the risk and the stop goes to the entry price — and that is the default, but it is the
    one part of the setup a search can legitimately move, so it is a parameter rather than a
    constant. `None` switches the rule off entirely, which is not a degenerate setting: taking a
    winner to breakeven is a known way to turn it into a scratch, and "what would this setup earn
    without that" has to be askable for the answer to mean anything. The average's rule has no
    such knob, because it is not a number — it is the event that defines the conduction.
    """

    def __init__(
        self,
        *,
        side: Side = Side.LONG,
        period: int = 9,
        name: str = "mme9",
        stop_buffer_ticks: int = 0,
        breakeven_at_r: Decimal | None = Decimal(2),
    ) -> None:
        if period < 1:
            raise ValueError(f"MME period must be >= 1, got {period}")
        if stop_buffer_ticks < 0:
            raise ValueError(f"stop buffer is a magnitude in ticks, got {stop_buffer_ticks}")
        if breakeven_at_r is not None and breakeven_at_r <= ZERO:
            raise ValueError(f"breakeven R multiple must be positive, got {breakeven_at_r}")

        self._side = side
        self._name = name
        self._stop_buffer_ticks = Decimal(stop_buffer_ticks)
        self._breakeven_at_r = breakeven_at_r
        self._ema = EMA(period=period, source="close")
        # Kept for the chart's label. The indicator does not expose its own period, and a label
        # rebuilt from the setup's JSON elsewhere would be a second place holding this number.
        self._period = period
        self._trail_of_the_average = _AverageTrail()

        self._armed: _Armed | None = None
        self._armed_count = 0
        # A turn is spent once its order has traded: no new entry until price closes back across
        # the average and crosses again. Set when a fill is observed, cleared by the first bar
        # that closes on the wrong side of the average — which is exactly a turn ending.
        self._spent = False

    def overlays(self) -> Mapping[str, Indicator]:
        """The average this setup is defined by — see `protocols.Charted`."""
        return {f"EMA {self._period}": self._ema}

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        candle = context.candle
        # The average tracks every bar, open position or not: the turn that ends a trade and the
        # cross that starts the next one are both read off a live MME9.
        self._ema.update(candle)
        average = self._ema.value()
        # Recorded on every bar, before any branch below can return early — see `_AverageTrail`.
        self._trail_of_the_average.record(candle, average)

        self._observe_fill(context)

        if average is None:
            # The MME9 is still warming up: there is no average to have closed across yet.
            return ()

        # Which side of the average did this bar close on — the setup's, or against it?
        on_side = candle.close > average if self._side is Side.LONG else candle.close < average

        # The open trade's stop, if either rule tightened it on this bar. Computed before the
        # branches below because both of them owe it: a bar that closes back across the average
        # is *itself* one of the two events that move the stop, and it is also the bar that ends
        # the turn — the same candle doing two unrelated jobs.
        conducted = self._conduct(context, closed_across=not on_side)

        if not on_side:
            # A close back across the average ends the turn — and it ends it whether or not a
            # trade is still open. The average does not know we are positioned: if price closed on
            # the wrong side while the trade ran and then crossed back, that is a *new* turn, and
            # the first bar to find the account flat is entitled to arm it. Reading this boundary
            # only when flat would swallow every turn that began inside a trade, and swallow it
            # silently — no number comes out wrong, trades simply stop appearing.
            signals: list[Signal] = [] if conducted is None else [conducted]
            if self._armed is not None:
                signals.append(self._withdraw(self._armed, candle))
                self._armed = None
            self._spent = False
            return tuple(signals)

        if context.position is not None:
            # Nothing arms beside an open trade, and nothing is resting either: `_observe_fill`
            # drops the armed name on any bar that shows a position. What this bar can still owe
            # is a tightening — the 2R touch does not care which side of the average we closed on.
            return () if conducted is None else (conducted,)

        if self._spent:
            # Still on the setup's side, but this turn already gave its trade. Wait for the cross.
            return ()

        entry = self._entry_for(candle, context.instrument)
        if entry is None:
            # A bar with no range is no reference — its high and low are one price, so the trigger
            # would sit on the stop and the trade would carry no risk. Keep whatever is resting and
            # wait for a bar that has a range.
            return ()

        # This bar is the reference (the cross, or a rearm that follows it). Replace any resting
        # order — the trigger tracks the latest bar, up or down.
        signals = []
        if self._armed is not None:
            signals.append(self._withdraw(self._armed, candle))
        # The counter, not the timestamp, is what makes the name unique. Below M1 — S30, ticks —
        # two references share a minute, and a repeated name is one the broker already holds in
        # `_consumed`: the second order would be refused in silence while the strategy believed
        # itself armed.
        self._armed_count += 1
        client_id = f"{self._name}-{candle.time:%Y%m%dT%H%M}-{self._armed_count}"
        self._armed = _Armed(reference=candle, client_id=client_id)
        signals.append(
            Signal(
                kind=SignalKind.ENTRY,
                side=self._side,
                reference_price=candle.close,
                stop_loss=entry.stop_loss,
                stop_price=entry.stop_price,
                reason=f"entry.{self._name}",
                client_id=client_id,
                # The level that made this bar a reference. The trigger and the stop are on the
                # order already; the average is the one number in the decision that no column
                # downstream would otherwise carry, and without it a chart of this entry shows
                # a bar breaking out of nothing in particular.
                context={"average": average},
                # And the same average as the curve it actually is. The scalar above is what a
                # later "does this only fire far from the average?" aggregates; this is what
                # gets drawn. Neither can be derived from the other.
                series=self._trail_of_the_average.series(),
            )
        )
        return tuple(signals)

    def _conduct(self, context: Context, *, closed_across: bool) -> Signal | None:
        """The open trade's stop, moved by whichever rule is tighter — or `None` to leave it.

        Two events, and **neither of them closes the position**:

        * **Touching `breakeven_at_r` times the initial risk** puts the stop at the entry price
          (the author's own multiple is 2). Touching, not closing: the bar's high reaching the
          level is the event. Nominal breakeven — with costs, leaving at the entry price is a
          small loss rather than nothing. `None` means the rule is off and only the average
          conducts.
        * **A bar closing back across the average** puts the stop just past that bar's far side.
          If price never takes that low and climbs back over the average, the stop *stays* — it
          does not resume following, and it never loosens.

        Between events, nothing. There is no bar-by-bar trailing in this setup.

        **The tighter rule wins, and no code here decides that.** The engine refuses a loosening
        outright (ADR-0018), so the strategy simply never asks for one — it compares against
        `position.stop_loss`, the level actually in force, and stays quiet when its candidate is
        not an improvement. Remembering what it last sent instead would be a second copy of the
        broker's state, and the day the two disagree the engine raises and the backtest dies.

        **The bar that fills can also conduct**, and that is the rule applied literally rather
        than an oversight. On a bar whose stop order fills and which then closes back across the
        average, the low the stop moves to may have printed *before* the fill — so the stop can
        land at a price the trade never actually carried. No future data is read (the bar is
        closed, and the level cannot execute until the next one), and refusing to conduct here
        would mean inventing a rule the author did not state.

        **`initial_stop_loss` is what makes 2R computable at all.** After the first tightening,
        `stop_loss` is no longer the level the lot was sized against — measure the risk from it
        and the 2R line drifts closer with every move, arming breakeven earlier and earlier on a
        trade that never actually reached it.
        """
        position = context.position
        if position is None:
            return None

        candle = context.candle
        candidates: list[Money] = []

        breakeven = breakeven_candidate(
            position=position, side=self._side, candle=candle, multiple=self._breakeven_at_r
        )
        if breakeven is not None:
            candidates.append(breakeven)

        # The average. The buffer is the entry's own, and deliberately so: a stop sitting exactly
        # on the low is taken out by the noise of the bar that set it, and on a trade that has
        # already run in our favour that is a worse way to lose than being wrong.
        if closed_across:
            buffer = self._stop_buffer_ticks * context.instrument.tick_size
            candidates.append(
                candle.low - buffer if self._side is Side.LONG else candle.high + buffer
            )

        return tighten(
            position=position,
            side=self._side,
            candle=candle,
            candidates=candidates,
            reason=f"trail.{self._name}",
        )

    def _observe_fill(self, context: Context) -> None:
        """Notice the armed order becoming a trade, and spend the turn.

        Two signs, either enough, the same pair the structure machine reads (ADR-0015): the bar's
        own fills carrying the armed name — the only sign that survives a position that opens and
        dies inside one bar — and, as a fallback, an open position for a fill the strategy was
        never shown. Forgetting `_armed` is part of it: the order is not resting any more, and
        keeping the name would cancel an order the trade already consumed.
        """
        armed = self._armed
        if armed is None:
            return
        filled = any(fill.order.client_id == armed.client_id for fill in context.fills)
        if filled or context.position is not None:
            self._spent = True
            self._armed = None

    def _entry_for(self, candle: Candle, instrument: InstrumentSpec) -> _Breakout | None:
        return _breakout_entry(
            candle, instrument, side=self._side, buffer_ticks=self._stop_buffer_ticks
        )

    def _withdraw(self, armed: _Armed, candle: Candle) -> Signal:
        return _withdraw(armed, candle, side=self._side, name=self._name)


_AVERAGES: Final[dict[str, type[EMA] | type[SMA]]] = {"EMA": EMA, "SMA": SMA}


class PontoContinuoStrategy:
    """Ponto Contínuo: buy the bar that came back to the average, touched it, and closed above it.

    The author's rule, dictated and then pinned question by question (`side=LONG` described; the
    short mirrors every line of it):

    1. **Any bar is a reference.** It is not the signal — it only says whether the bar after it is a
       *correction*, which means a strictly lower high **and** a strictly lower low.
    2. **Two consecutive corrections qualify the pullback.** Not one, not none: his choice.
    3. **The trigger is a bar that touches the average and closes above it.** Touching may pierce —
       going through the average is still touching. That same bar is the whole setup: the order
       enters at its **high** and is protected at its **low**, a stop order (ADR-0016).
    4. **A bar closing below the average cancels the setup** — the resting order *and* the
       qualification with it, so the next entry needs two fresh corrections.
    5. **Entries repeat, one trade at a time**, and the setup re-arms once a trade ends — whether it
       ended at the stop or at the target.

    **The qualification latches, and that is his answer rather than an inference.** Once two
    corrections have happened the pullback stays qualified: the triggering bar does not itself have
    to be a correction, and ordinary bars may sit between the correction and the touch. Requiring an
    unbroken run right up to the trigger would throw away the strongest reversal bars, which
    routinely print a *higher* high on the day they take the average back. It is also the geometry
    he already validated once: `MarketStructure` — now a transcription of his own indicator —
    arms a BOS on the same two-bar counter-move and then lets it stand, so that a bar which is not
    a correction cannot disarm what is already armed. The worked example in `test_structure.py`
    is where that is pinned: two corrections arm the top, an ordinary bounce follows, and the bar
    after it breaks anyway.

    **A close *on* the average is neither event.** Above triggers, below cancels, and equal does
    neither — the rule is stated with strict words in both directions, so a bar closing exactly on
    the mean leaves a qualified pullback exactly as it found it. Note this is deliberately *not* how
    the MME9 breakout reads the same tie, where anything short of a close above ends the turn: there
    the average's side is the whole signal, here it is only a boundary.

    **Nothing is counted before the average exists.** Corrections during the warmup are real
    geometry, but the rule that *cancels* a qualification needs a level to compare against — so
    counting them would let a pullback qualify across a stretch of chart where its own canceller was
    not running, and be spent by the first touch after the warmup. A qualification never outlives
    the rule that can kill it.

    **While the order rests, the latest qualifying bar owns it.** A second touch-and-close-above
    replaces the order on the newer bar, up or down, like the rest of the swing family. In practice
    this can only move the trigger *down*: a later bar whose high cleared the resting trigger would
    have filled it on the way through.

    **The trade is conducted structurally, not by the average — his choice, made with the cost of
    it on the table.** So this setup carries a `MarketStructure` of its own and shares
    `StructuralTrail` with the SMC setups: breakeven on the first break of structure in the trade's
    favour, then the stop behind each later break's `origin`, with the 2x1 rule alive alongside and
    the tighter of them winning. The average that *found* the trade has no say in how it is left.

    **This setup does not need the guard that makes the structure family sit out the bar it filled
    on.** That guard exists because a *limit* order fills where price came back to, so the bar's
    favourable excursion can be entirely before the fill. A stop order fills going *through* its
    level in the direction of travel: the favourable extreme of that bar is necessarily after the
    fill, so reading it credits nothing the trade did not live through.
    """

    _MIN_CORRECTION: Final = 2

    def __init__(  # noqa: PLR0913 — keyword-only; each names one knob of the setup
        self,
        *,
        side: Side = Side.LONG,
        period: int = 20,
        average: AverageKind = "EMA",
        name: str = "ponto-continuo",
        stop_buffer_ticks: int = 0,
        breakeven_at_r: Decimal | None = Decimal(2),
    ) -> None:
        if period < 1:
            raise ValueError(f"average period must be >= 1, got {period}")
        if stop_buffer_ticks < 0:
            raise ValueError(f"stop buffer is a magnitude in ticks, got {stop_buffer_ticks}")
        if breakeven_at_r is not None and breakeven_at_r <= ZERO:
            raise ValueError(f"breakeven R multiple must be positive, got {breakeven_at_r}")
        # `.get` rather than a branch on the literal: the type says only two strings are legal, but
        # this constructor is what a wiring layer reading JSON will call, and a mistyped kind has to
        # raise rather than quietly fall through to whichever average the `else` branch held.
        build = _AVERAGES.get(average)
        if build is None:
            raise ValueError(f"unknown average {average!r}; expected one of {sorted(_AVERAGES)}")

        self._side = side
        self._name = name
        self._stop_buffer_ticks = Decimal(stop_buffer_ticks)
        self._breakeven_at_r = breakeven_at_r
        # Exponential by default — his answer, and the one consistent with the rest of the swing
        # line, which is MME throughout. The arithmetic mean of the same period is slower, so it
        # sits further from price and asks for a deeper correction before it is touched.
        self._average = build(period=period, source="close")
        # Both kept for the chart's label: unlike the MME9 setup, which average this one uses is
        # itself a parameter, so the label has to say *which* as well as how long.
        self._period = period
        self._average_kind = average
        self._trail_of_the_average = _AverageTrail()

        self._previous: Candle | None = None
        self._corrections = 0
        # The latch: two corrections have happened and no close below the average has undone them.
        self._qualified = False
        self._armed: _Armed | None = None
        self._armed_count = 0
        # Structure, tracked on every bar whether or not a trade is open — a break of structure is
        # a fact about the chart, and the trail needs the ones that land mid-trade.
        self._structure = MarketStructure()
        self._trail = StructuralTrail()

    def overlays(self) -> Mapping[str, Indicator]:
        """The average the corrections are counted against — see `protocols.Charted`."""
        return {f"{self._average_kind} {self._period}": self._average}

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        candle = context.candle
        self._average.update(candle)
        average = self._average.value()
        # Recorded on every bar, before any branch below can return early — see `_AverageTrail`.
        self._trail_of_the_average.record(candle, average)
        break_ = self._structure.update(candle)
        previous, self._previous = self._previous, candle

        self._observe_fill(context)

        conducted = self._conduct(context, break_)
        signals: list[Signal] = [] if conducted is None else [conducted]

        if average is None:
            return tuple(signals)

        # The two closes the rule names, tested independently rather than as each other's negation.
        # They are not complements: a bar closing *on* the average is neither, and reading the
        # trigger as "did not close below" would silently make the tie an entry.
        long = self._side is Side.LONG
        closed_beyond = candle.close > average if long else candle.close < average
        closed_short_of = candle.close < average if long else candle.close > average

        if closed_short_of:
            # The setup is over: the order comes back and the qualification goes with it. This bar
            # becomes the new reference — a reference is any bar, and the count starts after it.
            if self._armed is not None:
                signals.append(self._withdraw(self._armed, candle))
                self._armed = None
            self._corrections, self._qualified = 0, False
            return tuple(signals)

        # The qualification as it stood *coming into* this bar is what lets this bar trigger; a
        # correction this bar happens to be counts toward the next one. That ordering is the BOS's
        # ("`_up_armed and close > top`" is tested before the correction is counted): the bar that
        # completes the pullback is part of it, not the reversal out of it.
        qualified = self._qualified
        self._track_correction(candle, previous)

        if (
            context.position is not None
            or not qualified
            or not closed_beyond
            or not self._touches(candle, average)
        ):
            return tuple(signals)

        entry = _breakout_entry(
            candle, context.instrument, side=self._side, buffer_ticks=self._stop_buffer_ticks
        )
        if entry is None:
            # A bar with no range is no reference: its high and low are one price, so the trigger
            # would sit on the stop and the trade would carry no risk. The qualification stands —
            # this bar failed to be a setup, it did not destroy one.
            return tuple(signals)

        if self._armed is not None:
            signals.append(self._withdraw(self._armed, candle))
        # The counter, not the timestamp, makes the name unique: below M1 two references share a
        # minute, and a repeated name is one the broker already holds — the second order would be
        # refused in silence while the strategy believed itself armed.
        self._armed_count += 1
        client_id = f"{self._name}-{candle.time:%Y%m%dT%H%M}-{self._armed_count}"
        self._armed = _Armed(reference=candle, client_id=client_id)
        signals.append(
            Signal(
                kind=SignalKind.ENTRY,
                side=self._side,
                reference_price=candle.close,
                stop_loss=entry.stop_loss,
                stop_price=entry.stop_price,
                reason=f"entry.{self._name}",
                client_id=client_id,
                # The average this bar touched and closed back above — the pullback's own
                # definition, and the level a reader needs to see the touch happen.
                context={"average": average},
                # And the curve it belongs to. In this setup the shape is the point: the whole
                # rule is price running away, coming *back* to the line, and turning off it —
                # which a single horizontal level cannot show.
                series=self._trail_of_the_average.series(),
            )
        )
        return tuple(signals)

    def _touches(self, candle: Candle, average: Money) -> bool:
        """Did this bar reach the average? Piercing it counts — going through is still touching."""
        return candle.low <= average if self._side is Side.LONG else candle.high >= average

    def _track_correction(self, candle: Candle, previous: Candle | None) -> None:
        """Fold this bar into the correction count, and qualify the pullback once two have run.

        A correction is a strictly lower high **and** a strictly lower low than the bar before (the
        mirror for a short). A bar that is not one breaks the streak — but a qualification already
        earned stands, which is the latch the class docstring argues for.
        """
        if previous is None:
            return
        corrected = (
            candle.high < previous.high and candle.low < previous.low
            if self._side is Side.LONG
            else candle.high > previous.high and candle.low > previous.low
        )
        if not corrected:
            self._corrections = 0
            return
        self._corrections += 1
        if self._corrections >= self._MIN_CORRECTION:
            self._qualified = True

    def _conduct(self, context: Context, break_: StructureBreak | None) -> Signal | None:
        """The open trade's stop, moved by whichever rule is tighter — or `None` to leave it.

        Two rules, neither of which closes the position: the structural trail (breakeven on the
        first break of structure in the trade's favour, leg origins after it) and touching
        `breakeven_at_r` times the initial risk. Both live from the fill, and no code here decides
        which wins — the engine refuses a loosening (ADR-0018), so `tighten` simply never asks for
        one. Between events the stop does not move; there is no bar-by-bar trailing here.
        """
        position = context.position
        if position is None:
            return None

        candle = context.candle
        candidates: list[Money] = []

        breakeven = breakeven_candidate(
            position=position, side=self._side, candle=candle, multiple=self._breakeven_at_r
        )
        if breakeven is not None:
            candidates.append(breakeven)

        structural = self._trail.candidate(position=position, break_=break_)
        if structural is not None:
            candidates.append(structural)

        return tighten(
            position=position,
            side=self._side,
            candle=candle,
            candidates=candidates,
            reason=f"trail.{self._name}",
        )

    def _observe_fill(self, context: Context) -> None:
        """Notice the armed order becoming a trade, and spend the pullback that found it.

        Two signs, either enough, the pair the whole engine reads (ADR-0015): the bar's own fills
        carrying the armed name — the only sign that survives a position opening and dying inside
        one bar — and an open position as the fallback. Forgetting `_armed` is part of it: the order
        is not resting any more, and keeping the name would cancel an order the trade consumed.

        **The fill spends the qualification**, so the next entry needs two fresh corrections. His
        rule says entries repeat and re-arm when a trade ends, not that one pullback may be traded
        twice — and every other setup in this engine spends its trigger at the fill for the same
        reason (`StructureStrategy`: one trade per zone, ever). Corrections found *while* the trade
        runs still count, so a pullback that completes mid-trade is a real one, and the first bar to
        find the account flat may act on it.

        **Zeroing `_corrections` here cannot change any outcome today, and it is kept anyway.** The
        bar that fills can never be a correction: the order rests at the last qualifying bar's high,
        and the bar before the fill either *is* that bar or failed to reach it, so the filling bar's
        high is `>=` the previous bar's — while a correction needs a *strictly* lower one (mirrored
        for a short, on the lows). The streak rule therefore zeroes the count on this very bar
        anyway, by a different line, and dropping the assignment leaves a mutant no test can kill.

        That proof covers the `filled` signal. The fallback — `position is not None` — coincides
        with the fill bar only while **one instance owns the account**, and that is the assumption a
        two-sided setup breaks first: compose a long and a short of this class over one account (the
        MME9 docstring already contemplates that composition) and the long sees a position opened by
        the short, burning its qualification on a bar that is not its own fill and may perfectly
        well be a correction. This line carries no weight today and will carry it that day, which is
        why it stays — and why the two flags are reset together rather than one at a time.
        """
        armed = self._armed
        if armed is None:
            return
        filled = any(fill.order.client_id == armed.client_id for fill in context.fills)
        if filled or context.position is not None:
            self._armed = None
            self._corrections, self._qualified = 0, False

    def _withdraw(self, armed: _Armed, candle: Candle) -> Signal:
        return _withdraw(armed, candle, side=self._side, name=self._name)
