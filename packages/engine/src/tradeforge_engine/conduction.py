"""Moving the stop of an open trade: the parts more than one method does the same way.

Two conduction rules live in this engine and they disagree about almost everything. The **average**
rule tightens on the bar that closes back across a moving average; the **structural** rule takes
the trade to breakeven on the first break of structure in its favour and then trails behind the low
each later break came from. A setup picks one — and picks it independently of how it *enters*, which
is not a hypothetical: the Ponto Contínuo enters on a breakout like the rest of the swing family and
conducts structurally like the SMC setups, because that is the rule its author trades.

What no method disagrees about is what happens once a rule has named a price:

* several rules can name a level on the same bar, and **the tighter one wins**;
* a level that does not improve on the stop already in force is **not sent at all**;
* a level at or below zero is not a stop.

That is `tighten`, and it holds no state — nor does `breakeven_candidate`. The distinction worth
keeping is *whose* state: a conductor remembering the stop it last sent would be a second copy of
the broker's, and the level in force is a fact the `Position` already reports (ADR-0018). Reading it
is free; mirroring it means owning the day the two disagree, which is a day the engine raises
`EngineError` in the middle of a winning trade. `StructuralTrail` does hold state, and it is exempt
for exactly that reason: how many breaks of structure a trade has seen is not something the broker
knows, has an opinion about, or could ever contradict.

**Nothing here decides that the tighter rule wins.** The engine refuses a loosening outright, so
the only thing a strategy owes is to not ask — which is why `tighten` returns `None` far more often
than it returns a signal, and why the tests that matter most are the ones asserting silence.
"""

import logging
from collections.abc import Sequence
from datetime import datetime
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
from tradeforge_engine.structure import StructureBreak, StructureKind, Trend

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


def _favours(break_: StructureBreak, position: Position) -> bool:
    """Does this break push the trend the way the open trade is pointing?"""
    wanted = Trend.BULLISH if position.side is Side.LONG else Trend.BEARISH
    return break_.trend is wanted


class StructuralTrail:
    """The stop that market structure names for one open trade: breakeven, then leg origins.

    The author's rule, and the only stateful thing in this module:

    * the **first** break of structure in the trade's favour brings the stop to the **entry price**;
    * **every break after it** brings the stop to that break's `origin`.

    One instance belongs to one strategy and follows it across trades. It is *not* a conductor: it
    names a candidate and nothing else — whether that candidate is sent is `tighten`'s question.

    **Why `origin` and not the price that was broken.** `StructureBreak.level` is where the move
    went *through*; `origin` is where it came *from* — the lowest low of the leg, and the very
    level the next opposite CHoCH is anchored to. Trailing behind it puts the stop where the trend
    would be **disproven**, not merely where it looks tired. Behind `level` the stop would sit
    inside the move it is riding and be taken out by any ordinary pullback.

    **Only a BOS counts, and the reason is the author's own.** A CHoCH in our favour looks like it
    should qualify — it is a structural event on our side — but inside a conducted trade the
    structure already points our way, so the CHoCH that actually shows up is the *opposite* one: it
    ends the trade rather than conducting it. A favourable CHoCH is not a case that was excluded; it
    is a case that does not occur. The test demanding silence on one guards the code path, not the
    method.

    **The count is keyed to the trade, not reset by an event.** A counter zeroed *at the fill* is
    correct only for as long as every path that opens a trade runs through the observer that notices
    fills — and the day one does not, the next trade's first break silently takes the second break's
    branch, putting the stop at a leg origin *below* the entry so the trade goes on carrying risk
    the method says it already gave up. Keyed by `entry_time`, a trade that never announced itself
    still counts from zero, because it is a different trade. (`entry_time` identifies a trade here
    because the engine holds one position at a time and time moves forward.)

    A consequence of keying rather than resetting: the class is safe to consult *only* on bars that
    carry a break, since the trade it is counting for is re-checked on the same call that counts.
    Its two callers ask on every bar anyway, which keeps them symmetrical and costs nothing.
    """

    def __init__(self) -> None:
        self._breaks_in_favour = 0
        self._counting_for: datetime | None = None

    def candidate(self, *, position: Position, break_: StructureBreak | None) -> Money | None:
        """The level this bar's structure names for `position`, or `None` if it names none."""
        if position.entry_time != self._counting_for:
            self._counting_for, self._breaks_in_favour = position.entry_time, 0

        if break_ is None or break_.kind is not StructureKind.BOS or not _favours(break_, position):
            return None

        self._breaks_in_favour += 1
        return position.entry_price if self._breaks_in_favour == 1 else break_.origin
