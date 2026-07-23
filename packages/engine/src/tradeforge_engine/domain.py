"""The vocabulary of the system.

Everything the engine reasons about lives here, and it lives here rather than in the
database or the collector on purpose: **the core owns the vocabulary, and the adapters
conform to it** (ADR-0012). A `Candle` defined by the storage layer would mean the
engine's model of a market is whatever happened to be convenient to persist.

Every type here is a **frozen dataclass**. Not style: an invariant. An indicator that has
already read a candle must be reading the same candle a thousand bars later, and a
`Position` that a risk check approved must be the position that gets filled. Mutable
domain objects turn "the same input produces the same output" into a hope, because any
holder of a reference can quietly change history. `slots=True` because there are millions
of these in a decade of M1 bars.

And every type here **validates itself**. The database has CHECK constraints; the engine,
which is where the arithmetic actually happens, needs the same. A `tick_size` of zero is a
division by zero at the bottom of the P&L; a negative cost is free money in the balance; a
naive datetime is a backtest silently shifted by hours. None of those raise on their own —
they produce plausible, wrong numbers — so they are refused at construction.

Money is `Decimal`, never `float` — see ADR-0011.
"""

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

# An amount of money, or a price. Exact decimal arithmetic: an equity curve is a sum of
# thousands of these, and binary floating point drifts through exactly that path.
type Money = Decimal

# Lots, contracts or shares — whatever the instrument's contract size says a unit is.
type Volume = Decimal

ZERO = Decimal(0)


def _require_utc(moment: dt.datetime, field: str) -> None:
    """A naive datetime is not an instant, and the failure it causes is silent.

    A backtest whose candles are naive and whose fills are aware does not crash on the
    happy path — it crashes ten years into a run, on the first comparison. And a backtest
    whose candles are all naive simply trades a market displaced by the broker's timezone,
    and reports a plausible result.
    """
    if moment.tzinfo is None:
        raise ValueError(f"{field} must be timezone-aware: {moment!r} means nothing on its own")


class Side(StrEnum):
    """Which way a position faces."""

    LONG = "long"
    SHORT = "short"

    @property
    def sign(self) -> int:
        """+1 for a long, -1 for a short.

        Every P&L formula in the engine is written once, for a long, and multiplied by
        this. The alternative — an `if side is LONG` in each of them — is how a codebase
        ends up with a short-selling bug in one function and not the other.
        """
        return 1 if self is Side.LONG else -1

    @property
    def opposite(self) -> "Side":
        return Side.SHORT if self is Side.LONG else Side.LONG


class AssetClass(StrEnum):
    """What kind of thing is being traded.

    Domain, not metadata: a tick of EURUSD on one lot is worth a dollar and a tick of AAPL
    is worth a cent per share. The class is what tells the cost model and the sizing which
    arithmetic they are in.
    """

    FOREX = "forex"
    STOCK = "stock"
    INDEX = "index"
    FUTURE = "future"
    CRYPTO = "crypto"


class SignalKind(StrEnum):
    """Open a position, close the one that is open, or withdraw an order still waiting.

    `CANCEL` is the odd one out on purpose: it is the only kind that never becomes an
    `OrderRequest`. A resting limit order outlives the bar that placed it, so something has
    to be able to take it back — see `Signal.client_id` and `Broker.cancel` (ADR-0014).
    """

    ENTRY = "entry"
    EXIT = "exit"
    CANCEL = "cancel"


@dataclass(frozen=True, slots=True)
class InstrumentSpec:
    """A symbol and the numbers that turn a price move into money."""

    symbol: str
    name: str
    asset_class: AssetClass
    currency_quote: str
    tick_size: Money
    tick_value: Money
    contract_size: Money
    """Units of the base currency in one lot (100 000 for forex, 1 for a share).

    **Not used by `money_for`, deliberately.** The broker quotes `tick_value` *per lot*, so
    the contract size is already inside it; multiplying by it again would count the lot
    twice and inflate every P&L by five orders of magnitude. It is kept because MT5 reports
    it and margin calculations (phase 2) will need it — not because the P&L does.
    """

    digits: int
    exchange: str | None = None
    currency_base: str | None = None

    def __post_init__(self) -> None:
        if self.tick_size <= ZERO:
            raise ValueError(f"{self.symbol}: tick_size must be positive, got {self.tick_size}")
        if self.tick_value <= ZERO:
            raise ValueError(f"{self.symbol}: tick_value must be positive, got {self.tick_value}")
        if self.contract_size <= ZERO:
            raise ValueError(
                f"{self.symbol}: contract_size must be positive, got {self.contract_size}"
            )

    def money_for(self, price_move: Money, volume: Volume) -> Money:
        """Convert a price movement into profit or loss.

        `(move / tick_size) * tick_value * volume` — and that single line is the whole
        reason instruments are data rather than code. One tick of EURUSD (0.00001) on a
        standard lot is worth $1; one tick of AAPL (0.01) on one share is worth one cent.
        Same formula, entirely different numbers, and no `if asset_class ==` anywhere.
        """
        return (price_move / self.tick_size) * self.tick_value * volume


