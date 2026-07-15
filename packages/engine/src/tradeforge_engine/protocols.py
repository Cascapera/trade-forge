"""The five seams (sdd.md §3.3.2).

Every one of these is a `Protocol`, not a base class, and the difference is the whole
design. With inheritance, `BacktestBroker` and `MT5Broker` would share an ancestor — and
the moment they do, the tempting shortcut appears: put a little logic in the base, and now
the engine's behaviour depends on which subclass it got. Structural typing gives the engine
an interface and nothing to inherit. It cannot reach into an implementation because there
is no implementation to reach into.

That is what ADR-04 buys in practice: the engine never learns where an order is executed.
The same strategy, the same loop, the same P&L arithmetic run against a simulated fill in a
backtest and a real one at a broker — the one property that stops "backtest result" and
"live result" from describing two different systems.

Which means these signatures have to be honest about **live**, not just convenient for the
backtest. `positions()` takes a symbol because a real MT5 account holds positions the
strategy never opened — another EA's, a manual trade, another symbol entirely.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from tradeforge_engine.domain import (
    AccountState,
    Candle,
    ClosedTrade,
    Context,
    EvalContext,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Signal,
    Volume,
)


@runtime_checkable
class Broker(Protocol):
    """Executes orders. Knows nothing about strategies.

    The split between `submit` and `on_bar` is the anti-lookahead rule in the type system:
    submitting an order does **not** execute it. `submit` queues; `on_bar` is the only place
    a `Fill` can ever be born, and it runs at the top of the *next* candle.

    Three obligations that the types cannot state, and that the engine checks or depends on:

    1. **`on_bar` must leave `account()` marked to market at the candle it was handed.**
       The loop reads equity straight after, once per bar. A broker that forgets to mark
       produces an equity curve that is really a balance curve — a flat line through a 50%
       drawdown, and a maximum drawdown of zero on a strategy that halved the account.
    2. **Fills returned from one `on_bar` are ordered exits before entries.** A reversal on
       a single bar closes before it opens; the other order makes the ledger refuse a second
       position, correctly, on a sequence that was merely mis-sorted.
    3. **A protective exit inherits the `decided_at` of the entry that placed it.** A stop
       hit intrabar was decided when the stop was set, not when price reached it — and that
       is what lets a stop trigger on the very bar the position opened without tripping the
       engine's lookahead guard.
    """

    def submit(self, order: OrderRequest) -> OrderResult:
        """Queue an order. Returns whether it was accepted, not whether it executed."""
        ...

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        """A new bar has arrived. Fill what is pending, check stops and targets, and mark
        the account to market at this candle's close.
        """
        ...

    def positions(self, symbol: str) -> Sequence[Position]:
        """What is open **in this symbol**, according to whoever is holding the risk.

        The argument is not decoration. A live MT5 account reports every position it holds:
        another expert advisor's, a manual trade, an entirely different instrument. A
        broker interface that returns "the positions" and lets the caller take the first is
        an interface that will one day close five lots of gold because a strategy trading
        EURUSD asked to exit.
        """
        ...

    def account(self) -> AccountState:
        """Balance and equity, from the same authority. Marked to market by `on_bar`."""
        ...

    def trades(self) -> Sequence[ClosedTrade]:
        """The round trips **this run** has closed. Not the account's history.

        The distinction is the same one `positions(symbol)` makes, and it matters for the
        same reason: a live MT5 account's deal history holds another expert advisor's trades,
        another symbol's, and everything from before this session started. Return those and
        the reconciliation property — `sum(net_pnl) == final equity - initial capital` — is
        false in live for reasons that have nothing to do with the strategy. An `MT5Broker`
        filters by its own magic number.

        The broker holds the ledger, so the broker reports the trades: the same authority
        that reports the account. PR-106's metrics are computed from these.
        """
        ...


@runtime_checkable
class Strategy(Protocol):
    """Turns a closed candle into intent.

    Returns `Signal`s, not orders: how big the position should be is not the strategy's
    business (see `Signal`). In PR-104 the DSL compiles into an implementation of this.
    """

    def on_bar(self, context: Context) -> Sequence[Signal]:
        """Decide. `context` holds exactly one candle — the one that just closed."""
        ...


@runtime_checkable
class Indicator(Protocol):
    """Incremental state, O(1) per candle.

    Not "recompute a 200-bar window on every bar": a backtest over a decade of M1 data is
    five million bars, and an O(n) update per bar makes the whole thing O(n²). The
    incremental form is also the only one that can run live, where there is no window to
    recompute — just the next candle.

    `value()` returns `None` until enough candles have been seen. That is not a nuisance; it
    is the warm-up period, and a strategy that trades on bar 3 of a 20-period average is
    trading on a number that does not exist yet.
    """

    def update(self, candle: Candle) -> None:
        """Fold one **closed** candle into the state."""
        ...

    def value(self) -> Money | None:
        """The current value, or `None` while still warming up."""
        ...


@runtime_checkable
class Condition(Protocol):
    """One node of the strategy's expression tree, evaluated once per closed candle.

    Takes an `EvalContext`, not the loop's `Context` (sdd.md §3.3.2). A condition can name
    an indicator and reach a candle N bars back, and neither of those lives in the
    single-candle view the loop hands the strategy. Same anti-lookahead discipline, wider
    vocabulary — and the wider vocabulary is precisely why it needs its own context.
    """

    def evaluate(self, context: EvalContext) -> bool: ...


@runtime_checkable
class CostModel(Protocol):
    """What a trade costs. Plugged in, never hard-coded (ADR-07).

    Forex pays a spread and a swap; a stock pays a commission; an index future pays both. An
    `if asset_class == ...` inside the engine would spread that difference across every fill
    site in the codebase. A cost model is one object, and adding a new asset class is a new
    implementation of this protocol — not an edit to the core.
    """

    def entry_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:
        """What it costs to open. A magnitude, never negative — the domain refuses one."""
        ...

    def exit_cost(self, order: OrderRequest, instrument: InstrumentSpec, price: Money) -> Money:
        """What it costs to close."""
        ...


@runtime_checkable
class RiskManager(Protocol):
    """How big, and whether at all.

    Two questions, deliberately separate. `size` is arithmetic: given this stop, this account
    and this instrument, how many lots put exactly 1% at risk. `allow` is a veto: the daily
    loss limit is hit, the kill switch is on, there are already three positions open. Sizing
    must not be where the kill switch lives, because a sizing bug would then be a safety bug.
    """

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Volume:
        """Lots (or shares). Zero means "do not take this trade"."""
        ...

    def allow(self, order: OrderRequest, account: AccountState) -> bool:
        """The veto. False means the order never reaches the broker."""
        ...
