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

Trade by trade assumes the trades independent, which is what resampling one at a time means. A
method whose losses come in runs — because the market's regime does — is understated by it. Since
01/10 every point is also drawn **in blocks** (`simulate_in_blocks`): runs of N trades in a row,
kept in the order they happened, so a losing stretch is dealt whole. The two side by side answer
"are the losses clustered": a drawdown much deeper in blocks says they are.
"""

import math
import random
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from decimal import Decimal

MIN_TRADES = 20
"""Fewer trades than this are not resampled: drawing thousands of paths out of a handful of
trades repeats the same few and dresses them as a distribution."""

DEFAULT_PATHS = 1000
MAX_PATHS = 5000

MIN_BLOCK_TRADES = 2
"""A block of one trade is trade by trade again."""
MAX_BLOCK_TRADES = 50
"""The most a request may ask for. Past this a block is a large part of any sample worth
resampling, and each path is a few long pieces of the one that happened, reshuffled."""


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
    block_trades: int | None = None
    """The trades in a row each block held (01/10); `None` when drawn trade by trade."""


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


def _measured(
    drawn: Iterable[Sequence[float]], *, paths: int, trades: int, block_trades: int | None
) -> Simulated:
    """Each drawn path walked once: its deepest fall, longest losing streak and final R.

    ⚠️ **One ruler for both draws.** Trade by trade and in blocks are measured by this same loop,
    so a difference between them is the draw's and never the measuring's.
    """
    drawdowns: list[float] = []
    streaks: list[float] = []
    finals: list[float] = []
    negative = 0
    for path in drawn:
        total = peak = deepest = 0.0
        streak = longest = 0
        for r in path:
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
        trades=trades,
        drawdown_r=_spread(drawdowns),
        losing_streak=_spread(streaks),
        net_r=_spread(finals),
        negative_share=Decimal(negative) / Decimal(paths),
        block_trades=block_trades,
    )


def simulate(rs: Sequence[Decimal], *, paths: int, seed: str) -> Simulated | None:
    """`paths` paths of `len(rs)` trades each, drawn with replacement from `rs` one at a time.

    `None` below `MIN_TRADES`. `seed` is any string — the caller makes it unique per point.
    """
    if len(rs) < MIN_TRADES:
        return None
    if paths < 1:
        raise ValueError(f"a simulation needs at least one path, got {paths}")

    pool = [float(r) for r in rs]
    draw = random.Random(seed)  # noqa: S311 — a simulation, not a secret
    count = len(pool)
    # ⚠️ One `choices` call per path, as before 01/10: a seed kept on 25/09 still deals the same.
    drawn = (draw.choices(pool, k=count) for _ in range(paths))
    return _measured(drawn, paths=paths, trades=count, block_trades=None)


def default_block_trades(trades: int) -> int:
    """The block a sample of `trades` is cut into when the request names none (01/10): the cube
    root of the sample, rounded, never under `MIN_BLOCK_TRADES` — 3 for 20 trades, 5 for 150, 7
    for 300.

    The cube root is the textbook order of a block bootstrap's block (Hall, Horowitz & Jing, 1995):
    long enough to carry a short run of related trades, short enough to leave many blocks to draw
    from. A rule of thumb, not a fit — the request's `block_trades` overrides it.
    """
    return max(MIN_BLOCK_TRADES, round(math.cbrt(trades)))


def block_fits(trades: int, block_trades: int) -> bool:
    """Whether blocks of `block_trades` leave a sample of `trades` something to resample.

    ⚠️ **Refused from half the sample up.** Two blocks or fewer to a path is the path that
    happened cut in two and dealt again — a spread that looks like a distribution and is not one.
    """
    return 2 * block_trades < trades


def simulate_in_blocks(
    rs: Sequence[Decimal], *, paths: int, seed: str, block_trades: int | None = None
) -> Simulated | None:
    """`paths` paths of `len(rs)` trades each, dealt in blocks of `block_trades` trades in a row
    (01/10): a circular moving block bootstrap.

    Each block starts at a trade drawn uniformly, with replacement, and runs on for `block_trades`
    trades in the order they happened; blocks are laid end to end and the last one cut, so each
    path has as many trades as the run. `block_trades` defaults to `default_block_trades(len(rs))`.

    ⚠️ **Circular**: a block that starts near the end wraps round to the first trades. Without the
    wrap a trade near either end sits in fewer possible blocks than one in the middle, and is dealt
    less often — the first and last trades would weigh less for nothing but where they fell. The
    price is a seam joining the last trade to the first, no worse than any other join of blocks.

    ⚠️ **The streak and the drawdown run across the joins.** A block ending in losses followed by
    one starting in losses is one longer streak, as it would be live.

    `None` below `MIN_TRADES`. A `ValueError` for a block that does not fit (`block_fits`) — the
    caller tells the person so before simulating anything.
    """
    if len(rs) < MIN_TRADES:
        return None
    if paths < 1:
        raise ValueError(f"a simulation needs at least one path, got {paths}")
    count = len(rs)
    size = default_block_trades(count) if block_trades is None else block_trades
    if size < MIN_BLOCK_TRADES:
        raise ValueError(f"a block holds at least {MIN_BLOCK_TRADES} trades, got {size}")
    if not block_fits(count, size):
        raise ValueError(f"a block of {size} trades is half or more of {count} trades")

    pool = [float(r) for r in rs]
    # The ring: a block that starts near the end reads straight on into the first trades.
    ring = pool + pool[: size - 1]
    starts = range(count)
    blocks = -(-count // size)
    draw = random.Random(seed)  # noqa: S311 — a simulation, not a secret

    def drawn() -> Iterator[list[float]]:
        for _ in range(paths):
            path: list[float] = []
            for start in draw.choices(starts, k=blocks):
                path.extend(ring[start : start + size])
            del path[count:]
            yield path

    return _measured(drawn(), paths=paths, trades=count, block_trades=size)


__all__ = [
    "DEFAULT_PATHS",
    "MAX_BLOCK_TRADES",
    "MAX_PATHS",
    "MIN_BLOCK_TRADES",
    "MIN_TRADES",
    "Observed",
    "Simulated",
    "Spread",
    "block_fits",
    "default_block_trades",
    "observed",
    "simulate",
    "simulate_in_blocks",
]