@dataclass(frozen=True, slots=True)
class Candle:
    """One closed OHLCV bar.

    `time` is the bar's **opening** instant, in **UTC**. Both halves matter. Labelling a
    bar by its close would make every off-by-one in the engine invisible; UTC because a
    broker's server clock is not a clock.

    Note what this means for the anti-lookahead rule: a strategy that "decides on the close
    of candle N" stamps its order with N's *opening* time, because that is the only time a
    candle has. The engine's guard is written against that fact rather than against a
    docstring — see `loop._reject_lookahead`.
    """

    time: dt.datetime
    open: Money
    high: Money
    low: Money
    close: Money
    tick_volume: int = 0
    spread: int = 0
    real_volume: int = 0

    def __post_init__(self) -> None:
        _require_utc(self.time, "Candle.time")

        # A high below the body is not a candle: it is a bar at which a stop would trigger
        # where price never went. Cheap to check, and it catches a corrupt feed at the
        # boundary instead of in the P&L.
        if self.high < max(self.open, self.close) or self.low > min(self.open, self.close):
            raise ValueError(
                f"candle at {self.time} does not contain its own body: "
                f"O={self.open} H={self.high} L={self.low} C={self.close}"
            )


@dataclass(frozen=True, slots=True)
class Signal:
    """A strategy's *intent*, before anyone decides how big it should be.

    Deliberately not an order. A strategy says "go long here, with the stop at 1.0950"; how
    many lots that is depends on the account, the risk budget and the instrument — none of
    which are the strategy's business. Keeping sizing out of the strategy is what lets the
    same strategy run on a $1 000 account and a $1 000 000 one without an edit, and it is
    the same separation as the cost model (ADR-07).
    """

    kind: SignalKind
    side: Side
    """For an EXIT, the side of the position being closed."""

    reference_price: Money
    """The close that triggered the decision. Sizing measures risk against it."""

    stop_loss: Money | None = None
    take_profit: Money | None = None
    reason: str = ""
    context: Mapping[str, Money | None] | None = None
    """What the indicators read at the instant this was decided.

    Captured here, at the entry, and carried untouched all the way to the `ClosedTrade` — it
    is the training material for the phase-3 analysis ("does this only work when ADX > 25?"),
    and recomputing it afterwards would mean re-running the engine and trusting nothing moved.
    None on an exit, and on any strategy with no indicators."""

    limit_price: Money | None = None
    """Where the order should wait, or `None` to take the next open at market (ADR-0014).

    An indicator setup has no preferred price — "RSI crossed 30, buy" is answered by whatever
    the next open is. A structure setup is the opposite: it enters *at the edge of a region*,
    which is a price and not an instant, and filling it at the next open measures a trade the
    method would never have taken."""

    client_id: str | None = None
    """The name this signal gives its order, so a later bar can take it back.

    A resting order outlives the bar that placed it, and only the strategy knows when it
    stopped making sense — its zone was mitigated, or aged out of the window. To say *that
    one*, it needs a name, and the name has to come from the strategy: the engine hands out
    no ids, because the engine is not the side that can carry one across bars. Required on a
    `CANCEL`, and on anything that rests."""

    def __post_init__(self) -> None:
        # A cancel that names nothing is a strategy asking the broker to guess which order it
        # meant. Refused here rather than downstream: by the time it reaches the broker the
        # bar that could have explained it is gone.
        if self.kind is SignalKind.CANCEL and self.client_id is None:
            raise ValueError("a CANCEL names the order it withdraws: client_id is required")
        if self.limit_price is not None and self.limit_price <= ZERO:
            raise ValueError(f"limit price must be positive, got {self.limit_price}")
        # A limit rests on the side price has to come back to: a buy waits *below*, a sell
        # *above*. The wrong side is not an exotic order, it is a sign error — and it does not
        # announce itself, because a buy limit above the market simply fills at the next open
        # while being sized against a price that never existed. The structure layer computes
        # these levels from zone edges, which is exactly where a top/bottom swap happens.
        if self.kind is SignalKind.ENTRY and self.limit_price is not None:
            wrong_side = (
                self.limit_price > self.reference_price
                if self.side is Side.LONG
                else self.limit_price < self.reference_price
            )
            if wrong_side:
                raise ValueError(
                    f"a {self.side.value} limit at {self.limit_price} is on the wrong side of "
                    f"{self.reference_price}: a buy limit rests below the market, a sell above"
                )


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A sized, submittable order: at market, or resting at a `limit_price` (ADR-0014).

    `decided_at` is the **opening instant of the candle the strategy was looking at** when
    it decided. It exists so the engine can *prove* the anti-lookahead rule rather than
    trust it: a fill may only land on a strictly later bar (see `loop._reject_lookahead`).
    That proof matters most to a limit order, which is the one fill in the engine priced
    *inside* a bar rather than at its open.

    One subtlety, and PR-105 depends on it: a protective exit (a stop or a target hit
    intrabar) inherits the `decided_at` of the **entry** that placed it. That is honest —
    the stop level was decided then — and it is what lets a stop trigger on the very bar
    the position opened without tripping the lookahead guard.
    """

    symbol: str
    side: Side
    intent: SignalKind
    volume: Volume
    decided_at: dt.datetime
    stop_loss: Money | None = None
    take_profit: Money | None = None
    reason: str = ""
    context: Mapping[str, Money | None] | None = None
    """The indicator snapshot from the signal that produced this order. See `Signal.context`."""

    limit_price: Money | None = None
    """The price this order waits at, or `None` for "fill at the next open". See `Signal`."""

    client_id: str | None = None
    """The strategy's name for this order, so it can be cancelled later. See `Signal`."""

    def __post_init__(self) -> None:
        _require_utc(self.decided_at, "OrderRequest.decided_at")
        if self.volume <= ZERO:
            raise ValueError(f"order volume must be positive, got {self.volume}")
        # A cancel withdraws an order; it is not one. It carries no volume, no side and no
        # fill, and letting it be built as an `OrderRequest` would put it in the queue the
        # broker fills — which is exactly the queue it is supposed to empty.
        if self.intent is SignalKind.CANCEL:
            raise ValueError("a cancel is not an order: withdraw it through Broker.cancel")
        if self.limit_price is not None and self.limit_price <= ZERO:
            raise ValueError(f"limit price must be positive, got {self.limit_price}")


