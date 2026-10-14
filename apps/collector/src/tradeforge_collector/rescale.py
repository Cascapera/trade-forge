"""A broker that rewrites a series' past, and the seam that leaves on disk (14/10).

Some series are not a record of what traded but a construction a broker keeps redoing. XP's
`WIN$` is the mini-index future made continuous by **adjusting every past bar by a ratio at each
roll**: on 14/10/2026, the day WINV26 expired, the whole history moved up 2.1 % — 4 000 to 4 500
points, more where the price was higher. Its shares come adjusted for dividends the same way, and
each new dividend rescales them again.

The bars already on disk are in the scale of the day they were collected. Whatever is collected
after a rescale — a year asked again, the live loop catching up from the newest bar on disk —
lands beside them in the new one, and the series gets a step that never traded. Measured that
day: the live sessions warmed on the disk up to 09/10 and went on with the broker's bars from
there, the step read as a close above the high of 05/10 — a bullish BOS the market never made —
and the continuation setup armed a long on it (signal #38, annulled).

So a collection asks first whether the broker's series still matches the disk: the same bars,
asked again, compared close for close (`compare`). Equal, and the collection goes ahead as asked.
Rescaled, and every year on disk is collected again with it, so the files stay one scale — and if
the broker no longer serves the oldest years on disk, nothing is written at all, because
collecting the recent ones alone would make the seam rather than mend it.

⚠️ **Why the same bars and not a jump in price.** A step in the series is not evidence: the index
opened 9.2 % up on 05/10, the Monday after the election, and that gap was real. Only one bar in
two versions proves the broker rewrote it.
"""

import statistics
from collections.abc import Sequence
from dataclasses import dataclass

from tradeforge_engine.domain import Candle

TOLERANCE = 0.0002
"""How far, as a share of the price, the bars may sit from the disk's and still be the same
series: 0.02 %, a hundred times below the roll of 14/10 (2.1 %) and well below a dividend's
adjustment, above any re-quote of a close the broker revised."""

SAMPLE = 200
"""Bars back from the newest on disk the comparison reads: a day of M5, ten months of D1."""

ENOUGH = 20
"""Fewer bars than this in both versions and nothing can be said either way."""

ONE_FACTOR = 0.25
"""How spread the ratios may be, as a share of their distance from 1, and still be one factor:
a rescale moves the whole past by the same amount (the ratios of 14/10 sat between 1.0211 and
1.0216 over a day of M5). Bars that differ each by its own amount are some other disagreement —
a different feed, revised bars — and collecting every year again would not mend it."""

__all__ = ["ENOUGH", "ONE_FACTOR", "SAMPLE", "TOLERANCE", "Comparison", "compare"]


@dataclass(frozen=True, slots=True)
class Comparison:
    """The bars two versions of a series share, and the median ratio of their closes."""

    matched: int
    ratio: float
    """Broker's close over the disk's, the median of the matched bars: 1.021 on 14/10."""
    dispersion: float = 0.0
    """How far apart the ratios are: the 90th percentile less the 10th."""

    @property
    def conclusive(self) -> bool:
        """Enough shared bars to say anything."""
        return self.matched >= ENOUGH

    @property
    def differs(self) -> bool:
        """The broker's past is not the disk's — by one factor or not."""
        return self.conclusive and abs(self.ratio - 1) > TOLERANCE

    @property
    def rescaled(self) -> bool:
        """The broker's past is the disk's, times one factor."""
        return self.differs and self.dispersion <= ONE_FACTOR * abs(self.ratio - 1)


def compare(stored: Sequence[Candle], fresh: Sequence[Candle]) -> Comparison:
    """`stored` (the disk's, in time order) against `fresh` (the broker's, asked again), by time.

    The newest stored bar is left out: until 14/10 a collection could write the bar still
    forming, and its close is not the one the broker has now. The median, not the mean, so one
    bar the broker revised cannot pass for a rescale and one the disk got wrong cannot hide one.
    """
    by_time = {candle.time: candle for candle in fresh}
    ratios = [
        float(by_time[candle.time].close / candle.close)
        for candle in stored[:-1]
        if candle.time in by_time and candle.close > 0
    ]
    if not ratios:
        return Comparison(matched=0, ratio=1.0)
    if len(ratios) == 1:
        return Comparison(matched=1, ratio=ratios[0])
    deciles = statistics.quantiles(ratios, n=10)
    return Comparison(
        matched=len(ratios), ratio=statistics.median(ratios), dispersion=deciles[-1] - deciles[0]
    )
