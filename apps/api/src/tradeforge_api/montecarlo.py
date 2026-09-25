"""Monte Carlo of one run's trades: how deep and how long it could have hurt, had the same trades
come in another draw — with no database and no HTTP.

A run's drawdown is **one path**. The same edge, dealt a different sequence of the same kind of
trades, would have fallen deeper or shallower, and the path that happened is only one of them. His
ask (25/09): before trusting a point, see the range — the drawdown to expect, the losing streak to
sit through, and how often the whole thing ends below zero.

⚠️ **Resampled with replacement (bootstrap), not only shuffled.** Shuffling keeps the same trades
and changes only their order, so the final R never moves and "how often does it end negative" is
always 0% or 100%. Drawing with replacement varies which trades come too, which is what answers
"could this result be luck". The drawdown and the streak come out of the same draws.

⚠️ **Deterministic.** Each point draws from its own generator, seeded from the request's seed and
the run's id — the same request gives the same answer, and adding or removing a point changes no
other point's draws.

⚠️ **Floats inside, on purpose.** Thousands of paths of hundreds of trades are millions of steps;
in `Decimal` that is minutes, in float well under a second a point (measured 25/09: 0.08 s for
2000 paths of 300 trades). The inputs are R to a few decimals and the outputs are percentiles read
to two — a float's error is far below either.

Trades are assumed independent, which is what resampling means. A method whose trades cluster —
losses that come in runs because the market's regime does — is understated here, and the per-year
and per-block cuts (`slices`) are where that shows.
"""

import random
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

MIN_TRADES = 20
"""Fewer trades than this are not resampled: drawing thousands of paths out of a handful of
trades repeats the same few and dresses them as a distribution."""

DEFAULT_PATHS = 1000
MAX_PATHS = 5000


@dataclass(frozen=True, slots=True)
class Spread:
    """Percentiles of one measure across the simulated paths."""

    p5: Decimal
    p50: Decimal
    p95: Decimal
    p99: Decimal


@dataclass(frozen=True, slots=True)
class Simulated:
    """What the paths drawn from one run's trades looked like."""

    paths: int
    trades: int
    drawdown_r: Spread
    """Deepest fall of each path's cumulative R from its running peak (which starts at zero)."""
    losing_streak: Spread
    """Most consecutive losing trades in each path."""
    net_r: Spread
    negative_share: Decimal
    """The share of paths that ended below zero R."""


def _percentile(ordered: Sequence[float], share: float) -> Decimal:
    """Nearest rank on an already sorted list — no interpolation, so every value is one a path
    actually reached."""
    index = min(len(ordered) - 1, max(0, round(share * (len(ordered) - 1))))
    return Decimal(repr(round(ordered[index], 4)))


def _spread(values: list[float]) -> Spread:
    values.sort()
    return Spread(
        p5=_percentile(values, 0.05),
        p50=_percentile(values, 0.50),
        p95=_percentile(values, 0.95),
        p99=_percentile(values, 0.99),
    )


@dataclass(frozen=True, slots=True)
class Observed:
    """The one path that happened, measured exactly as each simulated path is."""

    net_r: Decimal
    drawdown_r: Decimal
    losing_streak: int


def observed(rs: Sequence[Decimal]) -> Observed:
    """The trades in the order they came: the point the simulated spreads are read against."""
    total = peak = deepest = Decimal(0)
    streak = longest = 0
    for r in rs:
        total += r
        peak = max(peak, total)
        deepest = max(deepest, peak - total)
        if r < 0:
            streak += 1
            longest = max(longest, streak)
        else:
            streak = 0
    return Observed(net_r=total, drawdown_r=deepest, losing_streak=longest)


def simulate(rs: Sequence[Decimal], *, paths: int, seed: str) -> Simulated | None:
    """`paths` paths of `len(rs)` trades each, drawn with replacement from `rs`.

    `None` below `MIN_TRADES`. `seed` is any string — the caller makes it unique per point.
    """
    if len(rs) < MIN_TRADES:
        return None
    if paths < 1:
        raise ValueError(f"a simulation needs at least one path, got {paths}")

    pool = [float(r) for r in rs]
    draw = random.Random(seed)  # noqa: S311 — a simulation, not a secret
    count = len(pool)
    drawdowns: list[float] = []
    streaks: list[float] = []
    finals: list[float] = []
    negative = 0

    for _ in range(paths):
        total = peak = deepest = 0.0
        streak = longest = 0
        for r in draw.choices(pool, k=count):
            total += r
            peak = max(peak, total)
            deepest = max(deepest, peak - total)
            if r < 0:
                streak += 1
                longest = max(longest, streak)
            else:
                streak = 0
        drawdowns.append(deepest)
        streaks.append(float(longest))
        finals.append(total)
        if total < 0:
            negative += 1

    return Simulated(
        paths=paths,
        trades=count,
        drawdown_r=_spread(drawdowns),
        losing_streak=_spread(streaks),
        net_r=_spread(finals),
        negative_share=Decimal(negative) / Decimal(paths),
    )


__all__ = [
    "DEFAULT_PATHS",
    "MAX_PATHS",
    "MIN_TRADES",
    "Observed",
    "Simulated",
    "Spread",
    "observed",
    "simulate",
]
