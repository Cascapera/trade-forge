"""Risk: how big a position, and whether to take it at all.

Two questions, deliberately answered by two methods (sdd.md §3.3.2). `size` is arithmetic:
given this stop, this account and this instrument, how many lots put exactly `percent` of the
account at risk. `allow` is a veto: the kill switch, the daily loss limit, the position cap.

They are separate because a bug must not leak between them. If sizing were where the kill
switch lived, a rounding error in "how big" would become a failure of "should we be trading
at all" — and the second is a safety property, not an arithmetic one.
"""

import logging
from decimal import ROUND_DOWN, Decimal

from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    InstrumentSpec,
    OrderRequest,
    Signal,
    Volume,
)

logger = logging.getLogger(__name__)


class PercentRiskManager:
    """Size a position so that hitting its stop costs a fixed percent of the account.

    The arithmetic, and why each part is there:

        risk_budget   = equity · percent/100          # the money you are willing to lose
        risk_per_lot  = money_for(|entry - stop|, 1)   # what one lot loses if the stop hits
        volume        = risk_budget / risk_per_lot

    This is the whole reason a `Signal` carries a stop and a `Position` is sized against it:
    the *same* strategy risks 1% on a $1 000 account and on a $1 000 000 one, and trades a
    tight stop bigger than a wide one, with no edit. Position size is a function of the stop
    distance, not a number the strategy picks.

    **No stop, no trade.** Percent-risk is meaningless without a stop to measure risk against —
    there is no distance to size. It returns zero (which the loop reads as "no trade") rather
    than inventing a default size, because a silent default is how an unstopped position ends
    up on the book at a size nobody chose.
    """

    def __init__(self, *, percent: Volume, lot_step: Volume = Decimal("0.01")) -> None:
        if percent <= ZERO:
            raise ValueError(f"risk percent must be positive, got {percent}")
        if lot_step <= ZERO:
            raise ValueError(f"lot step must be positive, got {lot_step}")
        self._percent = percent
        self._lot_step = lot_step

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Volume:
        if signal.stop_loss is None:
            logger.debug("percent-risk sizing with no stop at %s; no trade", signal.reference_price)
            return ZERO

        stop_distance = abs(signal.reference_price - signal.stop_loss)
        if stop_distance <= ZERO:
            logger.debug("stop distance is zero at %s; no trade", signal.reference_price)
            return ZERO

        risk_budget = account.equity * (self._percent / 100)
        risk_per_lot = instrument.money_for(stop_distance, Decimal(1))
        raw = risk_budget / risk_per_lot

        # Floor to the broker's lot step. A venue will not fill 0.2037 lots, and rounding
        # *down* never risks more than the budget — the direction that matters for a limit.
        # It also keeps volume a terminating decimal, so the P&L it produces reconciles to the
        # cent instead of trailing Decimal dust from an unrounded division. (A real system
        # reads the step from the instrument, per MT5's `volume_step`; phase 1 takes it here.)
        steps = (raw / self._lot_step).to_integral_value(rounding=ROUND_DOWN)
        return steps * self._lot_step

    def allow(self, order: OrderRequest, account: AccountState) -> bool:  # noqa: ARG002
        """The veto. Always true in phase 1.

        The daily-loss limit and the kill switch belong here, but both need state this method
        is not yet handed — the day's opening equity, the wall-clock of the bar. Wiring that in
        is a session-level concern (the live safeguards of sdd.md §11), tracked for a later PR
        rather than half-built here where a test could not exercise it. Kept as a real method,
        returning true, so the seam exists and the loop already asks the question.
        """
        return True


__all__ = ["PercentRiskManager"]
