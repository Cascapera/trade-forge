"""The swing family: enter on the break of the candle the average has just qualified.

The structure setups (`setups.py`) enter on a **pullback** — price comes back to a zone and a
limit order fills at its edge. The swing setups (Larry Williams / Stormer — 9.1, 9.2, 9.3, 9.4,
Ponto Contínuo) enter on the **breakout**, the opposite direction: an average qualifies a bar, and
the order rests a tick past that bar's edge waiting for price to break through it. That is a stop
order (ADR-0016), the geometric mirror of the limit — so this is a different machine from the one
in `setups.py`, and it lives in its own module for exactly that reason.

**The setups here qualify the bar differently, and that difference is the whole method.** For the
MME9 breakout the event is a bar *closing across* the average. For the Ponto Contínuo it is a bar
that *touches* the average and closes back on the trend's side, after price has corrected twice — a
pullback setup by shape, still entered on the breakout of its own high. For the published 9.1
(`Mme9TurnStrategy`) it is the average's own **slope** changing sign — which on an exponential
average turns out to be the same event as his close, so what makes the two different setups is
what they do *after* arming. The shared part is the geometry of the order they leave behind, which
is `_breakout_entry`, and nothing else: each holds different state, watches a different event, and
conducts its trades by its own rules.

⚠️ **Two setups here are both called 9.1 and they are not the same setup.** `Mme9BreakoutStrategy`
is the author's, dictated; `Mme9TurnStrategy` is the one the Larry Williams literature publishes.
They arm on different events, they re-price differently, and they are cancelled by different
things. Keeping both is the point — the pair is comparable, and comparing them is a study.

**The four paragraphs that follow are the author's 9.1 and the Ponto Contínuo.** The published
9.1 answers three of them differently, and `Mme9TurnStrategy` is where it says so.

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

from tradeforge_engine.average_setups import AverageEntryPoint, PatternOrder, PatternWatch
from tradeforge_engine.bar_setups import GiftStop
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
    order: PatternOrder | None = None
    """What a pattern entry has resting, so a level that moved can be told from one that did
    not. `None` for the classic breakout, whose order is re-priced by replacing the reference."""


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


def _long_label(period: int) -> str:
    """The filter's curve name. One function so the trail and the property cannot drift."""
    return f"long EMA {period}"


class LongAverageFilter:
    """His optional direction filter: enter only on one side of a long average (2026-09-09).

    Dictated the day after the higher-timeframe filter, for the same two setups: *"como filtro ele
    só compraria acima da média longa ou venderia abaixo da média longa, o período da média o
    usuário pode escolher"*. Exponential, like the averages the setups themselves are defined by,
    and off unless a period is named — *"os filtros são opcionais"*.

    ⚠️ **What is compared is the price the order would ENTER at, not the bar's close**, and that is
    his answer rather than the obvious reading. Put to him with the case that separates the two — a
    bar closing at 99 under a long average of 100, whose high breaks at 101 — he chose the entry:
    *"a barra que fecha a mme9 pra cima, se a entrada ocorre acima da média longa já conta neste
    caso mesmo o fechamento sendo abaixo"*. The rule is about where the trade begins, so a bar that
    is still under the line but whose break carries price over it is a buy above the long average.
    Read on the close instead, this setup would skip exactly the entries that cross it.

    ⚠️ **It gates placing an order; it never withdraws one.** *"ela fica, só retira se o setup
    desconfigurar"* — losing the long average is not the setup coming apart, and what already
    ended an order goes on ending it (a close back across the setup's own average, a pattern's
    window running out). So a blocked bar leaves whatever rests exactly where it is, which for the
    classic entry means the order stops chasing the newest bar until the filter allows again.

    ⚠️ **An open trade is not touched.** *"trade aberto conduz independente da média longa"*: the
    conduction is the setup's, as it is for every trigger he has added.

    **`allows` is false while the average is warming up**, and that is a reading of ours rather
    than his rule (`specs/backlog.md`). With no value there is nothing to be above, so a filter
    that let the entry through would be answering a question it cannot answer — and a period of
    200 on a short backtest would then trade its first 199 bars unfiltered, which is the silent
    version of the mistake. What it costs is visible instead: a run that arms nothing early on.
    """

    def __init__(self, *, period: int, side: Side) -> None:
        if period < 1:
            raise ValueError(f"long average period must be >= 1, got {period}")
        self._period = period
        self._side = side
        self._ema = EMA(period=period, source="close")
        self._trail = _AverageTrail(label=_long_label(period))

    @property
    def label(self) -> str:
        """How the curve is named on a chart and in an entry's snapshot.

        ⚠️ **"long" is in the name so the two curves cannot collide.** A setup filtered by an
        average of its own period — `Mme9BreakoutStrategy(period=9, long_average_period=9)`, which
        is a legal thing to ask for and a reasonable first experiment — would otherwise offer
        `overlays()` two entries under one key, and a chart would draw one curve where two were
        meant. The curves happen to be identical in that case, so nothing would look wrong.
        """
        return _long_label(self._period)

    @property
    def indicator(self) -> Indicator:
        """The live average, for `Charted.overlays` — the caller must not drive it."""
        return self._ema

    def update(self, candle: Candle) -> None:
        """Fold one closed bar in. Call on **every** bar, before any branch can return early: an
        average fed only on the bars that reached it is a different average."""
        self._ema.update(candle)
        self._trail.record(candle, self._ema.value())

    def value(self) -> Money | None:
        return self._ema.value()

    def series(self) -> tuple[SnapshotSeries, ...]:
        """The long average as a curve, for the entry's picture. Empty while it is warming up."""
        return self._trail.series()

    def allows(self, entry: Money) -> bool:
        """May an order entering at this price be placed?

        Strictly beyond the average on the setup's side — a reading of ours, and the one the rest
        of this module already uses for "above the average" (`candle.close > average`). An entry
        landing exactly on the line is neither above nor below it, and the strictness costs a
        trade nobody would notice either way.
        """
        average = self._ema.value()
        if average is None:
            return False
        return entry > average if self._side is Side.LONG else entry < average


