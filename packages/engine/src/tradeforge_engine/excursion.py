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

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from tradeforge_engine.domain import ZERO, ClosedTrade, InstrumentSpec, Money, Side

LADDER: Final[tuple[Money, ...]] = tuple(
    Decimal(k) for k in ("0.5", "1", "1.5", "2", "2.5", "3", "4", "5", "6", "8", "10")
)
"""The targets every sweep run is scored at, in R (his call, 23/09). Dense up to 3 R, where most
setups live and half an R changes a result; sparse after, where a step is noise over few hits;
capped at 10 R, which almost no trade reaches in an ordinary window."""


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


@dataclass(frozen=True, slots=True)
class TargetOutcome:
    """What a run's trades would have made at one target, in R net of each trade's own costs."""

    trades: int
    hits: int
    """How many of them the target would have closed."""
    net_r: Money
    """The sum, in R net of costs — the target's equivalent of a run's net profit."""
    expectancy_r: Money
    """`net_r / trades`: what one trade was worth, on average."""
    max_drawdown_r: Money
    """The deepest fall of the running sum from its peak, in R. Zero for a run that never fell."""


def target_ladder(
    trades: Sequence[ClosedTrade],
    instrument: InstrumentSpec,
    ladder: Sequence[Money] = LADDER,
) -> dict[Money, TargetOutcome | None]:
    """Each target of `ladder` applied to every trade (`with_target`), summed per target.

    **Net of costs.** `with_target` is gross, a price ratio; a trade's costs are money. Each trade's
    costs are turned into R of its own risk and taken off, so a run that breaks even gross and
    loses the spread does not read as positive. The costs are the trade's actual ones — a target
    would have exited at another price, and the exit leg might have cost a hair more or less.

    A target is `None` when any trade cannot answer it (`with_target` returns `None`: no stop, not
    measured, or a target of its own closer than this one). A sum over the trades that *could*
    answer would be a sum over a different set of trades at each rung, and read as one run.

    ⚠️ Per trade, not per run — see the module docstring for the cascade this cannot see.
    """
    out: dict[Money, TargetOutcome | None] = {}
    costs_r = [_costs_in_r(trade, instrument) for trade in trades]
    for target in ladder:
        results: list[Money] = []
        hits = 0
        for trade, cost in zip(trades, costs_r, strict=True):
            gross = with_target(trade, target)
            if gross is None or cost is None:
                break
            # `with_target` answered, so `mfe_r` is set; reaching the target is what a hit is.
            if trade.mfe_r is not None and trade.mfe_r >= target:
                hits += 1
            results.append(gross - cost)
        else:
            out[target] = _summed(results, hits)
            continue
        out[target] = None
    return out


def _summed(results: Sequence[Money], hits: int) -> TargetOutcome:
    total, peak, deepest = ZERO, ZERO, ZERO
    for result in results:
        total += result
        peak = max(peak, total)
        deepest = max(deepest, peak - total)
    count = len(results)
    return TargetOutcome(
        trades=count,
        hits=hits,
        net_r=total,
        expectancy_r=total / count if count else ZERO,
        max_drawdown_r=deepest,
    )


def _costs_in_r(trade: ClosedTrade, instrument: InstrumentSpec) -> Money | None:
    if trade.stop_loss is None:
        return None
    risk = instrument.money_for(abs(trade.entry_price - trade.stop_loss), trade.volume)
    if risk <= ZERO:
        return None
    return trade.costs / risk


__all__ = ["LADDER", "TargetOutcome", "gross_r", "target_ladder", "with_target"]
