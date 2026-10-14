"""Share splits: adjusting a stored history for them, once, and finding the ones not recorded.

**Why the bars are adjusted on disk and not when read (14/10).** Tradeview sends US shares'
history raw: AMZN's 20-for-1 of June 2022 is a fall from 2 446 to 125 between two bars, and every
backtest, every ML label and every indicator around it read a crash that never happened. At
least four readers open these files — the engine on every worker, the ML features, a live
session's warm-up, the charts — and some on other machines. Adjusting at read time would need
all of them to know the splits; one that forgot would bring the crash back silently. Adjusting
when the collector *writes* puts the rule in one place, and the readers, the workers' caches and
the remote copies (which already follow a file's size and modification time) need no change.

**The rule.** A split of `shares_after` for `shares_before` multiplies the share count, so every
price before it is multiplied by `shares_before / shares_after` (AMZN: ÷ 20). The spread, in
points, is a price distance and is scaled the same way; the real volume, in shares, the other
way. The tick volume counts ticks, not shares, and is left alone.

**Idempotence, which is the whole difficulty.** Dividing twice is worse than not dividing, and the
adjustment is run again for perfectly ordinary reasons: a second split recorded, a command
re-run after a failure. So each Parquet file carries, in its own metadata, the splits it has
already been adjusted for (`SPLITS_KEY`), and the adjustment only applies the ones missing. A
file with no stamp is raw. Bars freshly downloaded are raw by definition, and get every split.
"""

import datetime as dt
import json
import math
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from decimal import ROUND_HALF_EVEN, ROUND_HALF_UP, Decimal
from fractions import Fraction
from itertools import pairwise
from pathlib import Path

import pyarrow.parquet as pq

from tradeforge_collector.storage import SPLITS_KEY, read_candles, symbol_path, write_candles
from tradeforge_collector.timeframes import step
from tradeforge_db.splits import RecordedSplit
from tradeforge_engine.domain import Candle

__all__ = [
    "SPLITS_KEY",
    "Candidate",
    "Rewritten",
    "Split",
    "adjust",
    "already_applied",
    "applied_splits",
    "bring_up_to_date",
    "find_candidates",
    "observed_gap",
    "refuse_unless_the_bars_split",
    "shows_the_split",
    "straddled",
]

_PRICE_PLACES = Decimal(1).scaleb(-10)
"""The storage's own scale (`storage.PRICE`): an adjusted 890.65 / 3 keeps ten places."""


@dataclass(frozen=True, slots=True)
class Split:
    """One split, as the bars see it: the instant the new price starts, and the ratio."""

    effective_at: dt.datetime
    """UTC. A bar that ends by then is scaled, one that starts at or after it is not, and one
    it falls inside is rebuilt (`adjust`)."""
    shares_after: int
    shares_before: int
    key: str
    """What a file's stamp records: `2022-06-06=20:1`. The day and the ratio — not a database
    id — so a file and a registry rebuilt from scratch still recognise each other."""

    @classmethod
    def of(cls, record: RecordedSplit) -> "Split":
        """The instant is the effective day's midnight on the broker's clock, in UTC."""
        midnight = dt.datetime.combine(record.effective_on, dt.time(), tzinfo=dt.UTC)
        return cls(
            effective_at=midnight - record.server_offset,
            shares_after=record.shares_after,
            shares_before=record.shares_before,
            key=f"{record.effective_on:%Y-%m-%d}={record.shares_after}:{record.shares_before}",
        )

    @property
    def price_factor(self) -> Fraction:
        """What a price before the split is multiplied by: 1/20 for a 20-for-1."""
        return Fraction(self.shares_before, self.shares_after)


