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
    """Open a position, or close the one that is open."""

    ENTRY = "entry"
    EXIT = "exit"


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


@dataclass(frozen=True, slots=True)
class OrderRequest:
    """A sized, submittable order. Market orders only in phase 1.

    `decided_at` is the **opening instant of the candle the strategy was looking at** when
    it decided. It exists so the engine can *prove* the anti-lookahead rule rather than
    trust it: a fill may only land on a strictly later bar (see `loop._reject_lookahead`).

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

    def __post_init__(self) -> None:
        _require_utc(self.decided_at, "OrderRequest.decided_at")
        if self.volume <= ZERO:
            raise ValueError(f"order volume must be positive, got {self.volume}")


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
