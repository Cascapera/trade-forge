"""The higher-timeframe filter on the structure setups: a region above releases one entry below.

Dictated 2026-09-08, the first thing after he closed the list of triggers: *"podemos usar qualquer
região do time frame superior [...] quando o preço atingir uma região no H4 qualquer que ainda não
foi mitigada, a partir deste ponto ele libera de fazer uma entrada no 15 minutos, e é somente uma
entrada por região de H4"*. It is a **filter**, chosen next to the entry — CHoCH on M15 with an H4
above it — and it says nothing about *how* the entry is made, only *whether it may be looked for*.

**The rule, in his example** (a demand region of [90, 100] on the H4, trading the M15):

* Until price reaches an H4 region nobody has touched, the M15 is deaf: nothing is armed.
* Price touches or enters the region. From that bar on, the M15 may take **one** entry.
* That one entry ends — stop, target, or the order withdrawn unfilled — and the M15 is deaf again
  until price reaches *another* untouched H4 region.
* Released and still without an entry, the search ends when the market walks away: price reaches
  120, the far edge plus **two heights** of the region (*"o equivalente a 2x a região"*); or price
  closes through the region the other way, below 90.

**His seven answers on the edges** (2026-09-08), each of which is a line below:

1. The touch that releases the M15 **is** the touch that mitigates the region — his rule that a
   region is taken by the first wick to its edge (`TrackedZone`). A second visit releases nothing.
2. The two heights count on the **wick**, not the close.
3. An order placed and withdrawn unfilled **spends** the region's one chance. Different from the
   M15 zone itself, which burns on the fill (ADR-0015) — two rules, both his.
4. The M15 zone may sit **outside** the H4 region.
5. The break that qualifies the M15 zone has to confirm **after** the touch: a change of character
   from before price reached the region is not the reaction to it (*"a partir dali"*).
6. Price closing through the region ends the search, on his own example: released, no entry, a
   bar closes at 85 under [90, 100] — stop looking for the buy. **Close**, not wick: read off that
   example rather than asked, and confirmed on 2026-09-09 (*"3 - correto"*).
7. The same rule serves the continuation setup — it is the structure family's filter, not the
   CHoCH's.

**And four more he confirmed on 2026-09-09**, having been readings of ours when this shipped
(*"1 - correto / 2 - correto / 3 - correto / 4 - sim"*): the reference is the deepest region the
bar reached; a region reached and closed through on one bar spends itself and releases nothing;
the break may confirm on the touching bar itself; and the close, above. Each is credited where it
runs, because a rule that says it is ours is the rule somebody loosens later.

**Which regions may release, his choice since 2026-09-29** (`RegionChoice`, and whether the
secondary regions of an impulse count). By default any region above that nobody has touched — the
rule above, and every result recorded before. Asked whether it should be any region or only one
in favour of the last structure (*"se ele fez um choch de alta ou bos de alta para ele não procurar
venda em uma região antiga demais"*), he wanted both on offer, and primaries only beside all. With
`WITH_TREND`, his three answers of the same day:

* Only a region **of the last leg** releases — one the last break of the timeframe above offered.
  Those point the way that break left the trend (a bullish break offers demand), so they are the
  regions in favour of it; an older one of the same side does not count. A break that offers no
  region empties the leg rather than keeping the one before (*"se teve quebra quando ativar a
  região vai estar ao mesmo tempo invertendo a tendência"*): by the time price reaches a region of
  that older leg, the move reaching it is already turning the trend.
* A region that does not qualify **still dies at its first touch**, and releases nothing — his
  mitigation rule is not suspended by the filter.
* A side released and still without an entry **is shut when the structure above turns against
  it** — a change of character the other way ends the search for the buy.

**Where the H4 comes from.** The engine hands a strategy one stream of bars (`Context` carries
one candle, and that is the anti-lookahead rule made structural). So the higher timeframe is
**assembled here**, from the bars the setup already receives, and a region on it exists from the
close of the H4 bar that revealed it — the moment his chart would draw it, and never earlier.
ADR-0026 has the alternatives.

⚠️ **The bars close on the broker's clock.** *"Sempre levar em consideração o horário do MT5"*
(2026-09-09). A MetaTrader chart closes its H4 at 00:00, 04:00 and 08:00 **server** time, and
the collector converts everything to UTC before storing it — so an aggregator anchored on UTC
cuts the bars somewhere else entirely, and with a broker three hours
ahead every region comes out three hours displaced from the one he is looking at. Plausible, and
wrong. `offset` is how far the broker's clock runs ahead of UTC, the same number and the same
vocabulary the collector takes as `--server-offset`. Since 2026-09-30 it is the **instrument's**
(`InstrumentSpec.server_offset`), never the strategy's: a document stated it from 09/09, and from
26/09 one that did not was cut on UTC — which put a broker's 21:00 H4 bar across the UTC midnight
of the D1 above and refused it. The gate itself still takes the offset as a number, never
measured — a measured clock is a nondeterministic one.
"""

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from tradeforge_engine.domain import MAX_SERVER_OFFSET, Candle, Money, Side
from tradeforge_engine.errors import EngineError
from tradeforge_engine.structure import (
    MarketStructure,
    OrderBlock,
    OrderBlockDetector,
    TrackedZone,
    Trend,
    ZoneKind,
)

