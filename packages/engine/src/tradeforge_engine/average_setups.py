"""His four bar patterns, hosted by a moving average instead of a region.

Dictated 2026-09-07, right after the gift merged: *"os padrões de martelo, barra de força, gift e
barra ignorada podem ser usados em outros setups não smart money, na média de 9 no 9.1 — desde que
nenhuma barra feche abaixo da MM9 ainda é válido — e no ponto contínuo também"*. It is the reason
he gave a day earlier for taking the hammer's stop off the region: *"poderá ser usado em qualquer
estratégia"*. The arithmetic in `bar_setups.py` already knows nothing about regions; what this
module adds is the **clock** an average keeps, which is not the region's clock.

**What the average changes, in his answers**, against the region of chapter 11:

* *What starts the count.* The region: the touch on its near edge. The average: the bar that
  **touches the average**.
* *The pattern's first bar.* The region: inside a window of five (hammer), or any touching bar
  (gift). The average: the touching bar **or the next**.
* *A ceiling on the entry.* The region: half a region (hammer), two regions (gift). The average:
  **none**.
* *What kills everything.* The region: its far edge, and every failed criterion. The average: **a
  bar closing across it**, and nothing else.
* *After the kill.* The region is spent. The average needs a **new turn** — a close back on the
  setup's side — before a touch counts again.
* *The order's life.* Two bars on both.
* *Conduction.* The region: the structural trail. The average: **the host's** — the 9.1 conducts
  on the MME9, the ponto contínuo structurally.

So a pattern that fails here — a bar after the hammer that is not a bar of force, a bar after the
force bar that is neither gift nor ignored bar, an order that lived its two bars unfilled — does
**not** end anything: the turn stands, and the next touch starts over. That is the largest
difference from the region, where every one of those spends the zone, and it follows from his
fourth answer: *"só desarma se fechar abaixo da média"*.

**The host owns the turn.** `PatternWatch` never reads a close against the average: whether this
bar is on the setup's side, whether the turn is spent by a fill, whether a trade is open, are the
host strategy's questions, and it answers them the way it always has. The watch is only fed bars
that are on the setup's side, and `reset` is how the host tells it a turn ended. Kept that way so
the ponto contínuo — which adds two corrections before the touch — can host the same watch without
the watch learning what a correction is.
"""

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum

from tradeforge_engine.bar_setups import (
    DEFAULT_BODY_FRACTION,
    DEFAULT_VOLUME_FRACTION,
    ForceFollowLevels,
    GiftStop,
    GiftTrigger,
    HammerBreakLevels,
    HammerBreakTrigger,
    HammerForceLevels,
    HammerForceTrigger,
    IgnoredBarTrigger,
    is_force_bar,
    is_hammer,
)
from tradeforge_engine.domain import Candle, Money, Side
from tradeforge_engine.errors import EngineError

logger = logging.getLogger(__name__)

DEFAULT_BARS_AFTER_TOUCH = 1
"""How many bars after the touching one may still open the pattern: *"a barra que encosta ou a
seguinte"*. Zero would be the touching bar alone; one is his rule, confirmed for the hammer on
2026-09-06 and for the gift and the ignored bar on 2026-09-07 (*"2 - confere"*)."""

DEFAULT_BARS_TO_FILL = 2
"""How long the armed order lives, unfilled — his fifth answer: *"duas barras"*, the same as the
region's."""


class AverageEntryPoint(StrEnum):
    """How a setup hosted by an average enters, once the average has qualified the bar.

    `CLASSIC` is what the 9.1 has always done: the bar that closed across the average is the
    reference, and the order rests at its high with the stop at its low. The other four are his
    bar patterns, and by his first answer they **replace** that entry rather than joining it —
    *"substitui: a entrada tem que seguir o gatilho escolhido"*. So a document naming `martelo`
    never arms the classic breakout, and one naming `classic` never looks for a hammer.

    The four pattern values are spelled the same as `ZoneEntryPoint`'s on purpose — a user who
    learned `martelo_forca` on a region should not learn a second word for it on the average —
    but they are a different enum, because the region's own three (`edge`, `midpoint`,
    `return_pass`) have no meaning here and `classic` has none there.
    """

    CLASSIC = "classic"
    MARTELO = "martelo"
    MARTELO_FORCA = "martelo_forca"
    GIFT = "gift"
    BARRA_IGNORADA = "barra_ignorada"