def _reconcile_pattern(  # noqa: PLR0913 — one host's whole identity, passed field by field
    watch: PatternWatch,
    context: Context,
    *,
    average: Money,
    side: Side,
    name: str,
    entry_point: AverageEntryPoint,
    armed: _Armed | None,
    count: int,
    series: tuple[SnapshotSeries, ...],
    long_average: LongAverageFilter | None = None,
) -> tuple[list[Signal], _Armed | None, int]:
    """Fold this bar into the pattern's clock and reconcile the book with what it wants resting.

    Three answers, and the clock gives them by returning an order or `None`: the order already
    resting (nothing to send), a different one (withdraw, then place), or none at all (withdraw).
    `PatternOrder` is a frozen dataclass of `Decimal`s, so the comparison is numeric and a level
    that did not move is never re-sent — the rule the structure machine applies to its own
    activations, arriving here for the same reason.

    **Shared by the two hosts because the reconciliation is the one thing they agree on.** They
    disagree about everything before it — when a turn begins, whether two corrections are needed,
    what ends the setup — and about nothing after. Written twice it would be two copies of the
    withdraw-compare-place dance to keep in step, and the suite would stay green with one of them
    wrong; `_breakout_entry` a few lines above exists for the same reason and says so.

    ⚠️ **The fields are passed rather than the host**, which is why the signature is long. A
    `Protocol` covering `_armed`, `_armed_count`, `_name`, `_side` and the rest would describe a
    client nobody else could ever implement, and it would put five private attributes into a
    published shape. Returning the two mutated values instead keeps the ownership where it is: the
    strategy still holds its own order and its own counter, and this function only says what to do.

    ⚠️ **Only the classic entries re-price by re-reading the bar.** A pattern's levels are fixed
    when its decisive bar closes, and the clock only offers a new one once the previous order is
    gone, so `armed.order != wanted` is false while both exist. The branch stays for the day a
    pattern that chases arrives — the botinha, on an average — which is where it would land.
    """
    wanted = watch.observe(context.candle, average, tick=context.instrument.tick_size)
    candle = context.candle
    signals: list[Signal] = []
    if armed is not None and wanted != armed.order:
        signals.append(_withdraw(armed, candle, side=side, name=name))
        armed = None
    if wanted is None or armed is not None:
        return signals, armed, count
    if long_average is not None and not long_average.allows(wanted.price):
        # His filter, on the price the order would enter at (`LongAverageFilter`). Placed here
        # rather than in each host because the hosts disagree about everything *before* the
        # order and about nothing after — the same argument this function exists for.
        #
        # ⚠️ **After the withdrawal above, not before it.** A pattern whose order the clock has
        # given up on is withdrawn whether or not the filter would allow a new one: that
        # withdrawal is the pattern's own two-bar window running out, which is the setup coming
        # apart in his words, and the filter has no opinion about it.
        logger.debug("%s wants %s, which the long average does not allow", name, wanted.price)
        return signals, armed, count

    count += 1
    client_id = f"{name}-{candle.time:%Y%m%dT%H%M}-{count}"
    armed = _Armed(reference=candle, client_id=client_id, order=wanted)
    signals.append(
        Signal(
            kind=SignalKind.ENTRY,
            side=side,
            reference_price=candle.close,
            stop_loss=wanted.stop_loss,
            stop_price=wanted.stop_price,
            limit_price=wanted.limit_price,
            reason=f"entry.{name}.{entry_point.value}",
            client_id=client_id,
            context=_entry_context(average, long_average),
            series=series + _long_series(long_average),
        )
    )
    return signals, armed, count


