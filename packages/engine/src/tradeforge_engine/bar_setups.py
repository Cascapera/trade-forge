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
"""

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

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


__all__ = [
    "DEFAULT_BODY_FRACTION",
    "DEFAULT_ENTRY_FRACTION",
    "DEFAULT_HAMMER_BREAK_TICKS",
    "DEFAULT_SHADOW_FRACTION",
    "DEFAULT_STOP_FRACTION",
    "HammerBreakLevels",
    "HammerBreakTrigger",
    "HammerForceLevels",
    "HammerForceTrigger",
    "is_force_bar",
    "is_hammer",
]


def _body(candle: Candle) -> Money:
    return abs(candle.close - candle.open)


def _range(candle: Candle) -> Money:
    return candle.high - candle.low


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

    def levels_for(
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
        here, and it is the open question this trigger hands upstairs.
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
            return HammerForceLevels(
                side=side,
                limit_price=to_tick(
                    hammer.high + self.entry_fraction * spread, tick, ROUND_CEILING
                ),
                stop_loss=stop_loss,
                annul_close=force.high,
            )
        spread = hammer.low - force.low
        if spread <= ZERO:
            return None
        return HammerForceLevels(
            side=side,
            limit_price=to_tick(hammer.low - self.entry_fraction * spread, tick, ROUND_FLOOR),
            stop_loss=stop_loss,
            annul_close=force.low,
        )