@dataclass(frozen=True, slots=True)
class OrderResult:
    """What the broker said when it was handed an order."""

    order: OrderRequest
    accepted: bool
    reason: str = ""


@dataclass(frozen=True, slots=True)
class Fill:
    """An order that actually executed, at a price, at a time, for a cost."""

    order: OrderRequest
    time: dt.datetime
    price: Money
    volume: Volume
    costs: Money

    def __post_init__(self) -> None:
        _require_utc(self.time, "Fill.time")
        if self.price <= ZERO:
            raise ValueError(f"fill price must be positive, got {self.price}")
        if self.volume <= ZERO:
            raise ValueError(f"fill volume must be positive, got {self.volume}")
        # Costs are a magnitude. A negative one is money appearing in the balance out of
        # nowhere — and it would look exactly like a profitable strategy.
        if self.costs < ZERO:
            raise ValueError(f"costs are a magnitude and cannot be negative, got {self.costs}")


@dataclass(frozen=True, slots=True)
class Position:
    """An open position. There is no `current_price` field, and that is deliberate.

    A position does not know what it is worth — marking to market needs a candle, and a
    position carrying a stale price is a position that will eventually be valued against a
    bar that has already gone.
    """

    symbol: str
    side: Side
    volume: Volume
    entry_price: Money
    entry_time: dt.datetime
    entry_costs: Money = ZERO
    stop_loss: Money | None = None
    take_profit: Money | None = None
    context: Mapping[str, Money | None] | None = None
    """The indicator snapshot from the entry that opened this position. See `Signal.context`."""


@dataclass(frozen=True, slots=True)
class AccountState:
    """Where the money is.

    `balance` is settled: it only moves when a position closes. `equity` is balance plus
    what the open position is worth right now. The gap between them is the whole reason
    drawdown is measured on equity — an account can be one bad open trade away from a
    margin call while its balance still says everything is fine.

    `currency` is the account's deposit currency, and it comes from the account. It is
    **not** the instrument's quote currency: trade USDJPY on a USD account and the quote
    currency is the yen, while every number here is in dollars.
    """

    balance: Money
    equity: Money
    currency: str = "USD"