def adjust(
    candles: Sequence[Candle],
    splits: Iterable[Split],
    *,
    timeframe: str,
    daily: Sequence[Candle] = (),
) -> list[Candle]:
    """The bars as they would read had every one of `splits` already happened.

    A bar that **ends** before a split is scaled; one that starts at or after it is left alone. A
    bar before several splits takes the product of their factors (TSLA's 5-for-1 of 2020 and
    3-for-1 of 2022 make a 2019 bar ÷ 15). Exact: the factor is a `Fraction`, and a price is
    rounded once, to the storage's ten places, after the whole product — not once per split.

    ⚠️ **A bar the split falls inside cannot be scaled at all** — only a week can be one here: a
    weekly bar opens on Sunday, before Monday's split, and holds only prices after it; one with a
    split on a Wednesday holds both kinds, and no factor fixes a high that may have come from
    either half. Found on the dry run of 14/10: AMZN's week of the split, divided by twenty, left
    a 19-fold gap behind it. Such a bar is **rebuilt from `daily`** — the daily bars of its span,
    already adjusted for every split — and is refused if they are not there.
    """
    ordered = sorted(splits, key=lambda split: split.effective_at)
    if not ordered:
        return list(candles)
    length = step(timeframe)

    adjusted: list[Candle] = []
    for candle in candles:
        end = candle.time + length
        if any(candle.time < split.effective_at < end for split in ordered):
            adjusted.append(_rebuilt(candle, end, daily, timeframe))
            continue
        factor = math.prod(
            (split.price_factor for split in ordered if end <= split.effective_at),
            start=Fraction(1),
        )
        adjusted.append(candle if factor == 1 else _scaled(candle, factor))
    return adjusted


def straddled(
    candles: Sequence[Candle], splits: Iterable[Split], timeframe: str
) -> list[tuple[dt.datetime, dt.datetime]]:
    """The spans of the bars a split falls inside — those `adjust` needs daily bars for."""
    length = step(timeframe)
    instants = [split.effective_at for split in splits]
    return [
        (candle.time, candle.time + length)
        for candle in candles
        if any(candle.time < instant < candle.time + length for instant in instants)
    ]


def _rebuilt(candle: Candle, end: dt.datetime, daily: Sequence[Candle], timeframe: str) -> Candle:
    if step(timeframe) <= step("D1"):
        # A daily bar a split falls inside means the broker's day does not start at its clock's
        # midnight, and there is nothing finer stored to rebuild it from.
        raise ValueError(f"a {timeframe} bar at {candle.time} straddles a split")
    inside = [bar for bar in daily if candle.time <= bar.time < end]
    if not inside:
        raise ValueError(
            f"the {timeframe} bar at {candle.time:%Y-%m-%d} holds a split and there are no daily "
            "bars of its span to rebuild it from — collect D1 first"
        )
    return replace(
        candle,
        open=inside[0].open,
        high=max(bar.high for bar in inside),
        low=min(bar.low for bar in inside),
        close=inside[-1].close,
        # The ticks are the week's own count, untouched by any split; the shares are the days'.
        spread=min(bar.spread for bar in inside),
        real_volume=sum(bar.real_volume for bar in inside),
    )


def _scaled(candle: Candle, factor: Fraction) -> Candle:
    def price(value: Decimal) -> Decimal:
        exact = value * factor.numerator / factor.denominator
        return exact.quantize(_PRICE_PLACES, rounding=ROUND_HALF_EVEN)

    # ⚠️ The spread is an integer number of points, so a small one rounds: 3 points ÷ 20 is 0.15,
    # stored as 0. Half up rather than to even, so 0.5 point still costs one. The error is at most
    # half a point either way, against a price the split made twenty times smaller — and, since it
    # rounds at every step, a file adjusted one split at a time can sit one point off a fresh
    # download adjusted for both (NVDA's 4:1 then 10:1). Prices keep ten places and do not drift.
    spread = (Decimal(candle.spread) * factor.numerator / factor.denominator).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    shares = (Decimal(candle.real_volume) * factor.denominator / factor.numerator).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    return replace(
        candle,
        open=price(candle.open),
        high=price(candle.high),
        low=price(candle.low),
        close=price(candle.close),
        spread=int(spread),
        real_volume=int(shares),
    )