@dataclass(frozen=True, slots=True)
class PatternOrder:
    """The order a pattern wants resting: a stop or a limit, never both, and its protective stop.

    The same shape `setups.ZoneEntry` has, and deliberately not that class: this module must not
    import the structure machine — the author was explicit that the MME9 and the structure setups
    *"não têm nada a ver"*. Reusing `ZoneEntry` is one import away and would drag the region's
    vocabulary into a module about a moving average, so
    `tests/test_architecture.py::test_the_swing_family_never_imports_the_structure_machine`
    guards it. ⚠️ That guard was written *because* this docstring claimed it existed before it
    did — the lesson's review of this PR caught the claim. A docstring naming a test is an
    assertion about the suite, and it is checkable."""

    side: Side
    stop_loss: Money
    limit_price: Money | None = None
    stop_price: Money | None = None

    def __post_init__(self) -> None:
        if (self.limit_price is None) == (self.stop_price is None):
            raise ValueError("a pattern order waits at a limit or a stop, not both and not neither")

    @property
    def price(self) -> Money:
        """Whichever of the two the order rests at — for logging and for the reference price."""
        price = self.limit_price if self.limit_price is not None else self.stop_price
        assert price is not None  # noqa: S101 - the invariant `__post_init__` just checked
        return price


_Levels = HammerBreakLevels | HammerForceLevels | ForceFollowLevels


