"""His hammer, and the two ways he enters on one. Chapter 11.1.

**A bar pattern, not a zone activation, and the difference is his own decision.** He dictated the
first variation with the stop on the region's lower edge, then changed it mid-sentence — *"vamos
facilitar o stop pq ele poderá ser usado em qualquer estratégia"* — to twenty percent of the
hammer's own height. That one change is what puts this in a file of its own: while the stop came
from the region, a hammer could not exist without one. Taken from the bar, it depends on nothing,
and the same arithmetic serves a demand region, the MME9 and the ponto contínuo instead of being
written three times.

So this module knows about **candles and nothing else**. It does not know what a region is, how
long a setup may wait for a hammer, or what cancels one — those are clocks, and a clock belongs to
whatever holds the setup across bars. The same split let the botinha be checked against his
arithmetic before anything owned a clock (`vwap_setups.py`), and it is the reason the swing
setups will be able to reuse this without importing a single zone concept.

**The two variations are two triggers, by his instruction** — *"vamos tratar cada variação como um
gatilho separado"* — not one trigger with a flag. They disagree about the most basic thing an
order can disagree about:

* `HammerBreakTrigger` — *superação do martelo*. A **stop** order one tick past the hammer's
  extreme: price has to **break through**.
* `HammerForceTrigger` — the hammer followed by a *barra de força*. A **limit** order 30% of the
  way between the two highs: price has to **come back**.

Read as one setup with a dial, the second one's cancel rule — a bar closing beyond the force
bar's high — is incomprehensible. Read as a pullback entry, it is the only thing that could
cancel it: the market left without you.

**The gift and the ignored bar, dictated 2026-09-07, are the force bar without the hammer.** A
*barra de força* comes off a region — a hammer before it is welcome and not required — and the
bar **after** it decides everything. If that bar is small and sits in the force bar's upper third
it is a *gift*; if it is a real bar that nevertheless fails to take the force bar's low it is a
*barra ignorada*, the market ignoring the seller. Either way the entry is a **stop** one tick
past the higher of the two highs, placed when the follower closes, and the difference between
the two is one line of geometry and one line of stop placement:

* `GiftTrigger` — the follower is at most a third of the force bar and sits entirely in its
  upper third. The stop is the gift's low **or** the force bar's: his two alternatives, and the
  user's dial.
* `IgnoredBarTrigger` — the follower carries a body over a third of the force bar and did not
  break its low. The stop is always the force bar's low.

Twenty percent of the chosen bar's own height past its low, the same proportion the hammer uses.
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal
from enum import StrEnum

from tradeforge_engine.domain import ZERO, Candle, Money, Side, to_tick
from tradeforge_engine.errors import EngineError

DEFAULT_SHADOW_FRACTION = Decimal("0.50")
"""How much of the hammer must be the shadow on the far side. His words: *"a sombra inferior é
maior do que 50% do tamanho total do candle"*.

⚠️ **Strictly greater, and the denominator is the whole candle.** Both halves carry weight. Read
as "at least half" a bar split exactly down the middle would qualify, and he said *maior*. And
because the total is `high - low` rather than the shadow plus the body, a bar with a long tail on
**both** sides is not a hammer — the upper shadow eats the budget the lower one needs."""

DEFAULT_BODY_FRACTION = Decimal("0.70")
"""How much of a *barra de força* must be body: *"na barra de força ele tem que ser
predominantemente corpo, 70% da barra é corpo"*. At least, not more than — a marubozu with no
shadows at all is the purest case of what he is describing, and a rule of "greater than" would
throw it out."""

DEFAULT_STOP_FRACTION = Decimal("0.20")
"""How far past the hammer the protective stop goes, as a fraction of the hammer's own height:
*"o stop tem que ser 20% abaixo do martelo em relação ao tamanho dele, se o martelo tem 10 pontos
... stop 2 ticks abaixo"*.

