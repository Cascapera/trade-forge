"""The backtest broker: where intent becomes a realistic fill, and every line kills a bias.

A `Strategy` decides. This turns each decision into a fill the way a real venue would — and
the value of a backtest is entirely in how honestly it does that. Read each step as the bias
it refuses:

* **Fill at the next bar's open, never this bar's close.** The strategy decided on the close
  of N; it executes at the open of N+1, a price it had not seen. Filling on the close is the
  single most common way a backtest reports profit that never existed. The engine's loop
  guard (PR-103) already refuses it; this broker is built so it never even tries.
* **Slippage against you, clamped to the bar.** A market order fills a little worse than the
  quote — `slippage_ticks` adverse. But never *worse than the bar traded*: you cannot be
  filled above the high or below the low, because nobody paid that price. The clamp is what
  keeps slippage honest and inside the loop's range guard.
* **Costs on both legs.** Spread or commission leaves the account on entry and again on exit,
  through the plugged-in `CostModel`. Gross P&L is not what the account saw.
* **Worst case when a bar is ambiguous.** If one candle's range covers *both* the stop and
  the target, the tick data to say which came first does not exist — so the backtest assumes
  the **stop** filled first. The optimistic assumption is how a strategy "discovers" an edge
  the market never gave it.
* **A resting order waits for a price, and only on a later bar.** An order carrying a
  `limit_price` (a pullback, ADR-0014) or a `stop_price` (a breakout, ADR-0016) rests here
  until a candle's range reaches it — the structure setups enter at the edge of a region and
  the swing setups on the break of a level, and both are a price, not an instant. It is the
  one fill in this engine priced *inside* a bar, which makes it the one place the anti-lookahead
  rule can be broken quietly; `_fill_resting` restates the rule where it can actually be violated.

Phase 1 holds one position at a time. Stops come from the strategy (a level fixed on the
decision bar, so it cannot see the future); targets are a risk multiple, computed here at the
fill, because the multiple is measured from an entry price that does not exist until the fill.
"""

import datetime as dt
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from tradeforge_engine.costs import NoCostModel
from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    Candle,
    ClosedTrade,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Side,
    SignalKind,
)
from tradeforge_engine.portfolio import Portfolio
from tradeforge_engine.protocols import CostModel

logger = logging.getLogger(__name__)


def _survives_the_gap(order: OrderRequest, price: Money) -> bool:
    """Would this fill open a position that is already past its own stop?

    Only a gap can do it: the market opens through the level *and* through the stop below it,
    so the "better price" a limit promises hands you an entry on the wrong side of your own
    exit. A long filled at 1.09000 with its stop at 1.09300 is a position whose stop is
    *above* it — the protective check closes it on the same bar for roughly the spread, and
    a trade log fills up with scratches that look like the strategy traded and lost small.

    It is a real market event, not a modelling artefact: the zone the order was waiting at was
    blown through overnight. What the strategy wanted at that level no longer exists.
    """
    stop = order.stop_loss
    if stop is None:
        return True
    return price > stop if order.side is Side.LONG else price < stop


_MAX_ENTRY_SLIP = Decimal("0.5")
"""How much of its own risk a stop entry may spend before it stops being the trade (ADR-0016).

Half. A buy stop triggered at 1.10500 with its stop at 1.10000 was sized for 500 points of
risk; filled at 1.10750 it now risks 750 — one and a half times the money the risk manager
agreed to. Past that the position is not the one the strategy priced, and booking it charges
the account for a trade nobody sized.

The number is a judgement, not a derivation, which is why it sits here alone with its name on
it instead of inline in a comparison: change it and every breakout backtest changes with it.
"""


