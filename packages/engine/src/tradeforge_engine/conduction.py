"""Moving the stop of an open trade: the part every method does the same way.

Two setups conduct trades in this engine and they disagree about almost everything. The swing
family watches an average and tightens on the bar that closes back across it; the structure family
watches market structure and tightens behind the low each break of structure came from. What they
do **not** disagree about is what happens once a rule has named a price:

* several rules can name a level on the same bar, and **the tighter one wins**;
* a level that does not improve on the stop already in force is **not sent at all**;
* a level at or below zero is not a stop.

That is the whole of this module, and it is deliberately not a class. A conductor holding state
would be a second copy of the broker's — and the level actually in force is a fact the `Position`
already reports (ADR-0018). Reading it is free; mirroring it means owning the day the two disagree,
which is a day the engine raises `EngineError` in the middle of a live trade.

**Nothing here decides that the tighter rule wins.** The engine refuses a loosening outright, so
the only thing a strategy owes is to not ask — which is why `tighten` returns `None` far more often
than it returns a signal, and why the tests that matter most are the ones asserting silence.
"""

import logging
from collections.abc import Sequence
from decimal import Decimal

from tradeforge_engine.domain import (
    ZERO,
    Candle,
    Money,
    Position,
    Side,
    Signal,
    SignalKind,
)

logger = logging.getLogger(__name__)


def breakeven_candidate(
    *,
    position: Position,
    side: Side,
    candle: Candle,
    multiple: Decimal | None,
) -> Money | None:
    """The entry price, once this bar has reached `multiple` times the initial risk — or `None`.

    **Touching, not closing.** The bar's high (low, for a short) reaching the level is the event;
    a method that waited for a close would leave a different trade behind, because the bar that
    spikes through and closes back is exactly the bar this rule exists for.

    **Risk is measured from `initial_stop_loss`, never from the stop being moved.** After the
    first tightening the live stop is no longer what the lot was sized against: measure from it
    and the line drifts closer with every move, arming breakeven earlier and earlier on a trade
    that never actually got there. The bias looks conservative and shows up in no number as wrong,
    which is what makes it worth a paragraph.

    Silent when the rule is switched off (`multiple is None`), when the entry carried no stop, and
    when the risk is zero — a multiple of nothing would be satisfied by nothing.
    """
    if multiple is None or position.initial_stop_loss is None:
        return None

    risk = abs(position.entry_price - position.initial_stop_loss)
    if risk <= ZERO:
        return None

    long = side is Side.LONG
    reach = candle.high if long else candle.low
    target = (
        position.entry_price + multiple * risk if long else position.entry_price - multiple * risk
    )
    if reach >= target if long else reach <= target:
        return position.entry_price
    return None


def tighten(
    *,
    position: Position,
    side: Side,
    candle: Candle,
    candidates: Sequence[Money],
    reason: str,
) -> Signal | None:
    """The `MODIFY_STOP` this bar owes, or `None` to leave the stop exactly where it is.

    `candidates` are the levels this bar's rules named, in any order and with no ranking: the
    caller supplies prices, not priorities. The tighter one wins, which for a long is the highest
    and for a short the lowest.

    Returns `None` — the common case — when no rule fired, when the winner would be at or below
    zero, and, most importantly, when it does not improve on `position.stop_loss`. That last one
    is not an optimisation. Sending a level that loosens raises (ADR-0018), and re-sending one
    already in force would restamp `stop_decided_at` on a bar that decided nothing, putting a
    signal in the stream for every remaining bar of a winning trade.
    """
    if not candidates:
        return None

    long = side is Side.LONG
    level = max(candidates) if long else min(candidates)
    if level <= ZERO:
        # A price at or below zero is not a stop. Reachable wherever a buffer is subtracted from
        # a low that is already small.
        logger.debug("conducted stop at %s would be <= 0; leaving it", candle.time)
        return None

    current = position.stop_loss
    if current is not None and (level <= current if long else level >= current):
        return None

    logger.debug("conducting the stop to %s at %s", level, candle.time)
    return Signal(
        kind=SignalKind.MODIFY_STOP,
        side=side,
        reference_price=candle.close,
        stop_loss=level,
        reason=reason,
    )