⚠️ **Proportional, so a small hammer produces a small stop and therefore a large position** —
`PercentRiskManager` divides the risk budget by the distance. It is the same shape as the
botinha's narrow band, and he has already ruled on it: *"podemos limitar o lote futuramente mas
para o backtest vai ser assim mesmo"*. A floor here would be a method rule he did not give."""

DEFAULT_ENTRY_FRACTION = Decimal("0.30")
"""Where the limit rests between the two highs, for the force-bar variation: *"a entrada ocorre
entre a máxima do martelo e a máxima da barra de força ... 30% então só spread entre as
máximas"*. Hammer high 90 and force bar high 100 put the order at **93**, which is his number."""

DEFAULT_HAMMER_BREAK_TICKS = 1
"""How far past the hammer's extreme the breakout order waits: *"a ordem vai na máxima dele 1
tick"*."""

DEFAULT_GIFT_RANGE_DIVISOR = 3
"""The gift is at most a third of the force bar, high to low: *"o range de máxima e mínima deste
candle tem que ter no máximo 1/3 do tamanho da barra anterior"*.

⚠️ **A divisor, not a fraction, because a third does not exist as a `Decimal`.** `Decimal(1) /
Decimal(3)` is `0.3333…` cut at twenty-eight places, a hair *under* a third, so a gift measuring
exactly one third of its force bar — which he allows, *no máximo* — would fail the comparison on
any grid where the two ranges are both whole ticks. Multiplying the gift by three and comparing
against the force bar asks the same question with no division in it."""

DEFAULT_GIFT_UPPER_DIVISOR = 3
"""Which part of the force bar the gift must sit in: the top third, entirely. *"Ele tem que
ocorrer no terço superior da barra de força, nunca no meio ou na mínima"* — and asked whether
that means the whole gift or its close, he answered *"gift inteiro"*, so it is the gift's **low**
that is held to the line. Its high is free to reach above the force bar's; when it does, the
entry sits above it, which is what *"a máxima mais alta entre as duas"* says."""

DEFAULT_IGNORED_BODY_DIVISOR = 3
"""The ignored bar's body is *more* than a third of the force bar's height: *"ao invés do gift a
barra após a barra de força tem corpo maior que 1/3 da barra de força"*. Strictly more — at
exactly a third it is neither this nor a gift, and the region is spent. Measured against the
force bar's **range**, by his answer: *"range da força"*."""

DEFAULT_VOLUME_FRACTION = Decimal("0.70")
"""When the volume filter is on, the follower may carry at most seventy percent of the force
bar's volume: *"o gift, quando levando em consideração o volume da barra de força, tem que ter no
máximo 70% do volume da barra de força, idem para a barra ignorada"*."""


__all__ = [
    "DEFAULT_BODY_FRACTION",
    "DEFAULT_ENTRY_FRACTION",
    "DEFAULT_GIFT_RANGE_DIVISOR",
    "DEFAULT_GIFT_UPPER_DIVISOR",
    "DEFAULT_HAMMER_BREAK_TICKS",
    "DEFAULT_IGNORED_BODY_DIVISOR",
    "DEFAULT_SHADOW_FRACTION",
    "DEFAULT_STOP_FRACTION",
    "DEFAULT_VOLUME_FRACTION",
    "ForceFollowLevels",
    "GiftStop",
    "GiftTrigger",
    "HammerBreakLevels",
    "HammerBreakTrigger",
    "HammerForceLevels",
    "HammerForceTrigger",
    "IgnoredBarTrigger",
    "is_force_bar",
    "is_gift",
    "is_hammer",
    "is_ignored_bar",
    "volume_of",
]


def _body(candle: Candle) -> Money:
    return abs(candle.close - candle.open)


def _range(candle: Candle) -> Money:
    return candle.high - candle.low


def volume_of(candle: Candle) -> int:
    """The bar's volume as the filter reads it: the exchange's real volume where the venue
    reports one, ticks where it does not — the same `auto` rule the VWAP runs on, so a strategy
    carrying both reads one number for volume rather than two.

    ⚠️ On this project's own data that is ticks everywhere but the AAPL, where `real_volume` is
    present. A bar that reports neither returns zero, and the filter treats zero as *unmeasured*
    rather than as *quiet* — see `_volume_passes`."""
    return candle.real_volume or candle.tick_volume


