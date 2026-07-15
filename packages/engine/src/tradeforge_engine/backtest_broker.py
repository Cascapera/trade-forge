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

Phase 1 holds one position at a time. Stops come from the strategy (a level fixed on the
decision bar, so it cannot see the future); targets are a risk multiple, computed here at the
fill, because the multiple is measured from an entry price that does not exist until the fill.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal

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
        self.submitted: list[OrderRequest] = []

        # The open position's protective levels, or None when flat or holding an unstopped one.
        self._protection: _Protection | None = None

    # ----------------------------------------------------------------------- #
    # Broker protocol                                                          #
    # ----------------------------------------------------------------------- #

    def submit(self, order: OrderRequest) -> OrderResult:
        self._pending.append(order)
        self.submitted.append(order)
        return OrderResult(order=order, accepted=True)

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        fills: list[Fill] = []

        # 1. A position carried in from an earlier bar: its stop/target is live from this
        #    bar's first tick. Checked before anything fills at the open, because a gap
        #    through the stop is the worst case and the worst case goes first.
        carried = self._check_protective(candle)
        if carried is not None:
            fills.append(carried)

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

        # 4. Value the account at the close (obligation of the protocol: the loop reads equity
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

    def _check_protective(self, candle: Candle) -> Fill | None:
        """Has this bar's range touched the open position's stop or target? If both, the stop
        wins — the worst case, because the data to prove otherwise does not exist."""
        position = self._portfolio.position
        protection = self._protection
        if position is None or protection is None:
            return None

        price, reason = self._protective_price(position.side, candle, protection)
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
        self, side: Side, candle: Candle, protection: _Protection
    ) -> tuple[Money | None, str]:
        """The exit price and reason if a protective level is touched, else `(None, "")`.

        A gap through the level fills at the open (worse than the level) rather than at the
        level itself — the "stop always fills at the stop" fantasy the PR-103 range guard
        exists to refuse. `min`/`max` against the open expresses exactly that, and the result
        is provably inside `[low, high]`, so the guard never fires on an honest fill.
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