# --------------------------------------------------------------------------- #
# The safety check: a split the bars do not show is a split already applied     #
# --------------------------------------------------------------------------- #


def observed_gap(candles: Sequence[Candle], instant: dt.datetime, timeframe: str) -> float | None:
    """The first open at or after `instant` over the close of the last bar that ended before it.

    `None` when there is nothing on one side to compare with. "Ended", not "opened": a weekly bar
    that opened before Monday's split closed after it, at the new price.
    """
    length = step(timeframe)
    before = [candle for candle in candles if candle.time + length <= instant]
    after = [candle for candle in candles if candle.time >= instant]
    if not before or not after or before[-1].close == 0:
        return None
    return float(after[0].open / before[-1].close)


def shows_the_split(observed: float, split: Split) -> bool:
    """Is the gap nearer the split's ratio than no gap at all? Compared as logarithms.

    ⚠️ **A sanity check, not a proof.** A 20-for-1 against a day's move is unmistakable; a 10 %
    bonus issue against a 5 % fall is not. What it does catch for certain is the case that
    matters most: bars already adjusted by the broker (XP's are) show *no* gap, and dividing them
    again would put a fake gap where there was none.
    """
    if observed <= 0:
        return False
    gap = math.log(observed)
    return abs(gap - math.log(split.price_factor)) < abs(gap)


def refuse_unless_the_bars_split(
    candles: Sequence[Candle], splits: Iterable[Split], *, timeframe: str, where: str
) -> None:
    """Raise if any split falls inside these bars and the bars show no gap there.

    Asked of raw bars about to be adjusted — a download, a daily series before a split is
    recorded. A split outside the bars cannot be checked here and passes.
    """
    for split in splits:
        observed = observed_gap(candles, split.effective_at, timeframe)
        if observed is not None and not shows_the_split(observed, split):
            raise ValueError(
                f"{where}: the bars show no {split.shares_after}-for-{split.shares_before} gap at "
                f"{split.key.split('=')[0]} (open/close {observed:.4f}, expected about "
                f"{float(split.price_factor):.4f}). Already adjusted by the broker, or the wrong "
                "day — refusing to divide them"
            )


# --------------------------------------------------------------------------- #
# Files: what each one has been adjusted for, and bringing them up to date      #
# --------------------------------------------------------------------------- #


def already_applied(root: Path, symbol: str, split: Split) -> bool:
    """Do the stored daily bars already carry this split in their stamp?

    True when the files were adjusted for it and the record of it was lost — a database restored
    from an older backup. Recording it again is then right, and checking the bars for its gap is
    wrong: they no longer have one.
    """
    directory = symbol_path(root, symbol) / "timeframe=D1"
    return any(split.key in applied_splits(year) for year in directory.glob("year=*"))


def applied_splits(year_directory: Path) -> frozenset[str]:
    """The splits the files of one year partition are adjusted for. Empty when raw.

    ⚠️ Every file in the partition must agree. Two that do not would mean one half of a year was
    divided and the other not — no rewrite can reconcile that, so it is refused, not guessed.
    """
    stamps: set[frozenset[str]] = set()
    for path in sorted(year_directory.glob("*.parquet")):
        metadata = pq.read_schema(path).metadata or {}
        raw = metadata.get(SPLITS_KEY)
        stamps.add(frozenset(json.loads(raw)) if raw else frozenset())
    if len(stamps) > 1:
        raise ValueError(f"the files under {year_directory} disagree about their splits")
    return next(iter(stamps), frozenset())


@dataclass(frozen=True, slots=True)
class Rewritten:
    """One year partition the adjustment changed."""

    timeframe: str
    year: int
    candles: int