def is_hammer(
    candle: Candle, *, side: Side, shadow_fraction: Decimal = DEFAULT_SHADOW_FRACTION
) -> bool:
    """Is this bar a hammer in `side`'s favour?

        long:   (open - low)  > fraction * (high - low)   and  close > open
        short:  (high - open) > fraction * (high - low)   and  close < open

    ⚠️ **The body must exist and must point the right way**, even if it is a few ticks: *"ele deve
    possuir um corpo mesmo que pequeno de alguns ticks de alta"*. A doji with a long tail is not a
    hammer here. That is stricter than most textbook definitions, and it is his — the body is what
    says the side that was defending actually closed in front.

    Written as a multiplication rather than a ratio so there is no division to guard: a bar with
    no range cannot reach this comparison anyway, because `Candle` refuses a high below its own
    body, so `high == low` forces `open == close` and the body test has already failed.
    """
    span = _range(candle)
    # ⚠️ **The shadow is measured from the `open`, and that is not a shortcut.** The body test
    # runs first and short-circuits, so by the time the shadow is measured the direction is
    # settled: a long bar's `open` **is** `min(open, close)`, a short bar's **is** the max.
    # Written with the min/max the two are provably the same number, which means the mutant
    # swapping one for the other can never be killed — dead surface reading as care.
    if side is Side.LONG:
        return candle.close > candle.open and (candle.open - candle.low > shadow_fraction * span)
    return candle.close < candle.open and (candle.high - candle.open > shadow_fraction * span)


def is_force_bar(
    candle: Candle, *, side: Side, body_fraction: Decimal = DEFAULT_BODY_FRACTION
) -> bool:
    """Is this bar a *barra de força* in `side`'s favour — predominantly body, pointing that way?

        long:   (close - open) >= fraction * (high - low)   and  close > open
        short:  (open - close) >= fraction * (high - low)   and  close < open

    ⚠️ **`≥`, against `is_hammer`'s `>`, and the asymmetry is deliberate.** A hammer is defined by
    a shadow *exceeding* half the bar (his *maior*); a force bar is defined by a body *reaching*
    seventy percent (his *é*). Written the same way, one of the two would stop matching a bar he
    would trade. The boundary case is reachable on any coarse grid and is pinned in the tests.
    """
    if side is Side.LONG and candle.close <= candle.open:
        return False
    if side is Side.SHORT and candle.close >= candle.open:
        return False
    return _body(candle) >= body_fraction * _range(candle)


def _stop_loss(hammer: Candle, *, side: Side, fraction: Decimal, tick: Money) -> Money:
    """Twenty percent of the hammer's height past its extreme, on the instrument's grid.

    Rounded **away** from the entry — floor for a long, ceiling for a short — which is the rule
    every level in this engine follows: a stop no nearer than the rule said, an entry no better.
    """
    reach = fraction * _range(hammer)
    if side is Side.LONG:
        return to_tick(hammer.low - reach, tick, ROUND_FLOOR)
    return to_tick(hammer.high + reach, tick, ROUND_CEILING)


@dataclass(frozen=True, slots=True)
class HammerBreakLevels:
    """The three prices the *superação* gives, and they are three rather than two.

    ⚠️ **`annul_price` and `stop_loss` are neighbours and must not be merged** — the same trap
    `FffdLevels` documents, arriving from a different setup. His rule has both: the stop sits
    twenty percent of the hammer below its low, and *losing the low itself* cancels the trigger
    without a trade — *"se não consumir a ordem e perder a mínima do martelo vamos cancelar o
    gatilho"*. On a ten-point hammer those are two points apart; on a small one they are a tick
    apart, and collapsing them would turn every cancelled setup into a losing trade in the ledger.

    They also fire in different worlds. The cancel only exists while the order is **resting**:
    once price has filled at the break, the low being lost is a trade walking toward its stop.
    """

    side: Side
    stop_price: Money
    """The entry: one tick past the hammer's extreme, on the side the move has to resume."""

    stop_loss: Money
    """Twenty percent of the hammer's height beyond the level that annuls, never on it."""

    annul_price: Money
    """The hammer's own extreme. **Reaching** it ends the setup rather than the trade."""

    @property
    def risk(self) -> Money:
        """The distance sizing measures, off the two levels as placed rather than as computed —
        they are snapped to the grid in opposite directions, so the two are different numbers."""
        return abs(self.stop_price - self.stop_loss)


