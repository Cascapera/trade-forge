"""How much of a finished run to keep — his bar for a sweep's runs (2026-09-23).

A sweep across hundreds of assets produces millions of runs, and a run kept whole is ~0.5 MB, 95%
of it pictures and an equity curve that nobody opens one run at a time. So a sweep's run keeps:

* its **metrics, always** — the losers included, because every winner is judged against how many
  points were tried and how they fared, and a database of survivors cannot answer that;
* its **trades, only when it passed the bar**: a net profit above zero **and** enough trades for
  the profit to mean something on its chart. The profit may be the run's own **or any rung of the
  target ladder** (`excursion.target_ladder`, net of costs): a sweep runs without a target, and a
  run that loses without one but wins with 2 R is exactly the one to open later;
* **never** the entry pictures or the equity curve. The engine is deterministic: running the same
  point again rebuilds both, exactly.

Anything that is not a sweep's run — a single backtest, a study, a basket, a walk-forward — keeps
everything, as before: those are read run by run.

The floor is his, per chart: on H4 and above a wide window gives few trades anyway, so any profit
counts; on the intraday charts a profit over a handful of trades is mostly luck.
"""

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Final

from tradeforge_db import Recorded

MIN_TRADES: Final[Mapping[str, int]] = {
    "M1": 60,
    "M5": 60,
    "M15": 30,
    "M30": 30,
    "H1": 30,
    "H4": 0,
    "D1": 0,
    "W1": 0,
}
"""The fewest trades a sweep's run needs, on each chart, for its trades to be kept. Every chart the
DSL names has a line here, and a test fails the day one is added without one."""


def recorded_for(
    *,
    in_sweep: bool,
    timeframe: str,
    net_profit: Decimal,
    total_trades: int,
    target_net_r: Iterable[Decimal | None] = (),
) -> Recorded:
    """What a finished run keeps. See the module docstring for why.

    `target_net_r` is the net result, in R, at each rung of the ladder; a rung that could not be
    scored is `None` and passes nothing.

    ⚠️ **Strictly above zero.** A run that broke even kept nothing worth reading trade by trade,
    and the bar exists to say which runs earned the space.

    ⚠️ **Choosing the best of eleven targets flatters the run** — that is chance, searched. Here
    it only decides what is *kept*; what is *good* is decided later, against the whole ladder,
    which is why every rung is recorded and not just the best.

    Raises `KeyError` for a chart with no floor, rather than picking one: a missing line is a
    decision nobody made, and a default would make it silently.
    """
    if not in_sweep:
        return Recorded.FULL
    floor = MIN_TRADES[timeframe]
    profitable = net_profit > 0 or any(r is not None and r > 0 for r in target_net_r)
    if profitable and total_trades >= floor:
        return Recorded.TRADES
    return Recorded.METRICS


__all__ = ["MIN_TRADES", "recorded_for"]
