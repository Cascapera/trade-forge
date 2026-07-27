"""The swing family: enter on the break of the candle that turned the average.

The structure setups (`setups.py`) enter on a **pullback** — price comes back to a zone and a
limit order fills at its edge. The swing setups (Larry Williams / Stormer — 9.1, 9.2, 9.3, 9.4,
Ponto Contínuo) enter on the **breakout**, the opposite direction: a bar closes across the MME9,
and the order rests a tick above that bar waiting for price to break through it. That is a stop
order (ADR-0016), the geometric mirror of the limit — so this is a different machine from the one
in `setups.py`, and it lives in its own module for exactly that reason.

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

**This module only enters and cancels.** The exit is the broker's: a take-profit at a multiple of
risk (`take_profit_rr`), or a bare stop when no multiple is set. The author's own exit — ride the
trade until a bar closes back across the MME9, then trail the stop to that bar's far side — is
stop conduction, a new verb on the position, and it gets its own ADR and review (the sibling PR).
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

from tradeforge_engine.domain import (
    ZERO,
    Candle,
    Context,
    InstrumentSpec,
    Money,
    Side,
    Signal,
    SignalKind,
)
from tradeforge_engine.indicators import EMA

logger = logging.getLogger(__name__)


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
    """

    def __init__(
        self,
        *,
        side: Side = Side.LONG,
        period: int = 9,
        name: str = "mme9",
        stop_buffer_ticks: int = 0,
    ) -> None:
        if period < 1:
            raise ValueError(f"MME period must be >= 1, got {period}")
        if stop_buffer_ticks < 0:
            raise ValueError(f"stop buffer is a magnitude in ticks, got {stop_buffer_ticks}")

        self._side = side
        self._name = name
        self._stop_buffer_ticks = Decimal(stop_buffer_ticks)
        self._ema = EMA(period=period, source="close")

        self._armed: _Armed | None = None
        self._armed_count = 0
        # A turn is spent once its order has traded: no new entry until price closes back across
        # the average and crosses again. Set when a fill is observed, cleared by the first bar
        # that closes on the wrong side of the average — which is exactly a turn ending.
        self._spent = False

    def on_bar(self, context: Context) -> tuple[Signal, ...]:
        candle = context.candle
        # The average tracks every bar, open position or not: the turn that ends a trade and the
        # cross that starts the next one are both read off a live MME9.
        self._ema.update(candle)
        average = self._ema.value()

        self._observe_fill(context)

        if average is None:
            # The MME9 is still warming up: there is no average to have closed across yet.
            return ()

        # Which side of the average did this bar close on — the setup's, or against it?
        on_side = candle.close > average if self._side is Side.LONG else candle.close < average

        if not on_side:
            # A close back across the average ends the turn — and it ends it whether or not a
            # trade is still open. The average does not know we are positioned: if price closed on
            # the wrong side while the trade ran and then crossed back, that is a *new* turn, and
            # the first bar to find the account flat is entitled to arm it. Reading this boundary
            # only when flat would swallow every turn that began inside a trade, and swallow it
            # silently — no number comes out wrong, trades simply stop appearing.
            signals: list[Signal] = []
            if self._armed is not None:
                signals.append(self._withdraw(self._armed, candle))
                self._armed = None
            self._spent = False
            return tuple(signals)

        if context.position is not None:
            # The broker's stop and target own an open trade; nothing arms beside it. Nothing is
            # resting either: `_observe_fill` drops the armed name on any bar that shows a
            # position, so there is never an order here left to withdraw.
            return ()

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
            )
        )
        return tuple(signals)

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
        """The stop order this reference candle places, or `None` if it carries no risk.

        A long breaks the **high** and is protected at the **low**; the short mirrors it. The
        buffer, in ticks, pushes the stop past the candle's edge — a stop *on* the low is taken
        out by the noise of the very bar that set it. It defaults to zero, which is the author's
        literal rule (the stop is the reference bar's low), and is offered because a real desk
        usually wants a tick of room.
        """
        if candle.high <= candle.low:
            logger.debug("reference bar at %s has no range; nothing to arm", candle.time)
            return None

        buffer = self._stop_buffer_ticks * instrument.tick_size
        if self._side is Side.LONG:
            stop_loss = candle.low - buffer
            if stop_loss <= ZERO:
                logger.debug("reference bar at %s would need a stop <= 0; nothing", candle.time)
                return None
            return _Breakout(stop_price=candle.high, stop_loss=stop_loss)
        return _Breakout(stop_price=candle.low, stop_loss=candle.high + buffer)

    def _withdraw(self, armed: _Armed, candle: Candle) -> Signal:
        """Take back a named order. Harmless if it never reached the book — a cancel for an order
        the broker does not hold is answered `False`, not raised (a live race, not a bug)."""
        logger.debug("withdrawing %s at %s", armed.client_id, candle.time)
        return Signal(
            kind=SignalKind.CANCEL,
            side=self._side,
            reference_price=candle.close,
            reason=f"cancel.{self._name}",
            client_id=armed.client_id,
        )