@dataclass(frozen=True, slots=True)
class HammerBreakTrigger:
    """Variation 1 — *superação do martelo*. A stop order past the hammer.

    **Stateless on purpose.** Whether *this* bar is a hammer is a question about this bar. How
    long the order then lives, how many bars a region may wait for one, and what a region's edges
    have to say about it are state and policy, and they belong to whatever holds the setup — see
    the module docstring.
    """

    break_ticks: int = DEFAULT_HAMMER_BREAK_TICKS
    stop_fraction: Decimal = DEFAULT_STOP_FRACTION
    shadow_fraction: Decimal = DEFAULT_SHADOW_FRACTION

    def __post_init__(self) -> None:
        if self.break_ticks < 1:
            raise EngineError(
                f"the order waits at least one tick past the hammer, got {self.break_ticks}"
            )
        if self.stop_fraction <= ZERO:
            raise EngineError(f"the stop sits past the hammer, got {self.stop_fraction}")
        if not (ZERO < self.shadow_fraction < 1):
            raise EngineError(f"the shadow is a fraction of the bar, got {self.shadow_fraction}")

    def levels_for(self, candle: Candle, *, side: Side, tick: Money) -> HammerBreakLevels | None:
        """The three levels if this bar is a hammer, or `None` if it is not."""
        if not is_hammer(candle, side=side, shadow_fraction=self.shadow_fraction):
            return None

        offset = self.break_ticks * tick
        stop_loss = _stop_loss(candle, side=side, fraction=self.stop_fraction, tick=tick)
        if side is Side.LONG:
            return HammerBreakLevels(
                side=side,
                stop_price=to_tick(candle.high + offset, tick, ROUND_CEILING),
                stop_loss=stop_loss,
                annul_price=candle.low,
            )
        return HammerBreakLevels(
            side=side,
            stop_price=to_tick(candle.low - offset, tick, ROUND_FLOOR),
            stop_loss=stop_loss,
            annul_price=candle.high,
        )


@dataclass(frozen=True, slots=True)
class HammerForceLevels:
    """The three prices the *barra de força* variation gives.

    ⚠️ **`annul_close` is tested against a close, not against a touch**, which is what makes it a
    different kind of level from `HammerBreakLevels.annul_price` even though both are called
    annulment. His rule: *"se na barra seguinte da barra de força ocorrer de a barra fechar acima
    da máxima da barra de força a entrada é anulada"*. Price trading through it and coming back is
    not the event; the bar has to **finish** beyond it.

    That reads as arbitrary until you notice the entry is a **limit below the market**. The order
    is waiting for a pullback, and a bar that closes beyond the force bar's high is the market
    saying the pullback is not coming.
    """

    side: Side
    limit_price: Money
    """The entry: 30% of the way from the hammer's extreme towards the force bar's."""

    stop_loss: Money
    """Twenty percent of the **hammer's** height past the hammer — not the force bar's. The force
    bar chooses where to get in; the hammer is still what the trade is measured against."""

    annul_close: Money
    """The force bar's extreme. A bar **closing** beyond it withdraws the order."""

    @property
    def risk(self) -> Money:
        """The distance sizing measures, off the levels as placed. See `HammerBreakLevels`."""
        return abs(self.limit_price - self.stop_loss)