def bring_up_to_date(root: Path, symbol: str, splits: Sequence[Split]) -> list[Rewritten]:
    """Adjust every stored year of `symbol`, at every timeframe, for the splits it lacks.

    Idempotent: a partition already stamped with all of `splits` is not opened twice, and one
    the missing splits would not change (every bar after them) is left as it is — rewriting it
    would only move its modification time and send it to every remote worker again.

    Shortest timeframe first, so the daily bars are up to date before a week is rebuilt from them.
    """
    keys = {split.key for split in splits}
    rewritten: list[Rewritten] = []
    directories = sorted(
        symbol_path(root, symbol).glob("timeframe=*"),
        key=lambda directory: step(directory.name.removeprefix("timeframe=")),
    )
    for timeframe_directory in directories:
        timeframe = timeframe_directory.name.removeprefix("timeframe=")
        for year_directory in sorted(timeframe_directory.glob("year=*")):
            year = int(year_directory.name.removeprefix("year="))
            stamped = applied_splits(year_directory)
            missing = [split for split in splits if split.key not in stamped]
            if not missing:
                continue
            first = dt.datetime(year, 1, 1, tzinfo=dt.UTC)
            last = dt.datetime(year, 12, 31, 23, 59, 59, 999999, tzinfo=dt.UTC)
            candles = read_candles(root, symbol, timeframe, start=first, end=last)
            spans = straddled(candles, missing, timeframe)
            daily = (
                read_candles(root, symbol, "D1", start=spans[0][0], end=spans[-1][1])
                if spans
                else []
            )
            adjusted = adjust(candles, missing, timeframe=timeframe, daily=daily)
            if adjusted == candles:
                continue
            write_candles(root, symbol, timeframe, adjusted, splits=stamped | keys)
            rewritten.append(Rewritten(timeframe=timeframe, year=year, candles=len(adjusted)))
    return rewritten


# --------------------------------------------------------------------------- #
# Finding splits nobody recorded                                                #
# --------------------------------------------------------------------------- #

_CLEAN_RATIOS: tuple[tuple[int, int], ...] = (
    *((k, 1) for k in (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)),
    *((1, k) for k in (2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 100)),
    (3, 2),
    (2, 3),
)
"""(shares after, shares before) a split is announced in. A 10 % bonus (11:10) is a real event
and recordable, but a 9 % gap is a Tuesday: it is not searched for."""

_SMALLEST_GAP = math.log(1.3)
"""A gap under 30 % is not looked at. Earnings moves of 25-40 % do happen (AMD +42 %, NFLX
-30 % in 2022); what separates them from splits is the next test, not this one."""

_TOLERANCE = 0.04
"""How far, as a log ratio (~4 %), a gap may sit from a clean ratio and still be called one.
The ten real splits found on 14/10 sat within 2.5 %; the nearest earnings gap, AMD's +42.7 %
against 3:2's 50 %, sits at 5 %."""


@dataclass(frozen=True, slots=True)
class Candidate:
    """A gap in a daily series that looks like a split nobody recorded."""

    effective_on: dt.date
    """The first day at the new price, on the broker's clock — what `splits add` takes."""
    shares_after: int
    shares_before: int
    observed: float
    """The first open over the previous close, as found."""


def find_candidates(daily: Sequence[Candle], server_offset: dt.timedelta) -> list[Candidate]:
    """Gaps between two daily bars that sit on a clean split ratio. Raw data only, obviously."""
    found: list[Candidate] = []
    for previous, current in pairwise(daily):
        if previous.close <= 0 or current.open <= 0:
            continue
        observed = float(current.open / previous.close)
        gap = math.log(observed)
        if abs(gap) < _SMALLEST_GAP:
            continue
        after, before = min(
            _CLEAN_RATIOS, key=lambda ratio: abs(gap - math.log(ratio[1] / ratio[0]))
        )
        if abs(gap - math.log(before / after)) <= _TOLERANCE:
            found.append(
                Candidate(
                    effective_on=(current.time + server_offset).date(),
                    shares_after=after,
                    shares_before=before,
                    observed=observed,
                )
            )
    return found