def _entry_context(average: Money, long_average: "LongAverageFilter | None") -> dict[str, Money]:
    """The levels this decision was judged against, as scalars for the entry's record.

    The setup's own average always; the long one only when the filter is on, because a key that
    is present and empty says something different from a key that is absent — and what a later
    "does this only work far from the long average?" aggregates over is the pair.
    """
    if long_average is None:
        return {"average": average}
    value = long_average.value()
    # Reachable only in principle: the filter refuses every entry while it has no value, so an
    # order that got this far has one. Written as a total function anyway, because the caller
    # that eventually asks for the context on a bar with no order would otherwise crash here.
    return {"average": average} if value is None else {"average": average, "long_average": value}


def _long_series(long_average: "LongAverageFilter | None") -> tuple[SnapshotSeries, ...]:
    """The long average as a second curve on the entry's picture, when the filter is on.

    A scalar cannot show what the filter did: the whole question a reader has is whether price was
    running away from the long average or curling back to it, and that is a shape.
    """
    return () if long_average is None else long_average.series()


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

    **`entry_point` chooses how the turn is entered, and his four bar patterns replace the
    classic breakout rather than joining it** (2026-09-07: *"substitui, a entrada tem que seguir
    o gatilho escolhido"*). With a pattern, the bar that touches the average — or the one after
    it — has to print the pattern, the order is the pattern's own (its stop twenty percent of the
    bar, not the reference's low, so `stop_buffer_ticks` says nothing about it), and it lives two
    bars. What does **not** change is everything the turn already owned: the close across the
    average that ends it and withdraws whatever rests, the fill that spends it, and the conduction
    — *"a condução segue o padrão do setup, não do gatilho"*. See `average_setups.PatternWatch`.

    **`long_average_period` is his optional direction filter** (2026-09-09): with it set, an order
    is only placed when the price it would *enter* at is beyond a long exponential average — above
    it for a buy, below it for a sell. It gates placing and never withdraws, and an open trade is
    conducted exactly as before. `LongAverageFilter` carries the rule and his answers.
    """

    def __init__(  # noqa: PLR0913 — keyword-only; each names one knob of the setup
        self,
        *,
        side: Side = Side.LONG,
        period: int = 9,
        name: str = "mme9",
        stop_buffer_ticks: int = 0,
        breakeven_at_r: Decimal | None = Decimal(2),
        entry_point: AverageEntryPoint = AverageEntryPoint.CLASSIC,
        gift_stop: GiftStop = GiftStop.GIFT,
        volume_filter: bool = False,
        long_average_period: int | None = None,
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
        self._entry_point = entry_point
        # The pattern's clock, or nothing for the classic entry — which needs none, because its
        # reference is re-read on every bar of the turn. Built here rather than branched on later,
        # so the two ways of entering are two objects and not a flag inside `on_bar`.
        self._watch: PatternWatch | None = (
            None
            if entry_point is AverageEntryPoint.CLASSIC
            else PatternWatch(
                entry_point=entry_point,
                side=side,
                gift_stop=gift_stop,
                volume_filter=volume_filter,
            )
        )
        self._ema = EMA(period=period, source="close")
        # His direction filter, or nothing. Built here rather than branched on later, so "the
        # filter is off" is an object that does not exist instead of a flag read in three places.
        self._long: LongAverageFilter | None = (
            None
            if long_average_period is None
            else LongAverageFilter(period=long_average_period, side=side)
        )
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
        # Which side the previous bar closed on, so the bar that *crosses* can be told from the
        # bars that follow it. Only the pattern entry reads it — see `_arm_pattern`.
        self._was_on_side = False

    def overlays(self) -> Mapping[str, Indicator]:
        """The averages this setup reads — see `protocols.Charted`.

        The one it is defined by, and the long one when the filter is on: a chart that drew only
        the MME9 would show entries being skipped with nothing on it to say why.
        """
        overlays: dict[str, Indicator] = {f"EMA {self._period}": self._ema}
        if self._long is not None:
            overlays[self._long.label] = self._long.indicator
        return overlays

    def on_bar(  # noqa: PLR0911 — one flat return per rule of the setup, like `_may_arm`
        self, context: Context
    ) -> tuple[Signal, ...]:
        candle = context.candle
        # The average tracks every bar, open position or not: the turn that ends a trade and the
        # cross that starts the next one are both read off a live MME9.
        self._ema.update(candle)
        average = self._ema.value()
        # Recorded on every bar, before any branch below can return early — see `_AverageTrail`.
        self._trail_of_the_average.record(candle, average)
        # And the filter's own average, on every bar for the same reason: one fed only on the
        # bars that reached it is a different average, and its curve would be drawn compressed.
        if self._long is not None:
            self._long.update(candle)

        self._observe_fill(context)

        if average is None:
            # The MME9 is still warming up: there is no average to have closed across yet.
            return ()

        # Which side of the average did this bar close on — the setup's, or against it?
        on_side = candle.close > average if self._side is Side.LONG else candle.close < average
        # ⚠️ **`_was_on_side` starts `False`, so the first bar the average can judge at all is
        # read as a crossing.** Deliberate: before it there is no turn to belong to — the average
        # was warming up, and a bar that finds itself on the setup's side without having crossed
        # anything is the beginning of the record, not the middle of a move. The pattern entry
        # therefore skips that one bar (the classic entry does not, because it needs no touch).
        # Same shape as `aquecimento-e-backtest-descartado`: what the warmup produces is not a
        # setup, it is the absence of history.
        crossing = on_side and not self._was_on_side
        self._was_on_side = on_side

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
            # The pattern's clock ends with the turn: *"depois de fechar abaixo, precisa de virada
            # nova"*. The next bar on the setup's side is that new turn, and a touch on it counts.
            if self._watch is not None:
                self._watch.reset()
            return tuple(signals)

        if context.position is not None or self._spent:
            # Nothing arms beside an open trade, and nothing is resting either: `_observe_fill`
            # drops the armed name on any bar that shows a position. What this bar can still owe
            # is a tightening — the 2R touch does not care which side of the average we closed on.
            # And a spent turn — still on the setup's side, its trade already given — waits for
            # the cross; with no position there is nothing to conduct, so it returns nothing.
            if self._watch is not None:
                self._watch.reset()
            return () if conducted is None else (conducted,)

        if self._watch is not None:
            return self._arm_pattern(self._watch, average, context, crossing=crossing)

        entry = self._entry_for(candle, context.instrument)
        if entry is None:
            # A bar with no range is no reference — its high and low are one price, so the trigger
            # would sit on the stop and the trade would carry no risk. Keep whatever is resting and
            # wait for a bar that has a range.
            return ()

        if self._long is not None and not self._long.allows(entry.stop_price):
            # His filter, on the price the break would enter at rather than on this bar's close
            # (`LongAverageFilter`). Returning early is what makes it *place* nothing: the
            # withdrawal below never runs, so an order already resting stays where it is — *"ela
            # fica, só retira se o setup desconfigurar"*. The cost is that this turn stops
            # chasing the newest bar while the filter says no, and that is the same rule read
            # from the other side.
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
                # The levels that made this bar a reference. The trigger and the stop are on
                # the order already; the average is the one number in the decision that no column
                # downstream would otherwise carry, and without it a chart of this entry shows
                # a bar breaking out of nothing in particular. The long average joins it when the
                # filter is on, because then it is half of why this entry was allowed.
                context=_entry_context(average, self._long),
                # And the same averages as the curves they actually are. The scalars above are
                # what a later "does this only fire far from the average?" aggregates; this is
                # what gets drawn. Neither can be derived from the other.
                series=self._trail_of_the_average.series() + _long_series(self._long),
            )
        )
        return tuple(signals)

    def _arm_pattern(
        self, watch: PatternWatch, average: Money, context: Context, *, crossing: bool
    ) -> tuple[Signal, ...]:
        """This bar's turn at the pattern's clock, once the turn itself has been judged.

        The reconciliation is `_reconcile_pattern`, shared with the Ponto Contínuo. What belongs
        here is the one question this host answers on its own:

        ⚠️ **The bar that crosses the average is the turn, not a touch.** Its range spans the
        average by construction — it opened on one side and closed on the other — so read as a
        touch it would open the window on every turn, and a bar of force crossing upward followed
        by any large bar would arm an "ignored bar" the method never pictured. His third answer,
        *"qualquer encostada vale, inclusive a primeira depois de um cruzamento"*, names the touch
        that comes **after** the crossing: price on the setup's side coming back to the line. So
        the crossing bar begins the turn and feeds the clock nothing; the first bar after it whose
        low reaches the average is the first touch. Put to him with the scenario, and confirmed
        the same day: *"pode, a barra da virada não conta"*.

        """
        if crossing:
            # ⚠️ **The reset here is redundant and stays.** A mutant deleting it survives the
            # whole suite, and provably so: to be `crossing` this bar's predecessor closed off
            # side, which already ran `reset` through the branch above — or there was no
            # predecessor at all and the watch has never been fed. Kept because it makes this
            # branch true on its own terms ("a crossing bar starts an empty turn") instead of
            # true because of what another branch happens to do, which is the property that
            # breaks silently the day the other branch moves. Declared rather than papered over,
            # the same standing `bar_setups` gives its own inseparable pair.
            watch.reset()
            return ()
        signals, self._armed, self._armed_count = _reconcile_pattern(
            watch,
            context,
            average=average,
            side=self._side,
            name=self._name,
            entry_point=self._entry_point,
            armed=self._armed,
            count=self._armed_count,
            series=self._trail_of_the_average.series(),
            long_average=self._long,
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


class Mme9TurnStrategy:
    """9.1 as the literature writes it: the bar that *bent the average* is the trade.

    `Mme9BreakoutStrategy` is the author's own 9.1 and this is the published one, and they are
    two different machines that happen to share a number. The difference is what "the average
    turned" means:

    * **His**: a bar **closes across** the MME9. No look at the average's own direction.
    * **Here**: the MME9's own **slope** changes sign — it was falling and this bar has it rising.

    ⚠️ **On an exponential average those two are the same event, and that is arithmetic, not
    coincidence.** `ema = prev + a(close - prev)`, so `ema > prev` exactly when `close > prev`,
    and `close > ema` reduces to `(1-a)(close - prev) > 0`, which is the same inequality again —
    **as long as `a < 1`**. At `period = 1` the alpha is exactly 1, the average *is* the close,
    and `close > ema` is never true: his setup can never arm there and this one still does. A
    degenerate corner the DSL allows (`period >= 1`), pinned rather than forbidden.
    Held over generated streams by `test_slope_and_close_are_the_same_event_on_an_ema`. On an
    **arithmetic** average it would not hold: an SMA turns when the bar leaving the window is
    bigger than the bar entering it, which the newest close alone cannot decide. The literature's
    MME is exponential, so what separates this setup from his is *not* the arming — it is the
    three rules below.

    (The identity is exact in real arithmetic. In `Decimal`, `a * (close - prev)` can round to
    zero when the two are within an ulp of the average, which leaves the line flat under a close
    that is strictly above it. It changes nothing here — this setup reads the slope and never the
    close — and it is why the claim above is pinned by a test rather than asserted as algebra.)

    Three consequences follow, and each of them is a rule of its own:

    1. **The reference bar does not move.** The order rests at the high of the bar that bent the
       line and stays there while the line keeps pointing up (*"espera-se ativação nos candles
       subsequentes desde que a MME9 continue apontando"*). His setup re-prices to the newest bar
       every turn — *"vale sempre a última barra"* — and that is exactly what this one must not
       do, or the two would be the same setup with different arming.
    2. **The trade is not conducted by the average.** A bar closing back across the MME9 tightens
       his stop onto that bar; here it does nothing at all — it cancels a *resting* order and
       leaves an open trade exactly as it was.
    3. **A turn is a single bar's event, not a state.** His turn is a stretch of bars, which is
       why he needs "one trade per turn" bookkeeping. Here, arming happens on the bar that bends
       the line and never again until the line bends back and forward once more, so a spent turn
       is not a thing this class can have.

    **Flat does not turn and does not cancel: it leaves the direction exactly as it was.** A line
    that stopped is not a line that turned, and both other readings of a flat bar are wrong in
    their own direction. Reading it as *"no longer rising"* cancels live orders on the one bar
    where nothing happened — and, worse, arms a buy on a bar in the middle of a fall. Reading it
    as *"direction unknown"* forgets the fall, so the real turn that follows has nothing to have
    turned from and the setup stops appearing after any pause of the average. On a coarse tick
    size a bar closing exactly on the average is not rare.

    ⚠️ **A turn that lands while a trade is open is lost, deliberately.** One position at a time
    is the house rule, and this setup's arming is an event: by the time the account is flat the
    bar that bent the line is history and its high may be far away. Carrying the reference forward
    would be inventing a rule the literature does not state, so the turn simply passes.

    **Conduction is only what the literature names**, which is the initial stop at the reference
    bar's low. `breakeven_at_r` is here because his own setups all carry it and a study has to be
    able to ask the question, but it defaults to `None` — off — because this class exists to say
    what the published setup does, and the published setup says nothing about moving a stop. The
    target stays the account's (`take_profit_rr`), as it is for every setup in this engine.

    Also absent, and for the same reason: `entry_point` bar patterns and the long-average filter.
    Both are the author's, both are grafts onto his own setups, and neither is part of this one.
    """

    def __init__(
        self,
        *,
        side: Side = Side.LONG,
        period: int = 9,
        name: str = "mme9turn",
        stop_buffer_ticks: int = 0,
        breakeven_at_r: Decimal | None = None,
    ) -> None:
        if period < 1:
            raise ValueError(f"MME period must be >= 1, got {period}")
        if stop_buffer_ticks < 0:
            raise ValueError(f"stop buffer is a magnitude in ticks, got {stop_buffer_ticks}")
        if breakeven_at_r is not None and breakeven_at_r <= ZERO:
            raise ValueError(f"breakeven R multiple must be positive, got {breakeven_at_r}")

        self._side = side
        self._name = name
        self._period = period
        self._stop_buffer_ticks = Decimal(stop_buffer_ticks)
        self._breakeven_at_r = breakeven_at_r
        self._ema = EMA(period=period, source="close")
        self._trail_of_the_average = _AverageTrail()

        self._armed: _Armed | None = None
        self._armed_count = 0
        # The average's last reading, and the direction it was moving in. `None` for the
        # direction means "not known yet", which is not the same as flat: the first slope this
        # class can measure has nothing before it to have turned *from*, so it is recorded and
        # not traded. The same entry toll the structure series pays on its first bar.
        self._previous_average: Money | None = None
        self._rising: bool | None = None

    def overlays(self) -> Mapping[str, Indicator]:
        """The average this setup is defined by — see `protocols.Charted`."""
        return {f"EMA {self._period}": self._ema}

    def on_bar(  # noqa: PLR0911 — one flat return per rule, the shape the sibling uses too
        self, context: Context
    ) -> tuple[Signal, ...]:
        candle = context.candle
        # Every bar feeds the average, open trade or not: the slope is a property of the line,
        # and a line fed only on the bars that reached the arming branch is a different line.
        self._ema.update(candle)
        average = self._ema.value()
        self._trail_of_the_average.record(candle, average)

        self._observe_fill(context)

        if average is None:
            return ()

        previous = self._previous_average
        self._previous_average = average
        if previous is None:
            # First reading the average has produced. There is no slope yet, so there is nothing
            # this bar can be: not a turn, not a cancel.
            return ()

        was_rising = self._rising
        # Flat leaves the direction alone — see the class docstring. Only a strict move re-reads
        # it, which also makes `_rising` a fact about the line rather than about this bar.
        if average > previous:
            self._rising = True
        elif average < previous:
            self._rising = False

        # The open trade's stop, if the breakeven rule tightened it. Owed on every bar, including
        # the ones that cancel or arm.
        conducted = self._conduct(context)
        signals: list[Signal] = [] if conducted is None else [conducted]

        if self._rising is None:
            # Every reading so far has been flat, so the line has no direction to be judged by.
            # Nothing is resting either — arming needs a turn, and there has been none.
            return tuple(signals)

        favourable = self._rising if self._side is Side.LONG else not self._rising
        turned = favourable and was_rising is not None and was_rising != self._rising

        if not favourable:
            # The line bent back: the setup is undone and whatever it left resting goes with it.
            if self._armed is not None:
                signals.append(self._withdraw(self._armed, candle))
                self._armed = None
            return tuple(signals)

        if not turned or context.position is not None:
            # The line is still pointing our way but did not bend on this bar, so the order that
            # is already resting stays exactly where it is — the reference does not follow price.
            # And nothing arms beside an open trade.
            return tuple(signals)

        entry = _breakout_entry(
            candle, context.instrument, side=self._side, buffer_ticks=self._stop_buffer_ticks
        )
        if entry is None:
            # A bar with no range is no reference: the trigger would sit on the stop and the
            # trade would carry no risk. The turn is spent all the same — this bar was it.
            return tuple(signals)

        # ⚠️ **Not reachable, and kept for the failure mode rather than for the coverage.**
        # `turned` requires the previous direction to have been against us, and the bar that made
        # it so left through the branch above, which withdrew and forgot whatever was resting — so
        # `_armed` is always `None` here. What it guards is the day a fourth way of arming is
        # added: without it, a second order would go out under a new name while the first still
        # rests at the broker, and the setup would hold two triggers believing it held one.
        if self._armed is not None:
            signals.append(self._withdraw(self._armed, candle))
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
                # The average that bent, as a scalar for aggregation and as the curve that shows
                # the bend. Neither can be derived from the other.
                context={"average": average},
                series=self._trail_of_the_average.series(),
            )
        )
        return tuple(signals)

    def _conduct(self, context: Context) -> Signal | None:
        """The open trade's stop, moved only by the breakeven rule — and only if it is on.

        There is no average rule here. The published setup states an entry and a protective stop
        and stops talking, so this class stops with it: between the fill and the exit the stop
        does not move unless `breakeven_at_r` was asked for.
        """
        position = context.position
        if position is None:
            return None

        breakeven = breakeven_candidate(
            position=position,
            side=self._side,
            candle=context.candle,
            multiple=self._breakeven_at_r,
        )
        if breakeven is None:
            return None
        return tighten(
            position=position,
            side=self._side,
            candle=context.candle,
            candidates=[breakeven],
            reason=f"trail.{self._name}",
        )

    def _observe_fill(self, context: Context) -> None:
        """Notice the armed order becoming a trade, and forget the name.

        Same two signs as the other setups (ADR-0015): a fill on this bar carrying the armed
        name, or a position for a fill this strategy was never shown. Keeping the name would
        cancel an order the trade has already consumed.
        """
        armed = self._armed
        if armed is None:
            return
        filled = any(fill.order.client_id == armed.client_id for fill in context.fills)
        if filled or context.position is not None:
            self._armed = None

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
    fill, so reading it credits nothing the trade did not live through. ⚠️ `martelo_forca` **does**
    rest a limit here, and the guard is still not added: it is the structure family's own, written
    against its own conduction, and inventing it for this setup would be a rule he never gave.

    **`entry_point` replaces rule 3 and leaves the other four standing** (2026-09-07). His four bar
    patterns may enter this setup too — *"e no ponto contínuo também"* — and when one is chosen it
    is the pattern, not the touch-and-close-above bar, that places the order. Everything around it
    is untouched: the two corrections still qualify the pullback, the latch still latches, a close
    **below** the average still erases the order and the qualification together, and the trade is
    still conducted structurally (*"a condução segue o padrão do setup, não do gatilho"*).

    ⚠️ **What the pattern sees is every bar that did not close below the average, once qualified.**
    Rule 3's own trigger demands a close *above*; a pattern does not, because the bar he cares about
    there is the one that touches, and a bar closing exactly *on* the mean is neither event by rule
    4. The window is the touching bar or the next; there is no ceiling; and a pattern that fails
    spends nothing — the next touch starts over inside the same qualification. See `PatternWatch`.

    **`long_average_period` is his optional direction filter** (2026-09-09): with it set, an order
    is only placed when the price it would *enter* at is beyond a long exponential average — above
    it for a buy, below it for a sell. It gates placing and never withdraws, and an open trade is
    conducted exactly as before. `LongAverageFilter` carries the rule and his answers.

    Three of these were our readings when #203 shipped, and he confirmed all three on 2026-09-08
    (*"1 - sim / 2 - sim / 3 - sim"*): the two corrections are still required before a pattern may
    arm; the clock is fed every bar that did not close below the average; an open position closes
    the gate and resets the clock. They are his rules now, not conservative defaults of ours.
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
        entry_point: AverageEntryPoint = AverageEntryPoint.CLASSIC,
        gift_stop: GiftStop = GiftStop.GIFT,
        volume_filter: bool = False,
        long_average_period: int | None = None,
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

        self._entry_point = entry_point
        # The pattern's clock, or nothing for the classic entry — the same seam the MME9 has, and
        # the same object: what differs between the two hosts is who feeds it, not what it counts.
        self._watch: PatternWatch | None = (
            None
            if entry_point is AverageEntryPoint.CLASSIC
            else PatternWatch(
                entry_point=entry_point,
                side=side,
                gift_stop=gift_stop,
                volume_filter=volume_filter,
            )
        )

        # His direction filter, or nothing — the same seam the MME9 has, and the same object.
        self._long: LongAverageFilter | None = (
            None
            if long_average_period is None
            else LongAverageFilter(period=long_average_period, side=side)
        )

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
        """The averages this setup reads — see `protocols.Charted`.

        The one the corrections are counted against, and the long one when the filter is on: a
        chart with only the first would show entries being skipped and nothing saying why.
        """
        overlays: dict[str, Indicator] = {f"{self._average_kind} {self._period}": self._average}
        if self._long is not None:
            overlays[self._long.label] = self._long.indicator
        return overlays

    def on_bar(  # noqa: PLR0911 — one flat return per rule of the setup, like `_may_arm`
        self, context: Context
    ) -> tuple[Signal, ...]:
        candle = context.candle
        self._average.update(candle)
        # The filter's own average, on every bar — one fed only on the bars that reached it is a
        # different average, and its curve would be drawn compressed. Same rule as the trail.
        if self._long is not None:
            self._long.update(candle)
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
            # And the pattern's clock goes with them. His rule 4 covers both entries: *"só desarma
            # se fechar abaixo da média"*, and here that erases the qualification too, so a pattern
            # half-built on the old pullback must not survive into the next one.
            #
            # ⚠️ **Redundant, and declared rather than removed.** A mutant deleting these two lines
            # survives the suite, provably: this branch also clears `_qualified`, so the next bar
            # meets the gate in `_arm_pattern` and is reset there before the clock can be read. The
            # difference is one bar of stale state that nothing looks at. It stays because the two
            # resets answer different questions — *"the pullback is over"* here, *"this bar may not
            # arm"* there — and the day the gate moves or learns an exception, the rule he actually
            # stated would leave with it. Same standing as the MME9's `crossing` reset.
            if self._watch is not None:
                self._watch.reset()
            return tuple(signals)

        # The qualification as it stood *coming into* this bar is what lets this bar trigger; a
        # correction this bar happens to be counts toward the next one. That ordering is the BOS's
        # ("`_up_armed and close > top`" is tested before the correction is counted): the bar that
        # completes the pullback is part of it, not the reversal out of it.
        qualified = self._qualified
        self._track_correction(candle, previous)

        if self._watch is not None:
            signals.extend(self._arm_pattern(self._watch, average, context, qualified=qualified))
            return tuple(signals)

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

        if self._long is not None and not self._long.allows(entry.stop_price):
            # His filter, on the entry price (`LongAverageFilter`). Like the bar with no range
            # above, this bar failed to be a setup and destroyed nothing: the two corrections and
            # the qualification stand, whatever rests stays resting, and the next touch asks
            # again. Placing is gated; withdrawing is not.
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
                # definition, and the level a reader needs to see the touch happen. The long
                # average joins it when the filter is on: it is half of why this entry was
                # allowed, and nothing else downstream would carry it.
                context=_entry_context(average, self._long),
                # And the curve it belongs to. In this setup the shape is the point: the whole
                # rule is price running away, coming *back* to the line, and turning off it —
                # which a single horizontal level cannot show. And the long average beside it,
                # for the same reason, when the filter is on.
                series=self._trail_of_the_average.series() + _long_series(self._long),
            )
        )
        return tuple(signals)

    def _arm_pattern(
        self, watch: PatternWatch, average: Money, context: Context, *, qualified: bool
    ) -> list[Signal]:
        """This bar's turn at the pattern's clock, once the pullback has been judged.

        ⚠️ **The qualification gates the clock, and the clock is reset while it is missing.**
        Feeding a pullback that has not qualified would let a hammer on any touch of the average
        arm this setup — which is the MME9's rule, not this one's. Here two corrections come
        first, and they are what makes it the Ponto Contínuo rather than a slower 9.1. A trade in
        flight closes the gate for the reason rule 5 already gives: one at a time. Both halves of
        this gate — the corrections and the open position — are his, confirmed 2026-09-08.

        The reconciliation itself is `_reconcile_pattern`, shared with the MME9.
        """
        if context.position is not None or not qualified:
            watch.reset()
            return []
        placed, self._armed, self._armed_count = _reconcile_pattern(
            watch,
            context,
            average=average,
            side=self._side,
            name=self._name,
            entry_point=self._entry_point,
            armed=self._armed,
            count=self._armed_count,
            long_average=self._long,
            series=self._trail_of_the_average.series(),
        )
        return placed

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