@dataclass(frozen=True, slots=True)
class HammerForceTrigger:
    """Variation 2 — the hammer, then a *barra de força*, and the entry on the pullback between
    their two extremes.

    His arithmetic, to check this against: hammer high **90**, force bar high **100**, order at
    **93**. Thirty percent of the spread between the highs, measured from the hammer's.
    """

    entry_fraction: Decimal = DEFAULT_ENTRY_FRACTION
    stop_fraction: Decimal = DEFAULT_STOP_FRACTION
    shadow_fraction: Decimal = DEFAULT_SHADOW_FRACTION
    body_fraction: Decimal = DEFAULT_BODY_FRACTION

    def __post_init__(self) -> None:
        if not (ZERO < self.entry_fraction < 1):
            raise EngineError(
                f"the entry rests between the two extremes, got {self.entry_fraction}"
            )
        if self.stop_fraction <= ZERO:
            raise EngineError(f"the stop sits past the hammer, got {self.stop_fraction}")

    def levels_for(  # noqa: PLR0911 - one flat refusal per way the pair fails to be his setup
        self, hammer: Candle, force: Candle, *, side: Side, tick: Money
    ) -> HammerForceLevels | None:
        """The three levels if `hammer` is a hammer and `force` is a force bar behind it.

        ⚠️ **`None` when the force bar's extreme does not clear the hammer's, and that is his
        rule rather than a conservative default of ours.** Asked directly, 2026-09-06: *"barra de
        força que não supera o martelo cancela entrada"*. It was written here first as a refusal
        we had chosen — a hammer may carry an upper shadow of almost half its height, so a
        perfectly valid force bar can top out *below* the hammer's high, and then "between the two
        highs" names an empty interval where thirty percent would put a buy limit **below** the
        hammer. Same behaviour either way; the difference is who may change it, and a docstring
        crediting the wrong author is an invitation to tune a method rule.

        ⚠️ It says **cancel**, not "not yet". There is no second candidate anyway — the force bar
        is the bar immediately after the hammer — so what the word decides is whether the region
        may go on offering *another* hammer inside the same window. That belongs to the clock, not
        here, and the two clocks answer it differently: on a region `HammerForceActivation` spends
        the zone (*"a região deixa de valer"*), on an average `PatternWatch` spends nothing and the
        next touch starts over (*"só desarma se fechar abaixo da média"*). Both are his.

        ⚠️ **And `None` again when the limit would not sit below the force bar's close** --
        for a long; above it for a short. A force bar owes only seventy percent of itself to its
        body, so the other thirty can be a wick through the hammer's high with a close back under
        the thirty-percent level: the most ordinary bar there is, a strong candle spiking
        resistance and finishing on it. The arithmetic then puts a **buy limit above the market**,
        which is not a pullback entry at all, and `Signal` refuses it as the sign error it usually
        is -- taking the whole backtest down with a `ValueError`, because nothing in the loop
        catches one.

        Measured on the bar that found it: hammer high 102, force bar 99.20/102.10/99.00/102.00
        at ninety percent body, limit 102.03 against a close of 102.00.

        Refusing is the same ending the two refusals above give, and it is the conservative one:
        his rule describes an entry *between* two extremes that price comes back to, and a level
        the market has already closed below is not that trade. **Put to him with the three
        alternatives -- cancel, a stop on the break of the force bar's high, or at market on its
        close -- and answered on 2026-09-07: "cancela".** So the refusal is his rule now, not our
        conservative default, and it is not a knob to loosen.
        """
        if not is_hammer(hammer, side=side, shadow_fraction=self.shadow_fraction):
            return None
        if not is_force_bar(force, side=side, body_fraction=self.body_fraction):
            return None

        stop_loss = _stop_loss(hammer, side=side, fraction=self.stop_fraction, tick=tick)
        if side is Side.LONG:
            spread = force.high - hammer.high
            if spread <= ZERO:
                return None
            limit = to_tick(hammer.high + self.entry_fraction * spread, tick, ROUND_CEILING)
            if limit > force.close:
                return None
            return HammerForceLevels(
                side=side,
                limit_price=limit,
                stop_loss=stop_loss,
                annul_close=force.high,
            )
        spread = hammer.low - force.low
        if spread <= ZERO:
            return None
        limit = to_tick(hammer.low - self.entry_fraction * spread, tick, ROUND_FLOOR)
        if limit < force.close:
            return None
        return HammerForceLevels(
            side=side,
            limit_price=limit,
            stop_loss=stop_loss,
            annul_close=force.low,
        )


