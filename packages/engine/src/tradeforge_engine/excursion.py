"""What a trade would have made under a target it did not have — read off how far it went.

His idea of 23/09: a sweep runs **without** a target, once, and every target is derived afterwards
from each trade's maximum favourable excursion (`ClosedTrade.mfe_r`). Five targets in a grid would
otherwise be five runs of the same point.

The rule is one line: a trade whose `mfe_r` reached `k` would have been closed at `+k R` by a
`k R` target; one that did not would have ended exactly as it did, because without a target the
position is conducted the same way until it leaves.

⚠️ **Exact per trade, and only per trade.** A target that closes a trade earlier frees the book
earlier, and this engine holds one position at a time: a real run with the target could take an
entry the run without it never saw, and every trade after that differs. Summing `with_target` over
a run answers "what these trades would have made", not "what a run with this target would have
made". Close for a setup that rarely re-arms while in a trade; not guaranteed for one that does.

⚠️ **Gross R, both ways.** `mfe_r` is a price ratio, so the derived result is compared with the
trade's own result as a price ratio too (`gross_r`), never with `r_multiple`, which is money after
costs. Mixing the two would make every derived target look better than the trade it replaced by
exactly the costs.

`mfe_r` can only err low (`Position.best_price`), so a derived hit is a hit the bars prove; a target
reached inside the same bar as the stop counts as missed, which is also what the broker does.
"""

from tradeforge_engine.domain import ZERO, ClosedTrade, Money, Side


def gross_r(trade: ClosedTrade) -> Money | None:
    """The trade's result as a price ratio: `(exit - entry) / |entry - stop|`, signed by side.

    `None` without a stop to measure against."""
    if trade.stop_loss is None:
        return None
    risk = abs(trade.entry_price - trade.stop_loss)
    if risk <= ZERO:
        return None
    moved = trade.exit_price - trade.entry_price
    return (moved if trade.side is Side.LONG else -moved) / risk


def with_target(trade: ClosedTrade, target_r: Money) -> Money | None:
    """The trade's gross R had it carried a `target_r` target. `None` when it cannot be said.

    It cannot be said when the trade was not measured (`mfe_r` is `None`: no stop, or recorded
    before excursions were), or when the trade **already had a target closer than this one**: it
    left at that target, and how far it would have gone after is not in the record.
    """
    if target_r <= ZERO:
        raise ValueError(f"a target is a positive multiple of the risk, got {target_r}")
    if trade.mfe_r is None:
        return None
    own = _own_target_r(trade)
    if own is not None and target_r > own:
        return None
    if trade.mfe_r >= target_r:
        return target_r
    return gross_r(trade)


def _own_target_r(trade: ClosedTrade) -> Money | None:
    if trade.take_profit is None or trade.stop_loss is None:
        return None
    risk = abs(trade.entry_price - trade.stop_loss)
    if risk <= ZERO:
        return None
    return abs(trade.take_profit - trade.entry_price) / risk


__all__ = ["gross_r", "with_target"]
