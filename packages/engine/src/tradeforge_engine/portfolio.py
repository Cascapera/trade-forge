"""The ledger: fills in, positions and equity out.

Deliberately a *pure state machine*. It has no broker, no database, no clock — you hand it
fills and candles, and it tells you where the money is. That is what makes it checkable by
hand against a spreadsheet (PR-105's golden test), and what lets the same accounting run
behind a simulated fill and a real one.

The property that governs everything here:

    sum(trade.net_pnl for trade in trades) == final equity - initial capital

It must hold **exactly**, with costs on both legs, or every metric in PR-106 is computed
from a number the account never saw. It is asserted in `test_portfolio.py`, and again as a
property-based test over random trade sequences.

Phase 1 holds at most one open position at a time (`max_open_positions` defaults to 1 in
the DSL). This class enforces that rather than pretending to support a book it has never
been tested with.
"""

from decimal import Decimal

from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    Candle,
    ClosedTrade,
    Fill,
    InstrumentSpec,
    Money,
    Position,
    SignalKind,
)
from tradeforge_engine.errors import EngineError


class Portfolio:
    """Positions, cash, and the difference between balance and equity."""

    def __init__(
        self,
        *,
        initial_capital: Money,
        instrument: InstrumentSpec,
        currency: str = "USD",
    ) -> None:
        if initial_capital <= ZERO:
            raise ValueError(f"initial capital must be positive, got {initial_capital}")

        self._instrument = instrument
        self._initial = initial_capital
        self._currency = currency
        self._balance = initial_capital
        self._equity = initial_capital
        self._position: Position | None = None
        self._trades: list[ClosedTrade] = []

    @property
    def initial_capital(self) -> Money:
        return self._initial

    @property
    def position(self) -> Position | None:
        return self._position

    @property
    def trades(self) -> tuple[ClosedTrade, ...]:
        return tuple(self._trades)

    def account(self) -> AccountState:
        return AccountState(balance=self._balance, equity=self._equity, currency=self._currency)

    def apply(self, fill: Fill) -> ClosedTrade | None:
        """Fold one fill into the ledger. Returns the trade, if this fill closed one."""
        if fill.order.intent is SignalKind.ENTRY:
            self._open(fill)
            return None

        return self._close(fill)

    def mark_to_market(self, candle: Candle) -> None:
        """Value the open position at this candle's close.

        Equity, not balance. An account with a losing position still open has not lost the
        money yet — but it *has* drawn down, and a drawdown measured on balance reports a
        serene flat line right up to the margin call.
        """
        self._equity = self._balance + self._unrealised(candle.close)

    # ----------------------------------------------------------------------- #

    def _open(self, fill: Fill) -> None:
        if self._position is not None:
            raise EngineError(
                f"cannot open a {fill.order.side} position while one is already open "
                f"({self._position.side} {self._position.volume}); phase 1 holds one at a time"
            )

        if fill.volume != fill.order.volume:
            raise EngineError(
                f"partial fill: {fill.volume} of an order for {fill.order.volume}. Phase 1 "
                f"fills an order whole. A broker that receives partials from the venue must "
                f"aggregate them before returning a Fill"
            )

        # The entry's cost leaves the account the moment the order executes. But it is also
        # *remembered on the position*, because the trade this becomes has to report the
        # whole round trip — see ClosedTrade.
        self._balance -= fill.costs
        self._equity = self._balance

        self._position = Position(
            symbol=fill.order.symbol,
            side=fill.order.side,
            volume=fill.volume,
            entry_price=fill.price,
            entry_time=fill.time,
            entry_costs=fill.costs,
            stop_loss=fill.order.stop_loss,
            take_profit=fill.order.take_profit,
        )

    def _close(self, fill: Fill) -> ClosedTrade:
        position = self._position
        if position is None:
            raise EngineError("cannot close a position that is not open")

        # A partial close would have priced the *whole* position at this fill: ten lots of
        # profit booked on a one-lot fill, and nine lots of live exposure vanishing from the
        # ledger while they are still open at the broker. Nothing would raise; the equity
        # curve would simply be wrong by a factor of ten.
        #
        # Partial fills are normal MT5 behaviour in a thin market. Phase 1 does not support
        # them — but it says so, rather than quietly inventing money.
        if fill.volume != position.volume:
            raise EngineError(
                f"partial close: {fill.volume} of an open {position.volume}. Phase 1 closes a "
                f"position whole; a partial close is a second position this ledger cannot see"
            )

        gross = self._pnl(position, position.entry_price, fill.price)

        # The balance only moves by what has not moved yet: the entry's cost was already
        # taken at `_open`. The *trade*, however, reports both legs.
        self._balance += gross - fill.costs
        self._equity = self._balance
        self._position = None

        costs = position.entry_costs + fill.costs
        trade = ClosedTrade(
            symbol=position.symbol,
            side=position.side,
            volume=position.volume,
            entry_time=position.entry_time,
            entry_price=position.entry_price,
            exit_time=fill.time,
            exit_price=fill.price,
            gross_pnl=gross,
            costs=costs,
            net_pnl=gross - costs,
            reason=fill.order.reason,
        )
        self._trades.append(trade)
        return trade

    def _pnl(self, position: Position, entry: Money, exit_price: Money) -> Money:
        """Written once, for a long, and flipped by the side's sign.

        A separate short branch is how a codebase ends up correct in one direction and
        subtly wrong in the other — and the wrong one is the one nobody trades until the
        strategy that shorts finally gets published.
        """
        move = (exit_price - entry) * position.side.sign
        return self._instrument.money_for(move, position.volume)

    def _unrealised(self, price: Money) -> Money:
        position = self._position
        if position is None:
            return Decimal(0)
        return self._pnl(position, position.entry_price, price)