# --------------------------------------------------------------------------- #
# The gift and the ignored bar — the bar after a bar of force                  #
# --------------------------------------------------------------------------- #


def is_gift(
    force: Candle,
    follower: Candle,
    *,
    side: Side,
    range_divisor: int = DEFAULT_GIFT_RANGE_DIVISOR,
    upper_divisor: int = DEFAULT_GIFT_UPPER_DIVISOR,
) -> bool:
    """Is `follower` a gift on the back of `force`? Small, and in the force bar's upper third.

        long:   (high - low)             * 3 <= force.high - force.low      -- small
                (force.high - low)       * 3 <= force.high - force.low      -- entirely up top
        short:  the same with the extremes exchanged

    ⚠️ **Both `<=`.** *No máximo um terço* admits a gift exactly a third tall, and a gift whose
    low sits exactly on the line a third down from the force bar's high is in the upper third,
    not below it. Written with multiplication so the boundary is reachable — see
    `DEFAULT_GIFT_RANGE_DIVISOR`.

    The gift's colour is not a condition. He gave two numbers — its size and where it sits —
    and nothing about which way it closed; a small bar is small whichever way its body points.
    """
    span = _range(force)
    if _range(follower) * range_divisor > span:
        return False
    if side is Side.LONG:
        return (force.high - follower.low) * upper_divisor <= span
    return (follower.high - force.low) * upper_divisor <= span


def is_ignored_bar(
    force: Candle,
    follower: Candle,
    *,
    side: Side,
    body_divisor: int = DEFAULT_IGNORED_BODY_DIVISOR,
) -> bool:
    """Is `follower` a *barra ignorada* on the back of `force`? A real body that failed to take
    the force bar's low.

        long:   |close - open| * 3 > force.high - force.low    and   low  >= force.low
        short:  |close - open| * 3 > force.high - force.low    and   high <= force.high

    ⚠️ **Strictly more than a third, against the gift's at-most-a-third**, so no bar is both.
    Between the two there is a gap — a bar with a body over a third whose *range* is under a
    third cannot exist, but a bar with a small body and a big range can, and it is neither: not
    a gift by size, not an ignored bar by body. That bar cancels the entry, which is his rule for
    anything that is not one of the two.

    ⚠️ **The low is held to `>=`, not `>`.** *"Não pode romper a mínima"* — sitting on it is not
    breaking it, the same reading `_RegionWatch.broke` gives the region's own edge.

    Colour is not a condition here either, and that is his answer rather than our reading of a
    silence: asked directly on 2026-09-07, *"sem cor"*. The word *ignorada* pictures a seller's bar
    the market shrugs off, but the rule is a body over a third that kept the low, and a bar closing
    up satisfies it.
    """
    if _body(follower) * body_divisor <= _range(force):
        return False
    if side is Side.LONG:
        return follower.low >= force.low
    return follower.high <= force.high


def _volume_passes(force: Candle, follower: Candle, fraction: Decimal | None) -> bool:
    """The optional volume filter: the follower at most `fraction` of the force bar's volume.

    ⚠️ **A force bar with no volume at all fails the filter rather than passing it.** With the
    filter on, zero against zero would read as "seventy percent of nothing is nothing, and the
    follower has nothing, so it passes" — a filter that never rejects on a feed that never
    reports volume. The project has met that silence before: a `volume="real"` VWAP outside the
    AAPL skipped every bar and nobody heard. Failing closed turns a missing feed into a setup that
    never arms, which is visible in a census, instead of a filter that is on and doing nothing.
    """
    if fraction is None:
        return True
    reference = volume_of(force)
    if reference == 0:
        return False
    return Decimal(volume_of(follower)) <= fraction * reference