def _survives_the_slip(order: OrderRequest, price: Money) -> bool:
    """Did this stop entry fill close enough to its trigger to still be the sized trade?

    The mirror of `_survives_the_gap`, for the mirror order — and it exists because that guard
    is provably a no-op for stops. A limit fills *toward* its own stop, so a gap can carry it
    past the exit and `_survives_the_gap` catches it. A stop fills *away* from its stop, so it
    can never cross it, and nothing bounded how far the wrong way it could go:

    * **A gap through the trigger.** The bar opens far above a buy stop and the fill is that
      open (ADR-0016 says a stop fills at "this price or worse"). Sized for 1R at the trigger,
      the position can open holding 10R of risk — one overnight gap in an index, and a 1%-risk
      account books a 10% loss the risk manager never agreed to.
    * **Eligibility deferred, no gap at all.** A resting order whose bar was taken by another
      position (step 4 of `on_bar`) waits; by the time the slot frees, price can be far past
      the trigger, and `max(open, level)` hands it that price.

    Both are the same event told two ways: the level the strategy chose is behind the market
    now. `_survives_the_gap` drops such an order rather than leaving it resting, and so does
    this — for the same reason, and at the same place in the fill path.

    An order with no stop was never sized against a distance, so there is nothing to measure
    it against and it passes.
    """
    stop = order.stop_loss
    trigger = order.stop_price
    if stop is None or trigger is None:
        return True
    risk = abs(trigger - stop)
    if risk <= ZERO:
        return True
    return abs(price - trigger) <= _MAX_ENTRY_SLIP * risk


@dataclass(frozen=True, slots=True)
class _Protection:
    """The open position's protective levels, armed at the fill and cleared at the exit.

    The three travel together — a stop with no decision instant could not build a lookahead-
    safe exit — so they live in one object that is either wholly present or wholly `None`,
    which is also what saves the exit path from asserting the decision instant back into
    existence. `target` alone is optional: a stop without a take-profit is a valid position.
    """

    stop: Money
    decided_at: dt.datetime
    target: Money | None


@dataclass(frozen=True, slots=True)
class _Resting:
    """A pending order waiting for the market to reach its level, with level and name pulled out.

    A **limit** waits for price to come *back* to it; a **stop** waits for price to break
    *through* it (ADR-0016). `is_stop` is which — it decides both the crossing test and, on a gap,
    whether the fill is the better price or the worse one.

    Level and name are stored here instead of being read back off the order for a typing reason
    that is really a testing reason: `submit` has already proved the level is not `None`, so
    keeping it narrow avoids a second check downstream — a branch no test could take, in the fill
    path.
    """

    order: OrderRequest
    level: Money
    is_stop: bool
    name: str