@dataclass(frozen=True, slots=True)
class ClosedTrade:
    """A round trip. `costs` is the **whole** round trip — both legs.

    Getting that wrong is not cosmetic. Every metric in PR-106 (expectancy, profit factor,
    R-multiple) is computed from this, and the property PR-105 must satisfy is that the
    trades reconcile with the equity curve: `sum(net_pnl) == final equity - initial`. Drop
    the entry's cost here and the table reports more profit than the account ever had — in
    forex, where the entry spread is usually the dominant cost, systematically so.
    """

    symbol: str
    side: Side
    volume: Volume
    entry_time: dt.datetime
    entry_price: Money
    exit_time: dt.datetime
    exit_price: Money
    gross_pnl: Money
    costs: Money
    net_pnl: Money
    reason: str = ""
    stop_loss: Money | None = None
    take_profit: Money | None = None
    r_multiple: Money | None = None
    """Net result in multiples of the risk taken: `net_pnl / (money risked at the stop)`. The
    unit that lets trades with different sizes and stops be compared — a +2R win is the same
    edge whether it made $200 or $2 000. None when the position carried no stop to measure
    risk against."""
    context: Mapping[str, Money | None] | None = None
    """The indicator snapshot from the entry. See `Signal.context`."""


@dataclass(frozen=True, slots=True)
class EquityPoint:
    """The account's equity at the close of one candle."""

    time: dt.datetime
    equity: Money


@dataclass(frozen=True, slots=True)
class Context:
    """Everything a strategy is allowed to see, and nothing else.

    This is the anti-lookahead rule made structural. The strategy is not handed the list of
    candles and asked politely not to look ahead in it — it is handed *one* candle, the one
    that just closed. There is no future in this object to peek at.
    """

    candle: Candle
    instrument: InstrumentSpec
    account: AccountState
    position: Position | None = None

    fills: tuple[Fill, ...] = ()
    """The fills born inside this bar, before the strategy saw its close (ADR-0015).

    Not a relaxation of the anti-lookahead rule: these are bar-N events handed to a
    strategy deciding on bar N's close — the same thing a live terminal does when it
    pushes a fill notification the moment it happens. The field exists because
    `position` alone cannot report one real outcome: a limit order that fills and is
    stopped out inside a single bar opens a position that is already gone by the time
    this object is built, and a strategy that never learns its order became a trade
    will treat the order as still resting.
    """


@dataclass(frozen=True, slots=True)
class EvalContext:
    """What a DSL condition is allowed to read on one bar — and how far back.

    Richer than `Context`, which hands the strategy a single candle. A condition speaks the
    DSL's reference grammar, and that grammar reaches named indicators and closed candles N
    bars back — neither of which fits in a one-candle view. But it is the *same*
    anti-lookahead rule, not a relaxation of it: `candles[0]` is the bar that just closed,
    and there is no index into this object that reaches a bar which has not.

    **Newest-first**, deliberately. `candles[0]` is the current bar N, `candles[1]` is N-1,
    and `candle[-1]` in the DSL is `candles[1]` here. Indicator values follow the same
    convention: `indicator_values["sma_fast"][0]` is this bar's value, `[1]` the previous
    bar's, and `None` anywhere the indicator was still warming up.

    Holding only resolved *values* — candles and decimals, never indicator objects — is what
    keeps the domain free of the indicator machinery. The strategy owns the indicators and
    the rolling windows; by the time a condition sees an `EvalContext`, everything is a plain
    number the anti-lookahead rule has already vouched for.
    """

    candles: tuple[Candle, ...]
    indicator_values: Mapping[str, tuple[Money | None, ...]]
    position: Position | None = None

    def candle_at(self, offset: int) -> Candle | None:
        """The bar `offset` steps back from the current one (0 = now), or `None` past the
        edge of what has been seen. A ref reaching past the horizon is not an error — early
        in a run there simply is no candle there, and the condition that asked is false."""
        if 0 <= offset < len(self.candles):
            return self.candles[offset]
        return None

    def indicator_at(self, indicator_id: str, offset: int) -> Money | None:
        """This indicator's value `offset` bars back (0 = now), or `None` if it is unknown —
        the indicator was warming up, or there is not yet that much history to look back on.
        An unknown id resolves to `None`; the compiler is what proves ids exist, not this."""
        history = self.indicator_values.get(indicator_id)
        if history is None or not (0 <= offset < len(history)):
            return None
        return history[offset]