class GiftStop(StrEnum):
    """Where the gift's protective stop is taken from — his two alternatives, the user's choice.

    * `GIFT` — twenty percent of the gift's own height below the gift's low. The tighter of the
      two by construction, since a gift is at most a third of the force bar: same risk, larger
      size, and a stop that a normal retest of the force bar's body would take out.
    * `FORCA` — twenty percent of the force bar's height below the force bar's low. The wider
      one, and the only one the ignored bar has.

    The default is the gift's own low, the alternative he named first. It is a method choice
    rather than an engineering one, and it is on the document so a study can hold the rest of
    the setup still and vary only this.
    """

    GIFT = "gift"
    FORCA = "forca"


@dataclass(frozen=True, slots=True)
class ForceFollowLevels:
    """The three prices a gift or an ignored bar gives.

    The third was not in the first dictation. Asked what takes the resting order back before its
    two bars run out, he answered (2026-09-07): *"se o preço perder a mínima da barra de força
    anula"*. So the force bar's low is the annulment for both followers — the gift's own low is
    never a level, whichever bar the stop is measured from.

    ⚠️ **`annul_price` and `stop_loss` are neighbours and must not be merged**, the same trap
    `HammerBreakLevels` documents. With the stop off the force bar the two are twenty percent of
    its height apart; with the stop off the gift the annulment sits *below* the stop, so a resting
    order can outlive its own stop level being traded through and still be cancelled at the force
    bar's low. Neither is a losing trade in the ledger, and collapsing them would make one.
    """

    side: Side
    stop_price: Money
    """The entry: one tick past the higher of the force bar's high and the follower's, on the
    side the move has to resume."""

    stop_loss: Money
    """Twenty percent of the chosen bar's height past its low — the gift's or the force bar's."""

    annul_price: Money
    """The force bar's own extreme. **Reaching** it while the order rests ends the setup rather
    than the trade — read the way the hammer reads its own low, and only while resting: once
    filled, the low being lost is a trade walking toward its stop."""

    @property
    def risk(self) -> Money:
        """The distance sizing measures, off the two levels as placed. See `HammerBreakLevels`."""
        return abs(self.stop_price - self.stop_loss)


def _entry_past(
    force: Candle, follower: Candle, *, side: Side, tick: Money, break_ticks: int
) -> Money:
    """The entry: `break_ticks` past the higher high (lower low) of the pair, on the grid,
    rounded away from the market the way every entry in this engine is."""
    offset = break_ticks * tick
    if side is Side.LONG:
        return to_tick(max(force.high, follower.high) + offset, tick, ROUND_CEILING)
    return to_tick(min(force.low, follower.low) - offset, tick, ROUND_FLOOR)


def _check_follow_dials(
    *, volume_fraction: Decimal | None, break_ticks: int, stop_fraction: Decimal
) -> None:
    if volume_fraction is not None and volume_fraction <= ZERO:
        raise EngineError(f"the volume ceiling is a positive fraction, got {volume_fraction}")
    if break_ticks < 1:
        raise EngineError(f"the order waits at least one tick past the high, got {break_ticks}")
    if stop_fraction <= ZERO:
        raise EngineError(f"the stop sits past the bar, got {stop_fraction}")