class BacktestBroker:
    """Simulated execution over a candle stream. Satisfies the `Broker` protocol structurally.

    It holds its own `Portfolio` — the ledger is the broker's, so `account()` and `trades()`
    answer from the same authority a live `MT5Broker` would. Protective levels (the stop, the
    computed target, and the entry's `decided_at`) are tracked here alongside the open
    position, because a target is not known until the entry fills and a protective exit must
    inherit the entry's decision instant to clear the lookahead guard.
    """

    def __init__(  # noqa: PLR0913 — keyword-only; each names one axis of a simulated venue
        self,
        *,
        instrument: InstrumentSpec,
        initial_capital: Money = Decimal(10_000),
        cost_model: CostModel | None = None,
        slippage_ticks: Decimal = ZERO,
        take_profit_rr: Decimal | None = None,
        currency: str = "USD",
    ) -> None:
        if slippage_ticks < ZERO:
            raise ValueError(f"slippage is a magnitude, got {slippage_ticks}")
        if take_profit_rr is not None and take_profit_rr <= ZERO:
            raise ValueError(f"take-profit R multiple must be positive, got {take_profit_rr}")

        self._instrument = instrument
        self._portfolio = Portfolio(
            initial_capital=initial_capital, instrument=instrument, currency=currency
        )
        self._cost_model: CostModel = cost_model if cost_model is not None else NoCostModel()
        # Kept in ticks, converted to a price at fill time. `slippage_ticks * tick_size` in
        # `__init__` would multiply outside the pinned decimal context (only `run()` installs
        # it), and while that product is exact for a power-of-ten tick it is the one arithmetic
        # that would otherwise escape `ENGINE_CONTEXT` — a tick of 0.003 exists (loop.py:57).
        self._slippage_ticks = slippage_ticks
        self._rr = take_profit_rr

        self._pending: list[OrderRequest] = []
        # Limit orders waiting for the market, in submission order — which is what makes the
        # tie deterministic when two of them are reachable on the same bar. Arrival order is
        # arbitrary as a rule (the nearest level would be defensible too) but it is a fact the
        # broker already has, and the entry layer above arms one order at a time anyway.
        self._resting: list[_Resting] = []
        # Names that have already filled. See `_reject_resting`.
        self._consumed: set[str] = set()
        self.submitted: list[OrderRequest] = []

        # The open position's protective levels, or None when flat or holding an unstopped one.
        self._protection: _Protection | None = None

    # ----------------------------------------------------------------------- #
    # Broker protocol                                                          #
    # ----------------------------------------------------------------------- #

    def submit(self, order: OrderRequest) -> OrderResult:
        # A resting order carries a level — a limit or a stop, never both (the domain proved it).
        # Neither means "at market", filled at the next open.
        level = order.limit_price if order.limit_price is not None else order.stop_price
        if level is None:
            self._pending.append(order)
            self.submitted.append(order)
            return OrderResult(order=order, accepted=True)

        rejection = self._reject_resting(order)
        if rejection is not None:
            return OrderResult(order=order, accepted=False, reason=rejection)

        # `_reject_resting` has just proved the name is there; narrowing it again would add a
        # branch no test could enter, in the middle of the path that accepts orders.
        self._resting.append(
            _Resting(
                order=order,
                level=level,
                is_stop=order.stop_price is not None,
                name=cast(str, order.client_id),
            )
        )
        self.submitted.append(order)
        return OrderResult(order=order, accepted=True)

    def _reject_resting(self, order: OrderRequest) -> str | None:
        """Why this resting order cannot rest, or `None` if it can.

        Both refusals are about a promise the broker would otherwise be unable to keep:

        * **An exit does not rest.** A resting exit would be a second take-profit, sitting
          beside the one `_check_protective` already fills — two paths closing one position,
          and the ledger only survives whichever ran first. Targets belong to `take_profit_rr`.
        * **A resting order needs a name, and a unique one.** `cancel` takes a `client_id`;
          two orders answering to the same one make "withdraw that order" a question with two
          answers, and picking either would be the broker guessing.
        """
        if order.intent is not SignalKind.ENTRY:
            return "only an entry can rest at a level; a target is the broker's protective exit"
        if order.client_id is None:
            return "a resting order needs a client_id: nothing else can cancel it later"
        if any(resting.name == order.client_id for resting in self._resting):
            return f"client_id {order.client_id!r} is already resting"
        if order.client_id in self._consumed:
            # A name is spent once it has filled. A strategy that re-emits its zone's signal
            # every bar would otherwise place a *second* order under the same name while the
            # first one's position is still open — invisible, unreachable by `cancel`, and
            # able to fill much later off a zone that stopped existing.
            return f"client_id {order.client_id!r} has already filled"
        return None

    def cancel(self, client_id: str) -> bool:
        before = len(self._resting)
        self._resting = [resting for resting in self._resting if resting.name != client_id]
        return len(self._resting) < before

    def resting(self) -> Sequence[OrderRequest]:
        """The orders still waiting, in arrival order. Not part of the `Broker` protocol.

        A backtest that ends with orders pending is not wrong, but it is worth being able to
        see: the strategy owns their lifetime, so a cancel it forgot to send shows up as
        trades that never happened — a silence, which is the hardest kind of bug to notice.
        """
        return tuple(resting.order for resting in self._resting)

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        fills: list[Fill] = []

        # 1. A position carried in from an earlier bar: its stop/target is live from this
        #    bar's first tick. Checked before anything fills at the open, because a gap
        #    through the stop is the worst case and the worst case goes first.
        # Did a position occupy this bar *past its first tick*? A limit order fills somewhere
        # inside the bar, so what it needs to know is not "was there a position" but "was the
        # market free after the open". A trade that ended at the open ended on the first tick
        # and left the rest of the bar demonstrably flat; one that ended at its stop, inside
        # the bar, did not — and the tick order that would separate them does not exist.
        #
        # The test is the same in every place it is asked — `exit price != open` — and it is
        # asked of protective exits only. A strategy's own exit fills at the open by
        # construction (give or take slippage, which is a tick or two either side of it), so
        # it never held the market past the first tick and never sets this.
        held_past_open = False

        carried = self._check_protective(candle)
        if carried is not None:
            fills.append(carried)
            held_past_open = carried.price != candle.open

        # 2. Fill what was decided last bar, at this bar's open. Exits before entries: a
        #    reversal must close before it opens (the ledger refuses a second position).
        pending = sorted(self._pending, key=lambda order: order.intent is SignalKind.ENTRY)
        self._pending.clear()
        for order in pending:
            filled = self._fill_at_open(order, candle)
            if filled is None:
                continue
            fills.append(filled)
            # 3. A stop hit inside the very bar the position opened is legitimate — the stop
            #    was set when the entry was decided, so its exit inherits that decision instant
            #    and clears the lookahead guard.
            if order.intent is SignalKind.ENTRY:
                same_bar = self._check_protective(candle)
                if same_bar is not None:
                    fills.append(same_bar)
                    # The same test as step 1, and it earns its keep here too: usually this
                    # position ran from the open down to a stop inside the bar, but if the
                    # market gapped *through* that stop overnight the entry and the exit both
                    # land on the open, and the trade was over on the first tick. Hard-coding
                    # `True` would block a bar nothing was ambiguous about.
                    held_past_open = held_past_open or same_bar.price != candle.open

        # 4. Limit orders resting from an earlier bar: the market came to the price, somewhere
        #    inside this bar. After the open fills, because the open is this bar's first tick —
        #    a limit cannot execute before it, and on a bar that gaps past the level the two
        #    would otherwise tie at the same price with no data to separate them.
        #
        #    **Not on a bar another position held past the open.** A bar that stopped a trade
        #    out at 1.09000 also traded 1.09500 on the way down, and letting a buy limit at
        #    1.09500 fill off that range books an entry at a price that only existed while
        #    another position was open. No tick data settles it, so the ambiguous bar resolves
        #    the way every ambiguous bar here resolves: against the trade. A position closed
        #    *at the open* is a different case and is not blocked — the rest of that bar was
        #    flat, with nothing to be ambiguous about.
        if not held_past_open and self._portfolio.position is None:
            resting = self._fill_resting(candle)
            if resting is not None:
                fills.append(resting)
                # Born *inside* the bar, its protective levels are read under different rules —
                # see `_newborn_protective_price`. But a fill *at the open* was not born inside
                # anything: the bar gapped through the level, the order executed on the first
                # tick, and the whole bar belongs to the position. Same test as steps 1 and 3,
                # for the same reason; hard-coding `True` would deny a position the target its
                # bar demonstrably reached.
                same_bar = self._check_protective(
                    candle, born_this_bar=resting.price != candle.open
                )
                if same_bar is not None:
                    fills.append(same_bar)

        # 5. Value the account at the close (obligation of the protocol: the loop reads equity
        #    straight after this, once per bar).
        self._portfolio.mark_to_market(candle)

        fills.sort(key=lambda fill: fill.order.intent is SignalKind.ENTRY)
        return fills

    def positions(self, symbol: str) -> Sequence[Position]:
        position = self._portfolio.position
        return (position,) if position is not None and position.symbol == symbol else ()

    def account(self) -> AccountState:
        return self._portfolio.account()

    def trades(self) -> Sequence[ClosedTrade]:
        return self._portfolio.trades

    # ----------------------------------------------------------------------- #
    # Fills                                                                    #
    # ----------------------------------------------------------------------- #

    def _fill_at_open(self, order: OrderRequest, candle: Candle) -> Fill | None:
        if order.intent is SignalKind.EXIT:
            return self._fill_exit_at_open(order, candle)
        return self._fill_entry_at_open(order, candle)

    def _fill_entry_at_open(self, order: OrderRequest, candle: Candle) -> Fill | None:
        if self._portfolio.position is not None:
            # A pending entry with a position already open should not happen — the strategy
            # only enters while flat. Refuse rather than let the ledger raise mid-bar.
            return None
        price = self._entry_price(order.side, candle)
        cost = self._cost_model.entry_cost(order, self._instrument, price)
        fill = Fill(order=order, time=candle.time, price=price, volume=order.volume, costs=cost)
        self._portfolio.apply(fill)
        self._arm_protection(order, price)
        return fill

    def _fill_exit_at_open(self, order: OrderRequest, candle: Candle) -> Fill | None:
        position = self._portfolio.position
        if position is None:
            # Already stopped out this bar (step 1), so the strategy's exit has nothing to do.
            return None
        price = self._exit_price(position.side, candle)
        cost = self._cost_model.exit_cost(order, self._instrument, price)
        fill = Fill(order=order, time=candle.time, price=price, volume=position.volume, costs=cost)
        self._portfolio.apply(fill)
        self._disarm_protection()
        return fill

    def _fill_resting(self, candle: Candle) -> Fill | None:
        """Fill the first resting order this bar reached, or `None`. ADR-0014.

        **The anti-lookahead rule, restated where it can actually be broken.** Every other
        fill in this broker lands on an open, and "the next open" is a bar the strategy had
        not seen by construction. A limit fills at a price *inside* a bar, from an order that
        outlives the bar that placed it — so the guard has to be a comparison, not a shape:
        `candle.time > decided_at`, or the order sits this bar out. Drop that line and an
        order placed on the close of N fills inside N at a level the strategy was looking at
        when it decided, which is the whole bias this engine exists to refuse.

        **At most one fill per bar.** Phase 1 holds one position, so a second fill could not
        be booked anyway. A reachable order that does not fill **keeps waiting** — a venue
        would not withdraw it, and the only side that knows the order stopped making sense is
        the strategy that named it. The one order that does *not* survive is one the market
        gapped past its own stop: see `_survives_the_gap`.
        """
        # Iterated over a snapshot: dead orders are removed from `self._resting` as they are
        # found, and mutating the list being walked would skip the entry after each removal.
        # `remove` matches by dataclass equality rather than identity — safe because a name is
        # unique among resting orders (`_reject_resting`), which is a distant invariant to be
        # leaning on, so it is written down here rather than left to be rediscovered.
        for resting in tuple(self._resting):
            if candle.time <= resting.order.decided_at:
                continue
            price = self._resting_price(resting, candle)
            if price is None:
                continue
            if not _survives_the_gap(resting.order, price):
                # The trade is gone, not waiting: price opened through the level *and* through
                # the stop that was measured from it, so the fill would open a position already
                # past its own exit. Booking it produces a scratch trade whose only content is
                # the spread. Dropped rather than left resting, because the level it was
                # waiting for is behind the market now — and dropped without ending the bar,
                # because this order losing its reason to exist says nothing about the next
                # one in the queue.
                self._resting.remove(resting)
                logger.debug(
                    "resting order %s dropped: %s gapped past its stop %s",
                    resting.name,
                    price,
                    resting.order.stop_loss,
                )
                continue
            if not _survives_the_slip(resting.order, price):
                # The stop's own version of the same event: the fill is so far past the trigger
                # that the position would carry risk the manager never sized (ADR-0016). Dropped
                # on the same terms as the gap above — the level is behind the market, so the
                # order is gone rather than waiting, and the bar goes on to the next in the queue.
                self._resting.remove(resting)
                logger.debug(
                    "resting order %s dropped: %s is too far past its trigger %s",
                    resting.name,
                    price,
                    resting.order.stop_price,
                )
                continue

            self._resting.remove(resting)
            order = resting.order
            self._consumed.add(resting.name)
            cost = self._cost_model.entry_cost(order, self._instrument, price)
            fill = Fill(order=order, time=candle.time, price=price, volume=order.volume, costs=cost)
            self._portfolio.apply(fill)
            self._arm_protection(order, price)
            return fill

        return None

    def _resting_price(self, resting: _Resting, candle: Candle) -> Money | None:
        """The price a resting order fills at on this bar, or `None` if the bar never reached it.

        Two mirror-image orders, split by `resting.is_stop`:

        * A **limit** rests on the side price has to come *back* to — a buy below, a sell above —
          and is a promise of "this price or better". On a bar that opens beyond it, the fill is
          the open: the market opening better than you asked hands you the better price.
        * A **stop** rests on the side price has to break *through* — a buy above, a sell below —
          and is a market order triggered at the level, "this price or worse" (ADR-0016). The same
          `min`/`max` toward the open now hands you the *worse* price on a gap, precisely because
          the stop rests on the opposite side: a bar that opens past the trigger fills at that
          open, beyond the level.

        Either way the result is provably inside `[low, high]`, so the loop's range guard never
        fires.

        **No slippage beyond the gap.** A limit never fills worse than its level; a stop never
        fills better than its trigger. The residual optimism is the queue — a one-tick wick (limit)
        or breakout (stop) is assumed to have filled you — the cost of simulating pending orders
        without tick data (ADR-0014/0016).
        """
        level = resting.level
        # Which side the order rests on, and so which way the bar must move to reach it. A limit
        # buy and a stop sell both rest *below*; a limit sell and a stop buy both rest *above*.
        if resting.is_stop:
            rests_below = resting.order.side is Side.SHORT
        else:
            rests_below = resting.order.side is Side.LONG

        if rests_below:
            if candle.low > level:
                return None
            return min(candle.open, level)
        if candle.high < level:
            return None
        return max(candle.open, level)

    def _check_protective(self, candle: Candle, *, born_this_bar: bool = False) -> Fill | None:
        """Has this bar's range touched the open position's stop or target? If both, the stop
        wins — the worst case, because the data to prove otherwise does not exist.

        `born_this_bar` is set for a position opened by a resting fill — a limit or a stop —
        that landed *inside* the bar, the one entry that does not happen at the open. A resting
        order that filled *at* the open is not one of them: the bar gapped through its level, so
        it executed on the first tick and the ordinary reading applies (see step 4 of `on_bar`).
        It changes how the levels are read, and only that. The entry *price* is deliberately not
        passed: the newborn reading never needs it (the proof is in
        `_newborn_protective_price`), and a parameter nobody reads is a standing invitation to
        start reading it.
        """
        position = self._portfolio.position
        protection = self._protection
        if position is None or protection is None:
            return None

        price, reason = self._protective_price(
            position.side, candle, protection, born_this_bar=born_this_bar
        )
        if price is None:
            return None

        exit_order = OrderRequest(
            symbol=position.symbol,
            side=position.side,
            intent=SignalKind.EXIT,
            volume=position.volume,
            decided_at=protection.decided_at,
            reason=reason,
        )
        cost = self._cost_model.exit_cost(exit_order, self._instrument, price)
        fill = Fill(
            order=exit_order, time=candle.time, price=price, volume=position.volume, costs=cost
        )
        self._portfolio.apply(fill)
        self._disarm_protection()
        return fill

    def _protective_price(
        self, side: Side, candle: Candle, protection: _Protection, *, born_this_bar: bool
    ) -> tuple[Money | None, str]:
        """The exit price and reason if a protective level is touched, else `(None, "")`.

        Two readings, because a position that existed at the open and one born in the middle
        of the bar know different things about the same candle.
        """
        if born_this_bar:
            return self._newborn_protective_price(side, candle, protection)
        return self._carried_protective_price(side, candle, protection)

    def _carried_protective_price(
        self, side: Side, candle: Candle, protection: _Protection
    ) -> tuple[Money | None, str]:
        """For a position that already existed at this bar's open — the ordinary case.

        The whole bar is fair game: every tick in it happened while the position was open, so
        touching a level is reaching it. A gap **through** a level fills at the open rather
        than at the level, in both directions and for opposite reasons. On the stop it is
        pessimism: the "stop always fills at the stop" fantasy the PR-103 range guard exists
        to refuse. On the target it is not a concession but what a limit order *is* — a
        take-profit is a sell limit, and a market that opens beyond it fills you at the better
        price, exactly as `_resting_price` does for entries. Symmetry of mechanism, not of
        optimism: both say "the open is where you actually traded".
        """
        stop = protection.stop
        target = protection.target
        if side is Side.LONG:
            if candle.low <= stop:  # SL first (worst case)
                return min(candle.open, stop), "sl"
            if target is not None and candle.high >= target:
                return max(candle.open, target), "tp"
        else:  # SHORT: stop above, target below
            if candle.high >= stop:
                return max(candle.open, stop), "sl"
            if target is not None and candle.low <= target:
                return min(candle.open, target), "tp"
        return None, ""

    def _newborn_protective_price(
        self, side: Side, candle: Candle, protection: _Protection
    ) -> tuple[Money | None, str]:
        """For a position born **inside** this bar, at a resting fill — a limit or a stop.

        Two things change, and both come from one fact: part of this bar happened before the
        position existed, and no tick data says which part. (A resting order that filled *at
        the open* is not this case at all — it owned the whole bar, and `on_bar` sends it to
        the ordinary reading.)

        **The levels fill exactly, with no gap treatment.** The open is a price from before
        the entry — it cannot be where this position exited. The fill is on the far side of
        the open from wherever the order rested, so the first touch of either level, after the
        fill, is the level itself.

        **The target must be *provable*, the stop only reachable.** The high of this bar may
        have printed before the entry existed, so "the high reached the target" proves
        nothing. The **close** does: price went from the fill to the close, so a close beyond
        the target crossed it after the entry, necessarily. The stop gets no such proof, and
        the reason differs by order type — worth spelling out, because the asymmetry is the
        engine's house rule and not a property of the geometry:

        * At a **limit** fill the stop sits on the far side of the very move that filled the
          entry, so a bar reaching it very likely reached it *after* the fill.
        * At a **stop** fill (ADR-0016) the entry came on the way *up* through the trigger, so
          the protective stop below sits behind where price came from: that low may well have
          printed before the entry existed. The bar is genuinely ambiguous — and an ambiguous
          bar resolves here the way every ambiguous bar in this engine resolves, against the
          trade. It is why a breakout can book a loss on a bar that closed above its entry;
          the alternative is an engine that decides its own doubts in its favour.

        The residue — a bar whose high tags the target but whose close falls short — is
        genuinely unknowable and the position simply carries into the next bar, where the
        ordinary reading applies again.

        Dropping the gap treatment on the stop is safe for both, but only the limit side has a
        price proof (`open >= fill` from `min(open, limit)`, plus `fill > stop` from
        `_survives_the_gap`, therefore `min(open, stop) == stop`); a stop entry has
        `open <= fill` instead, so `min(open, stop)` can differ from `stop` and it is the
        *first* argument above — the open predates the position — that rules the open out.
        Written plainly here because reintroducing `min`/`max` would quietly reprice an exit
        at a tick the position was not alive for.
        """
        stop = protection.stop
        target = protection.target
        if side is Side.LONG:
            if candle.low <= stop:  # SL first (worst case)
                return stop, "sl"
            if target is not None and candle.close >= target:
                return target, "tp"
        else:  # SHORT: stop above, target below
            if candle.high >= stop:
                return stop, "sl"
            if target is not None and candle.close <= target:
                return target, "tp"
        return None, ""

    # ----------------------------------------------------------------------- #
    # Slippage and protective arming                                          #
    # ----------------------------------------------------------------------- #

    def _slippage(self) -> Money:
        """The slippage as a price, computed under whatever context `on_bar` runs in — which is
        `run()`'s `ENGINE_CONTEXT`, the same one every other fill number is rounded in."""
        return self._slippage_ticks * self._instrument.tick_size

    def _entry_price(self, side: Side, candle: Candle) -> Money:
        """Open, plus adverse slippage, clamped to the bar. A buy fills a little higher, a
        sell a little lower — but never past the high or low, where nobody traded."""
        slippage = self._slippage()
        if side is Side.LONG:
            return min(candle.open + slippage, candle.high)
        return max(candle.open - slippage, candle.low)

    def _exit_price(self, position_side: Side, candle: Candle) -> Money:
        """Closing crosses the other way: a long exits by selling (fills lower), a short by
        buying (fills higher). Same clamp to the bar."""
        slippage = self._slippage()
        if position_side is Side.LONG:
            return max(candle.open - slippage, candle.low)
        return min(candle.open + slippage, candle.high)

    def _arm_protection(self, order: OrderRequest, entry_price: Money) -> None:
        """Arm the stop and target the moment the entry fills. No stop ⇒ no protection at all:
        an unstopped position can only be closed by a strategy condition."""
        if order.stop_loss is None:
            self._protection = None
            return
        target: Money | None = None
        if self._rr is not None:
            risk = abs(entry_price - order.stop_loss)
            sign = 1 if order.side is Side.LONG else -1
            target = entry_price + sign * self._rr * risk
        self._protection = _Protection(
            stop=order.stop_loss, decided_at=order.decided_at, target=target
        )

    def _disarm_protection(self) -> None:
        self._protection = None


__all__ = ["BacktestBroker"]