GIVE_UP_AT_REGION_HEIGHTS: Final = Decimal(2)
"""How far past the region price may run, released and without an entry, before the search ends.

His number, in his example: a demand region of [90, 100] — ten high — and the M15 stops looking
for the buy when price reaches 120, the top plus two heights. Read off the bar's high, because
*"atingir"* is price trading there, not settling there.
"""


class RegionChoice(StrEnum):
    """Which untouched regions above may release the timeframe below — see the module docstring."""

    ANY = "any"
    """Any region nobody has touched, whichever way the structure above points: the rule of
    2026-09-08 and the default."""
    WITH_TREND = "with_trend"
    """Only a region the last break above offered — in favour of the structure it left."""


# A Monday at midnight. Every bucket is a whole number of `target` from here, so an H1, H4 or D1
# bucket starts on the hour, on a four-hour boundary or at midnight, and a W1 bucket on a Monday.
# The epoch itself (a Thursday) would put the week's edge on the wrong day. Read as an instant on
# the **broker's** clock, not on UTC — `BarAggregator` shifts it by the offset.
_ANCHOR: Final = dt.datetime(1970, 1, 5, tzinfo=dt.UTC)


class BarAggregator:
    """Folds bars of one timeframe into bars of a coarser one, closing each on the broker's clock.

    A bar of the target timeframe is **complete** on the base bar that ends exactly at its
    boundary, and it is returned from that base bar's `update` — so a strategy reading the H4 on
    the 03:45 M15 sees the H4 that closed at 04:00 on the same call, not one bar later. The
    open is the first base bar's, the close the last one's, the extremes the widest, and the
    volumes are summed. `spread` is the widest seen, which is the honest reading of a number the
    structure never reads anyway.

    ⚠️ **A gap closes the bucket that was open.** Sessions end, weekends happen, and the base bar
    that would have ended the bucket never arrives. The first bar of a *later* bucket therefore
    flushes the one still open — one base bar late, and late is the only honest reading: nothing
    could have known the session was over until the next bar said so. A bar that both flushes an
    old bucket and ends its own returns both, oldest first.

    A partial bucket is never returned: the run ends, and whatever was accumulating is simply not
    a closed bar. Reading it would be reading a bar that has not closed.

    ⚠️ **`offset` is where the broker's day starts**, in hours ahead of UTC — his rule, and the
    module docstring says why. Every boundary is measured from midnight on *that* clock, so a
    broker three hours ahead closes its H4 bars at 21:00, 01:00 and 05:00 UTC. Zero means the
    broker keeps UTC, which is a claim about a real terminal rather than a convenient default,
    and that is why nothing here supplies one.

    ⚠️ **A base bar that straddles a boundary raises**, rather than being folded into whichever
    bucket its opening instant fell in. It cannot happen when the offset describes the clock the
    candles were actually collected under; it happens immediately when it does not — an offset of
    ten minutes against M15 bars, say — and the failure it prevents is the expensive one: buckets
    that close a little late, regions displaced by a little, and every number still plausible.
    """

    def __init__(self, *, base: dt.timedelta, target: dt.timedelta, offset: dt.timedelta) -> None:
        if base <= dt.timedelta(0):
            raise ValueError(f"the base timeframe must be positive, got {base}")
        if target <= base:
            raise ValueError(f"the higher timeframe must be coarser than {base}, got {target}")
        if target % base != dt.timedelta(0):
            raise ValueError(f"the higher timeframe {target} is not a whole number of {base} bars")
        if abs(offset) > MAX_SERVER_OFFSET:
            raise ValueError(
                f"a broker's clock sits within {MAX_SERVER_OFFSET} of UTC, got {offset}"
            )
        self._base = base
        self._target = target
        # Midnight on the broker's clock, expressed in UTC — the instant every boundary counts
        # from. Computed once: the alternative is adding and subtracting the offset on both sides
        # of every comparison, which is the same arithmetic written three times.
        self._origin = _ANCHOR - offset
        self._offset = offset
        self._bucket: dt.datetime | None = None
        self._open: Money = Decimal(0)
        self._high: Money = Decimal(0)
        self._low: Money = Decimal(0)
        self._close: Money = Decimal(0)
        self._tick_volume = 0
        self._real_volume = 0
        self._spread = 0

    @property
    def target(self) -> dt.timedelta:
        return self._target

    @property
    def offset(self) -> dt.timedelta:
        """How far the broker's clock runs ahead of UTC."""
        return self._offset

    def bucket_of(self, moment: dt.datetime) -> dt.datetime:
        """The opening instant, in UTC, of the target bar `moment` falls in.

        Counted from midnight on the broker's clock, so with a broker three hours ahead an H4
        bucket opens at 21:00, 01:00 and 05:00 UTC — the instants his chart draws.
        """
        return self._origin + ((moment - self._origin) // self._target) * self._target

    def update(self, candle: Candle) -> tuple[Candle, ...]:
        """Fold in one closed base bar; return the target bars it completed, oldest first."""
        completed: list[Candle] = []
        bucket = self.bucket_of(candle.time)
        if candle.time + self._base > bucket + self._target:
            raise EngineError(
                f"the bar at {candle.time} spans the boundary at {bucket + self._target}: "
                f"a {self._base} bar cannot belong to one {self._target} bucket under a broker "
                f"offset of {self._offset}"
            )
        if self._bucket is not None and bucket != self._bucket:
            completed.append(self._flush())
        if self._bucket is None:
            self._bucket = bucket
            self._open, self._high, self._low, self._close = (
                candle.open,
                candle.high,
                candle.low,
                candle.close,
            )
            self._tick_volume, self._real_volume, self._spread = (
                candle.tick_volume,
                candle.real_volume,
                candle.spread,
            )
        else:
            self._high = max(self._high, candle.high)
            self._low = min(self._low, candle.low)
            self._close = candle.close
            self._tick_volume += candle.tick_volume
            self._real_volume += candle.real_volume
            self._spread = max(self._spread, candle.spread)
        if candle.time + self._base >= bucket + self._target:
            completed.append(self._flush())
        return tuple(completed)

    def _flush(self) -> Candle:
        assert self._bucket is not None  # noqa: S101 — internal invariant, callers check first
        bar = Candle(
            time=self._bucket,
            open=self._open,
            high=self._high,
            low=self._low,
            close=self._close,
            tick_volume=self._tick_volume,
            spread=self._spread,
            real_volume=self._real_volume,
        )
        self._bucket = None
        return bar


@dataclass(frozen=True, slots=True)
class Release:
    """One side of the M15 released by one H4 region, from one bar on.

    `block` is the region price came to — the one the two heights and the break are measured
    against — and `opened_at` is the base bar that touched it, which is the earliest bar whose
    break may qualify an entry (his answer 5).
    """

    block: OrderBlock
    opened_at: dt.datetime


class RegionTracker:
    """The higher timeframe's regions and which of them the base bars have reached — nothing more.

    ⚠️ **Pure: a function of the base bars, the higher timeframe and the broker's clock alone**
    (ADR-0029). Nothing a setup does — arming, spending a side, a refusal — reaches it; those
    live in `HigherTimeframeGate`, which reads this. That is what lets one tracker be read by
    many runs of a sweep over the same market, and why it was split out of the gate (27/09).

    Fed every base bar through `observe`, which assembles the bars above, runs his structure and
    region detectors on them, and records the regions this bar reached (`reached`) at the base
    bar's resolution — the gate says why the touch is read there and not on the higher bar.

    **One touch per region, ever.** A region enters `_touched` on the bar that reaches it and never
    leaves, so the same region is reported as reached once — his answer 1, and a set that is only
    ever asked about membership, so nothing about its order can reach a result (`AGENTS.md §5.2`).
    """

    def __init__(self, *, base: dt.timedelta, target: dt.timedelta, offset: dt.timedelta) -> None:
        self._bars = BarAggregator(base=base, target=target, offset=offset)
        self._structure = MarketStructure()
        self._blocks = OrderBlockDetector()
        self._touched: set[OrderBlock] = set()
        # ⚠️ **The regions a base bar can still touch, in the detector's order.** Every region
        # already in `_touched` would be skipped anyway; asking the set about each of them on every
        # base bar re-hashed a frozen dataclass two hundred times a bar — 6.6 million hashes in one
        # CHOCH run with an H4 gate (24/09). The detector's list only changes when a higher bar
        # offers a region, so this is rebuilt there, and shrinks on the bar a region is reached.
        self._untouched: list[OrderBlock] = []
        self._reached: dict[Side, tuple[OrderBlock, ...]] = {}
        # The last break above: the trend it left, and the regions it offered (possibly none).
        # Kept for every run alike — which of them may release is the gate's choice, not this.
        self._trend: Trend | None = None
        self._last_leg: frozenset[OrderBlock] = frozenset()

    @property
    def timeframe(self) -> dt.timedelta:
        """How long one bar of the higher timeframe lasts."""
        return self._bars.target

    @property
    def offset(self) -> dt.timedelta:
        """How far the broker's clock runs ahead of UTC — where its bars are cut."""
        return self._bars.offset

    @property
    def zones(self) -> tuple[TrackedZone, ...]:
        """Every region the higher timeframe has offered, oldest first — the detector's own view.

        ⚠️ `mitigated` here is on the higher timeframe's schedule; whether a region has been
        *reached* at the base bar's resolution is this class's own record, and a region can be
        reached here and still read as standing there until its H4 bar closes.
        """
        return self._blocks.zones

    @property
    def trend(self) -> Trend | None:
        """The trend the last break of the timeframe above left in force; `None` before any."""
        return self._trend

    def of_last_leg(self, block: OrderBlock) -> bool:
        """Whether the last break above offered this region — his "in favour" (2026-09-29)."""
        return block in self._last_leg

    @property
    def reached(self) -> Mapping[Side, tuple[OrderBlock, ...]]:
        """The regions the last observed base bar reached for the first time, per side, oldest
        offered first. Empty on a bar that reached none.

        ⚠️ **Read-only by type, not by promise** (27/09): a tracker read by a whole batch of runs
        hands every one of them this same mapping, and a run that popped a region from it would
        take it from the others without an error anywhere. A frozen mapping of tuples makes that
        write a type error, and a `TypeError` if anyone tries anyway.
        """
        return MappingProxyType(self._reached)

    def observe(self, candle: Candle) -> None:
        """Fold in one closed base bar: assemble the bar above, then record what this bar reached.

        The higher bar is completed first, so a region revealed by the H4 that closed on this very
        base bar is already known when this bar is read against it — that is the earliest his
        chart would show it, and this bar's own low is part of that H4's low, so the detector will
        not offer a region this bar already took.
        """
        changed = False
        for bar in self._bars.update(candle):
            # ⚠️ **Only a bar that offered a region can change the detector's list** — it extends
            # the list and trims it back to its bound in that same branch, and nowhere else. On
            # every other higher bar the list is the one already read: `_touched` holds nothing
            # outside it (it only ever gains regions of `_untouched`), and `_untouched` has been
            # kept equal to the list minus `_touched` bar by bar. Rebuilding both there anyway was
            # half of a CHOCH run with an M15 gate over M5 — 52 million hashes of a frozen
            # dataclass in 372 thousand bars (27/09). Still pruned bar by bar when it does change,
            # which is what keeps `_touched` no larger than the detector's bounded list.
            broke = self._structure.update(bar)
            offered = self._blocks.update(bar, broke)
            if broke is not None:
                self._trend = broke.trend
                self._last_leg = frozenset(offered)
            if offered:
                held = {tracked.block for tracked in self._blocks.zones}
                self._touched.intersection_update(held)
                changed = True
        if changed:
            self._untouched = [
                tracked.block
                for tracked in self._blocks.zones
                if tracked.block not in self._touched
            ]

        reached: dict[Side, list[OrderBlock]] = {}
        for block in self._untouched:
            # The same two questions as ever, the cheap one first: only a region the bar reaches
            # is looked up. Still asked, because two equal regions in the detector's list are one
            # region here — the first reached spends the second.
            if not _reaches(block, candle) or block in self._touched:
                continue
            self._touched.add(block)
            reached.setdefault(block.side, []).append(block)
        if reached:
            self._untouched = [block for block in self._untouched if block not in self._touched]
        self._reached = {side: tuple(blocks) for side, blocks in reached.items()}


class HigherTimeframeGate:
    """His filter: may a zone of the base timeframe be armed, given the regions above it?

    Fed every base bar through `observe`, **before** anything asks it a question on that bar. It
    reads the regions above and what each base bar reached from its `RegionTracker` — the same
    two detectors the setup runs on its own bars, on the higher bars — and keeps, per side,
    whether the search is open and which region opened it. The tracker is pure; what this class
    keeps is not: a side is spent by the setup's arming and handed back by a refusal (ADR-0029).

    **The touch is read on the base bar, not on the higher one.** The detector above marks a
    region mitigated when the H4 bar that reached it closes, up to sixteen M15 bars after the
    wick that did it. Releasing the M15 on that schedule would release it late; so this class
    keeps its own record of which regions have been reached, at the base bar's resolution, and
    reads the detector only for which regions exist. The two agree on *whether* a region was
    taken, and disagree only about *when* — which is the whole reason for the second record.

    **One release per region, ever.** The tracker reports a region as reached once, on the bar
    that first reaches it — his answer 1.

    **The reference is the region price is at; the release keeps its first bar.** Released by
    region A and, still without an entry, price reaches region B below it: the search stays open,
    B is now the region the two heights and the break are measured from, and *from that bar on*
    still means from the touch of A — the side has been released since then, and a break that
    confirmed between the two touches was a reaction to a region price had come to. Price is
    working B, not A; ending the search at A's far edge plus two heights while price sits in B
    would be ending it on a region price has already left. Several regions reached on one bar —
    his impulse leaves a primary and a secondary, and one bar can fall through both — spend all
    of them and leave the **innermost** as the reference, the one the bar's extreme is in.
    Measured, not reasoned: with the newer region as the reference the very first scenario probed
    released and ended on the same bar, because that bar had closed through the secondary on its
    way down to the primary. A region reached and closed through on the same bar spends itself
    and releases nothing. Both were readings of ours, and both are his answers since 2026-09-09
    (*"1 - correto / 2 - correto"*).
    """

    def __init__(  # noqa: PLR0913 — keyword-only; the bars, the clock and his two choices
        self,
        *,
        base: dt.timedelta,
        target: dt.timedelta,
        offset: dt.timedelta,
        regions: RegionTracker | None = None,
        choice: RegionChoice = RegionChoice.ANY,
        allow_secondary: bool = True,
    ) -> None:
        # Which regions may release (2026-09-29): the run's own choice, read against the tracker's
        # pure record — so runs choosing differently still share one tracker in a batch.
        self._choice = choice
        self._allow_secondary = allow_secondary
        # ⚠️ **A tracker handed in is advanced by whoever handed it in**, never here (ADR-0029): a
        # `MarketReading` owns it — the setup's own, or a batch's leader shared by many runs. The
        # gate then only reads what the bar reached. Without one the gate builds and advances its
        # own, which is every use of the gate on its own.
        if regions is not None and (regions.timeframe, regions.offset) != (target, offset):
            raise ValueError(
                f"the tracker reads {regions.timeframe} cut at {regions.offset}, "
                f"not {target} cut at {offset}"
            )
        self._owns_regions = regions is None
        self._regions = (
            RegionTracker(base=base, target=target, offset=offset) if regions is None else regions
        )
        self._releases: dict[Side, Release] = {}

    @property
    def timeframe(self) -> dt.timedelta:
        """How long one bar of the higher timeframe lasts."""
        return self._regions.timeframe

    @property
    def offset(self) -> dt.timedelta:
        """How far the broker's clock runs ahead of UTC — where its bars are cut."""
        return self._regions.offset

    @property
    def zones(self) -> tuple[TrackedZone, ...]:
        """Every region the higher timeframe has offered, oldest first (`RegionTracker.zones`)."""
        return self._regions.zones

    def observe(self, candle: Candle) -> None:
        """Fold in one closed base bar: the tracker reads it, then the releases follow.

        Order matters and is deliberate: the regions first (`RegionTracker.observe`), then the
        touches become releases, then the endings — a bar that reaches a new region and runs two
        heights past it in one go releases and ends on the same call, which is what his rule says
        about that bar.
        """
        if self._owns_regions:
            self._regions.observe(candle)
        for side, reached in self._regions.reached.items():
            # A region that does not qualify was still reached, and the tracker has spent it: it
            # dies at its first touch and releases nothing (his answer, 2026-09-29).
            blocks = [block for block in reached if self._qualifies(block)]
            if not blocks:
                continue
            # A side already released keeps the bar that released it: *from that bar on* is
            # counted from the first region price came to, and only the reference moves. A region
            # reached and closed through on this same bar opens a release here and loses it in
            # the loop below, which is the same as never having opened it.
            standing = self._releases.get(side)
            opened_at = candle.time if standing is None else standing.opened_at
            self._releases[side] = Release(block=_innermost(blocks), opened_at=opened_at)

        for side in (Side.LONG, Side.SHORT):
            release = self._releases.get(side)
            if release is not None and (
                _search_over(release.block, candle) or not self._trend_allows(side)
            ):
                del self._releases[side]

    def _qualifies(self, block: OrderBlock) -> bool:
        """May this region, reached, release its side? Always, under the defaults."""
        if not block.primary and not self._allow_secondary:
            return False
        return self._choice is RegionChoice.ANY or self._regions.of_last_leg(block)

    def _trend_allows(self, side: Side) -> bool:
        """Whether the structure above lets this side stay released — always under `ANY`; under
        `WITH_TREND`, not once the last break above points the other way (his answer, 29/09)."""
        if self._choice is RegionChoice.ANY:
            return True
        trend = self._regions.trend
        return trend is None or (trend is Trend.BULLISH) == (side is Side.LONG)

    def shut_on_this_bar(self) -> bool:
        """No side released now, and the base bar just read released none — asked before
        `observe` on that bar, with the tracker already at it. Such a bar can end a search but
        cannot open one, so `allows` answers no to every zone on it (`StructureStrategy.quiet`).
        """
        return not self._releases and not self._regions.reached

    def allows(self, block: OrderBlock) -> bool:
        """May this zone of the base timeframe be armed now?

        Yes only while its side is released, and only for a zone whose break confirmed on or after
        the bar that released it (his answer 5). `confirmed_at` is the opening time of the bar
        whose close broke structure; `opened_at` the opening time of the bar that touched the
        region. A break on the touching bar itself counts — *from that bar on*, and he confirmed
        the boundary reading on 2026-09-09 (*"4 - sim"*): the touching bar yes, the one before no.
        """
        release = self._releases.get(block.side)
        return release is not None and block.confirmed_at >= release.opened_at

    def release_for(self, block: OrderBlock) -> Release | None:
        """The release standing on this zone's side, or `None` if the side is shut."""
        return self._releases.get(block.side)

    def reference(self, block: OrderBlock) -> OrderBlock | None:
        """The higher-timeframe region that released this zone's side, or `None` if it is shut."""
        release = self.release_for(block)
        return None if release is None else release.block

    def spend(self, side: Side) -> None:
        """The one entry this release allowed has been taken; the side is shut until a new touch.

        Called when the setup **arms** a zone — the moment it has chosen its entry — rather than
        when the order fills or ends. His answer 3 draws the line there: an order placed and
        withdrawn unfilled has spent the region, so nothing later than the arming can be the
        event. Whatever becomes of that order is the setup's own business, with one exception,
        `restore`.
        """
        self._releases.pop(side, None)

    def restore(self, release: Release, candle: Candle) -> None:
        """Hand a spent release back: the order it paid for never reached the book.

        His answer 3 is about an order *placed* and withdrawn; an order the venue turned away at
        the gate — volume above the cap, trading disabled, the session's hand-over order that is
        always refused (ADR-0023) — was never placed, and spending the region's one chance on it
        would burn the first release of every paper and live session on an order nobody saw. His
        answer, shown the two cases (2026-09-08): *"1 ok"* to this, and *"como está tá bom"* to
        spending on the arming rather than on the placing.

        A refusal reaches the setup one bar late (`Context.refusals`), so `candle` is that later
        bar, and the release comes back only if the search would still be open on it: not if the
        market has since run two heights past or closed through the region, and not if a newer
        touch has already released the side on its own terms.

        ⚠️ **Not for an order the market withdrew.** `RefusedBy.MARKET` is an order that rested
        and was taken back because price moved through it — placed and withdrawn unfilled, which
        is exactly the case his answer says spends the region. The caller keeps the two apart.
        """
        side = release.block.side
        if (
            side in self._releases
            or _search_over(release.block, candle)
            or not self._trend_allows(side)
        ):
            return
        self._releases[side] = release


def _innermost(blocks: Sequence[OrderBlock]) -> OrderBlock:
    """Of several regions one side reached on one bar, the one price went deepest into.

    The lowest top among demand regions, the highest bottom among supply ones. `min`/`max` keep
    the first of equals, and the list arrives oldest-offered first, so a tie is the older region
    — deterministic, and not a case any real impulse produces.
    """
    if blocks[0].kind is ZoneKind.DEMAND:
        return min(blocks, key=lambda block: block.top)
    return max(blocks, key=lambda block: block.bottom)


def _reaches(block: OrderBlock, candle: Candle) -> bool:
    """His touch: the first wick to the region's entry edge, the reading `TrackedZone` uses."""
    if block.kind is ZoneKind.DEMAND:
        return candle.low <= block.top
    return candle.high >= block.bottom


def _search_over(block: OrderBlock, candle: Candle) -> bool:
    """Has the market left this region behind — two heights past it, or closed through it?

    Both his: *"atinge 120 paramos de procurar"* on the wick, and the close at 85 under [90, 100]
    on his own example — the close and not the wick, confirmed 2026-09-09. A close exactly on the
    far edge is neither, the same reading his rule 4 on the averages gives a close exactly on the
    line.
    """
    height = block.top - block.bottom
    clearance = height * GIVE_UP_AT_REGION_HEIGHTS
    if block.kind is ZoneKind.DEMAND:
        return candle.high >= block.top + clearance or candle.close < block.bottom
    return candle.low <= block.bottom - clearance or candle.close > block.top


__all__ = [
    "GIVE_UP_AT_REGION_HEIGHTS",
    "MAX_SERVER_OFFSET",
    "BarAggregator",
    "HigherTimeframeGate",
    "RegionChoice",
    "RegionTracker",
    "Release",
]