@dataclass(frozen=True, slots=True)
class GiftTrigger:
    """A bar of force, then a gift, and a stop order past the higher of their two highs.

    His numbers, to check this against: force bar 100/106/99.50/105.50, gift
    105.20/105.60/104.40/105.00 — the order at **106.01** on a penny grid, the stop at **104.16**
    off the gift (20% of its 1.20 range below 104.40) or **98.20** off the force bar (20% of its
    6.50 below 99.50). Read off the trigger before being written here, not worked out on paper.

    **Stateless, like the hammer triggers.** Whether these two bars are a force bar and a gift is
    a question about the two bars. How the force bar had to relate to a region, how many bars
    the order then lives, and what cancels it are the clock's — `ForceFollowActivation`.
    """

    stop_at: GiftStop = GiftStop.GIFT
    volume_fraction: Decimal | None = None
    """`None` is the filter off. A number is the ceiling on the gift's volume as a fraction of
    the force bar's — see `_volume_passes`."""
    range_divisor: int = DEFAULT_GIFT_RANGE_DIVISOR
    upper_divisor: int = DEFAULT_GIFT_UPPER_DIVISOR
    break_ticks: int = DEFAULT_HAMMER_BREAK_TICKS
    stop_fraction: Decimal = DEFAULT_STOP_FRACTION
    body_fraction: Decimal = DEFAULT_BODY_FRACTION

    def __post_init__(self) -> None:
        if self.range_divisor < 1 or self.upper_divisor < 1:
            raise EngineError("the gift is measured in whole divisions of the force bar")
        _check_follow_dials(
            volume_fraction=self.volume_fraction,
            break_ticks=self.break_ticks,
            stop_fraction=self.stop_fraction,
        )

    def levels_for(
        self, force: Candle, follower: Candle, *, side: Side, tick: Money
    ) -> ForceFollowLevels | None:
        """The two levels if `force` is a force bar and `follower` a gift on it, else `None`.

        `None` is one answer for three refusals — not a force bar, not a gift, a gift too loud
        for the filter — and the clock treats them all the same way, by his rule: *"se alguns
        destes critérios não ocorrer cancela a entrada e espera uma nova oportunidade"*.
        """
        if not is_force_bar(force, side=side, body_fraction=self.body_fraction):
            return None
        if not is_gift(
            force,
            follower,
            side=side,
            range_divisor=self.range_divisor,
            upper_divisor=self.upper_divisor,
        ):
            return None
        if not _volume_passes(force, follower, self.volume_fraction):
            return None
        stop_bar = follower if self.stop_at is GiftStop.GIFT else force
        return ForceFollowLevels(
            side=side,
            stop_price=_entry_past(
                force, follower, side=side, tick=tick, break_ticks=self.break_ticks
            ),
            stop_loss=_stop_loss(stop_bar, side=side, fraction=self.stop_fraction, tick=tick),
            annul_price=force.low if side is Side.LONG else force.high,
        )


@dataclass(frozen=True, slots=True)
class IgnoredBarTrigger:
    """A bar of force, then a *barra ignorada*, and a stop order past the higher of the two
    highs — the gift's entry with a different follower and no choice of stop.

    His numbers: the same force bar 100/106/99.50/105.50, then 105.50/105.80/102.00/102.50 — a
    three-point body against a 6.50 force bar, low 102 above 99.50. The order at **106.01**, the
    stop at **98.20** off the force bar, which is the only stop this trigger has.
    """

    volume_fraction: Decimal | None = None
    body_divisor: int = DEFAULT_IGNORED_BODY_DIVISOR
    break_ticks: int = DEFAULT_HAMMER_BREAK_TICKS
    stop_fraction: Decimal = DEFAULT_STOP_FRACTION
    body_fraction: Decimal = DEFAULT_BODY_FRACTION

    def __post_init__(self) -> None:
        if self.body_divisor < 1:
            raise EngineError("the ignored bar is measured in whole divisions of the force bar")
        _check_follow_dials(
            volume_fraction=self.volume_fraction,
            break_ticks=self.break_ticks,
            stop_fraction=self.stop_fraction,
        )

    def levels_for(
        self, force: Candle, follower: Candle, *, side: Side, tick: Money
    ) -> ForceFollowLevels | None:
        """The two levels if `force` is a force bar and `follower` an ignored bar on it."""
        if not is_force_bar(force, side=side, body_fraction=self.body_fraction):
            return None
        if not is_ignored_bar(force, follower, side=side, body_divisor=self.body_divisor):
            return None
        if not _volume_passes(force, follower, self.volume_fraction):
            return None
        return ForceFollowLevels(
            side=side,
            stop_price=_entry_past(
                force, follower, side=side, tick=tick, break_ticks=self.break_ticks
            ),
            stop_loss=_stop_loss(force, side=side, fraction=self.stop_fraction, tick=tick),
            annul_price=force.low if side is Side.LONG else force.high,
        )