@dataclass(slots=True)
class PatternWatch:
    """The clock a moving average keeps for one of his four bar patterns.

    Fed one bar at a time by `observe`, **only the bars the host judged eligible**, and answering
    with the order that should be resting after this bar, or `None` when nothing should. The host
    compares the answer with what it has on the book: a new order replaces a different one, the
    same order is left alone, `None` withdraws.

    ⚠️ **"Eligible" is the host's word and it is not the same word twice.** The MME9 feeds bars
    that closed on the setup's side, because there the side of the average *is* the signal. The
    Ponto Contínuo feeds every bar that did not close **against** it — including a bar closing
    exactly *on* the mean, which by its rule 4 is neither trigger nor cancel. This docstring said
    "closed on the setup's side" until the lesson's review of PR-203 measured the second host
    against it: a sentence that describes one caller is a sentence the other caller silently
    contradicts.

    The machine has three stages and the pattern decides how many it visits:

    1. **hunting** — a touch opens a window of the touching bar and the next; inside it the
       pattern's first bar is looked for: a hammer (both hammer variations), or a bar of force
       (gift, ignored bar). `MARTELO` arms straight from here.
    2. **waiting for the second bar** — `MARTELO_FORCA` needs the bar of force right after the
       hammer; `GIFT` and `BARRA_IGNORADA` need the follower right after the force bar. One bar,
       one chance: a second bar that is not the one wanted clears the wait, and the same bar is
       then read as a possible *first* bar of a fresh pattern, because nothing here is spent.
    3. **resting** — the order's own clock, two bars, and the pattern's own annulment: the
       hammer's low, the force bar's low, or — for `MARTELO_FORCA` — a close beyond the force bar's
       high on the bar after it. All three are `bar_setups` rules, read here exactly as the region
       activations read them.

    ⚠️ **No ceiling, and nothing is spent.** Both are his: *"sem teto, só desarma se fechar
    abaixo da média"*. A hammer a hundred points above the average arms; a lapsed order is simply
    gone, and the next touch starts again. The only thing that ends a turn is the host calling
    `reset`, on the bar that closed across.

    That a *failed* pattern spends nothing was put to him as its own question, with the case of a
    force bar whose follower is neither gift nor ignored bar, and confirmed: the turn stands and
    the next touch starts over. On a region the same failure retires the zone — two hosts, opposite
    answers, and each is his.
    """

    entry_point: AverageEntryPoint
    side: Side
    bars_after_touch: int = DEFAULT_BARS_AFTER_TOUCH
    bars_to_fill: int = DEFAULT_BARS_TO_FILL
    gift_stop: GiftStop = GiftStop.GIFT
    volume_filter: bool = False
    body_fraction: Decimal = DEFAULT_BODY_FRACTION

    _bars_since_touch: int | None = field(default=None, init=False, repr=False)
    _first: Candle | None = field(default=None, init=False, repr=False)
    _levels: _Levels | None = field(default=None, init=False, repr=False)
    _bars_since_order: int = field(default=0, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.entry_point is AverageEntryPoint.CLASSIC:
            raise EngineError("the classic breakout is the host's own entry; it keeps no watch")
        if self.bars_after_touch < 0:
            raise EngineError(f"bars after the touch is a count, got {self.bars_after_touch}")
        if self.bars_to_fill < 1:
            raise EngineError(f"the order lives at least one bar, got {self.bars_to_fill}")

    # ------------------------------------------------------------------ the host's two verbs

    def reset(self) -> None:
        """The turn ended — a bar closed across the average. Everything goes: the touch, a first
        bar waiting for its second, a resting order. *"Depois de fechar abaixo, precisa de virada
        nova"* — and the host is the one who knows when the new turn begins.

        ⚠️ **Idempotent, and the host leans on that.** It is called on every bar that is not on
        the setup's side, on the crossing bar, and on every bar spent beside an open trade — most
        of those on a watch that is already empty. Cheap by construction (four fields), and the
        alternative is the host reasoning about which resets are redundant, which is the kind of
        reasoning that stops being true when a fifth call site appears."""
        self._bars_since_touch = None
        self._first = None
        self._levels = None
        self._bars_since_order = 0

    def observe(self, candle: Candle, average: Money, *, tick: Money) -> PatternOrder | None:
        """Fold in one bar on the setup's side, and say what should be resting after it.

        ⚠️ **The bar that kills a resting order is not re-read as a pattern's first bar, and the
        bar that merely fails as a second one is.** The asymmetry is deliberate and it is his,
        confirmed 2026-09-07 against the worked case: a hammer arms on N, N+1 reaches the hammer's
        low and cancels it, and that same N+1 is itself a hammer touching the average — it arms
        nothing, and a hammer on N+2 does. Asked which, he answered *"n+2 arma"*.

        Read as an oversight, the fix is one deleted `return` — which is exactly why it is written
        down. A bar that took out the level the last pattern was protected at is not the bar he
        starts the next one from.
        """
        self._count_the_touch(candle, average)

        if self._levels is not None:
            self._age_the_order(candle)
            return self._resting()

        if self._first is not None:
            first, self._first = self._first, None
            levels = self._second_bar(first, candle, tick)
            if levels is not None:
                self._arm(levels, candle)
                return self._resting()
            logger.debug("the bar at %s did not carry the pattern forward", candle.time)
            # Not spent: this bar may itself open a new pattern, if the window admits it.

        if self._window_open():
            self._first_bar(candle, tick)
        return self._resting()

    # ------------------------------------------------------------------ the three stages

    def _count_the_touch(self, candle: Candle, average: Money) -> None:
        """A touch is bar zero of the window; every bar after it counts up. Piercing counts, and
        so does resting exactly on the average — the ponto contínuo's own reading of a touch."""
        touched = candle.low <= average if self.side is Side.LONG else candle.high >= average
        if touched:
            self._bars_since_touch = 0
        elif self._bars_since_touch is not None:
            self._bars_since_touch += 1

    def _window_open(self) -> bool:
        return self._bars_since_touch is not None and (
            self._bars_since_touch <= self.bars_after_touch
        )

    def _first_bar(self, candle: Candle, tick: Money) -> None:
        """Is this bar the pattern's first? The hammer arms at once; everything else waits."""
        if self.entry_point is AverageEntryPoint.MARTELO:
            levels = HammerBreakTrigger().levels_for(candle, side=self.side, tick=tick)
            if levels is not None:
                self._arm(levels, candle)
            return
        if self.entry_point is AverageEntryPoint.MARTELO_FORCA:
            if is_hammer(candle, side=self.side):
                self._first = candle
                logger.debug(
                    "hammer at %s on the average; waiting for the bar of force", candle.time
                )
            return
        if is_force_bar(candle, side=self.side, body_fraction=self.body_fraction):
            self._first = candle
            logger.debug("force bar at %s on the average; judging the next bar", candle.time)

    def _second_bar(self, first: Candle, candle: Candle, tick: Money) -> _Levels | None:
        """The bar after the first: the bar of force behind a hammer, or the follower behind a
        bar of force. `None` is every way it fails to be that, and none of them spends anything."""
        if self.entry_point is AverageEntryPoint.MARTELO_FORCA:
            return HammerForceTrigger().levels_for(first, candle, side=self.side, tick=tick)
        volume_fraction = DEFAULT_VOLUME_FRACTION if self.volume_filter else None
        if self.entry_point is AverageEntryPoint.GIFT:
            return GiftTrigger(stop_at=self.gift_stop, volume_fraction=volume_fraction).levels_for(
                first, candle, side=self.side, tick=tick
            )
        return IgnoredBarTrigger(volume_fraction=volume_fraction).levels_for(
            first, candle, side=self.side, tick=tick
        )

    def _arm(self, levels: _Levels, candle: Candle) -> None:
        self._levels = levels
        self._bars_since_order = 0
        logger.debug("%s armed on the bar at %s", self.entry_point.value, candle.time)

    def _age_the_order(self, candle: Candle) -> None:
        """One bar of the resting order's life: the pattern's own annulment first, then the clock.

        Three annulments, each the pattern's own rule carried over unchanged from the region:
        the hammer's low **reached** (`HammerBreakLevels.annul_price`), the force bar's low
        reached (`ForceFollowLevels.annul_price`), and for the pullback variation a bar **closing**
        beyond the force bar's high on the bar right after it (`HammerForceLevels.annul_close`) —
        a touch, a touch and a close, and the difference is the same one `bar_setups` documents.
        """
        levels = self._levels
        if levels is None:  # pragma: no cover - only called with a live order
            return
        self._bars_since_order += 1
        if self._annulled(levels, candle):
            self._levels = None
            logger.debug("the pattern's own level annulled the order at %s", candle.time)
            return
        if self._bars_since_order >= self.bars_to_fill:
            self._levels = None
            logger.debug("the order lived its %d bars unfilled", self.bars_to_fill)

    def _annulled(self, levels: _Levels, candle: Candle) -> bool:
        if isinstance(levels, HammerForceLevels):
            # ⚠️ Only the first bar after the force bar, his argument in full: by the second the
            # limit has filled or the clock takes it anyway. At `bars_to_fill == 2` the two
            # readings agree; the dial is what makes `== 1` the rule rather than a shortcut.
            if self._bars_since_order != 1:
                return False
            return (
                candle.close > levels.annul_close
                if levels.side is Side.LONG
                else candle.close < levels.annul_close
            )
        return (
            candle.low <= levels.annul_price
            if levels.side is Side.LONG
            else candle.high >= levels.annul_price
        )

    def _resting(self) -> PatternOrder | None:
        levels = self._levels
        if levels is None:
            return None
        if isinstance(levels, HammerForceLevels):
            return PatternOrder(
                side=levels.side, stop_loss=levels.stop_loss, limit_price=levels.limit_price
            )
        return PatternOrder(
            side=levels.side, stop_loss=levels.stop_loss, stop_price=levels.stop_price
        )


__all__ = [
    "DEFAULT_BARS_AFTER_TOUCH",
    "DEFAULT_BARS_TO_FILL",
    "AverageEntryPoint",
    "PatternOrder",
    "PatternWatch",
]
