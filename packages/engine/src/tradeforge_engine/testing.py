"""Test doubles for the things that plug into the engine.

Shipped with the package rather than hidden in a test folder, for two reasons. The engine's
tests need them; so will PR-104's and PR-105's, and a helper that three suites copy is a
helper that drifts into three versions. And anyone writing their own `Broker` or
`RiskManager` against these protocols gets a working reference implementation — which is
what an interface is *for*.

Nothing here inherits from anything. `ImmediateFillBroker` satisfies `Broker` by having the
right methods and no other relationship to the engine at all.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

from tradeforge_engine.domain import (
    ZERO,
    AccountState,
    AssetClass,
    Candle,
    ClosedTrade,
    Context,
    Fill,
    InstrumentSpec,
    Money,
    OrderRequest,
    OrderResult,
    Position,
    Side,
    Signal,
    SignalKind,
    Volume,
)
from tradeforge_engine.portfolio import Portfolio

EURUSD = InstrumentSpec(
    symbol="EURUSD",
    name="Euro vs US Dollar",
    asset_class=AssetClass.FOREX,
    currency_base="EUR",
    currency_quote="USD",
    tick_size=Decimal("0.00001"),
    tick_value=Decimal("1"),
    contract_size=Decimal("100000"),
    digits=5,
)

AAPL = InstrumentSpec(
    symbol="AAPL",
    name="Apple Inc.",
    asset_class=AssetClass.STOCK,
    exchange="NASDAQ",
    currency_quote="USD",
    tick_size=Decimal("0.01"),
    tick_value=Decimal("0.01"),
    contract_size=Decimal("1"),
    digits=2,
)

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
HOUR = dt.timedelta(hours=1)


def bar(
    index: int,
    *,
    open_: str,
    close: str,
    high: str | None = None,
    low: str | None = None,
) -> Candle:
    """One candle, `index` hours after the start."""
    body = [Decimal(open_), Decimal(close)]
    return Candle(
        time=START + index * HOUR,
        open=Decimal(open_),
        high=Decimal(high) if high else max(body),
        low=Decimal(low) if low else min(body),
        close=Decimal(close),
    )


def rising(count: int, *, start: str = "1.10000", step: str = "0.00100") -> list[Candle]:
    """A market that only goes up. Boring on purpose — the loop is what is under test."""
    price = Decimal(start)
    delta = Decimal(step)

    bars: list[Candle] = []
    for index in range(count):
        bars.append(bar(index, open_=str(price), close=str(price + delta)))
        price += delta
    return bars


def falling(count: int, *, start: str = "1.20000", step: str = "0.00100") -> list[Candle]:
    """A market that only goes down — so that shorts get exercised, not just longs."""
    return rising(count, start=start, step=f"-{step}")


def entry(
    side: Side = Side.LONG,
    *,
    price: str = "1.10100",
    stop: str | None = None,
    reason: str = "test",
) -> Signal:
    return Signal(
        kind=SignalKind.ENTRY,
        side=side,
        reference_price=Decimal(price),
        stop_loss=Decimal(stop) if stop else None,
        reason=reason,
    )


def close_out(side: Side = Side.LONG, *, price: str = "1.10100", reason: str = "test") -> Signal:
    return Signal(kind=SignalKind.EXIT, side=side, reference_price=Decimal(price), reason=reason)


class ImmediateFillBroker:
    """Fills whatever is pending at the next bar's open. The honest minimum.

    **Not** `BacktestBroker` — that is PR-105, with slippage, a cost model and intrabar
    stops. This exists so the loop has something to talk to, and so the tests can assert
    *when* a fill happens without a fill model muddying the question.

    It honours the three obligations in the `Broker` protocol: it marks to market before
    returning, it sorts exits before entries, and it never invents a `decided_at`.
    """

    def __init__(
        self,
        *,
        initial_capital: Money = Decimal(10_000),
        instrument: InstrumentSpec = EURUSD,
        costs: Money = ZERO,
    ) -> None:
        self._instrument = instrument
        self._portfolio = Portfolio(initial_capital=initial_capital, instrument=instrument)
        self._costs = costs
        self._pending: list[OrderRequest] = []
        self.submitted: list[OrderRequest] = []

    def submit(self, order: OrderRequest) -> OrderResult:
        self._pending.append(order)
        self.submitted.append(order)
        return OrderResult(order=order, accepted=True)

    def cancel(self, client_id: str) -> bool:  # noqa: ARG002
        """Nothing ever rests here: everything pending fills at the very next open, so by the
        time anyone could withdraw an order it has already executed. Always false, which is
        the same answer the real broker gives for an order it cannot find."""
        return False

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        # Exits before entries: a reversal on one bar has to close before it opens, or the
        # ledger refuses the second position on a sequence that was merely mis-sorted.
        pending = sorted(self._pending, key=lambda order: order.intent is SignalKind.ENTRY)
        self._pending.clear()

        fills = [
            Fill(
                order=order,
                time=candle.time,
                price=candle.open,
                volume=order.volume,
                costs=self._costs,
            )
            for order in pending
        ]

        for fill in fills:
            self._portfolio.apply(fill)

        # The protocol requires it: without this, the equity curve is a balance curve and
        # the maximum drawdown of a strategy that halved the account comes out as zero.
        self._portfolio.mark_to_market(candle)
        return fills

    def positions(self, symbol: str) -> Sequence[Position]:
        position = self._portfolio.position
        return (position,) if position and position.symbol == symbol else ()

    def account(self) -> AccountState:
        return self._portfolio.account()

    def trades(self) -> Sequence[ClosedTrade]:
        return self._portfolio.trades


@dataclass
class ScriptedStrategy:
    """Emits signals from a script keyed by candle index. No indicators, no conditions."""

    script: dict[int, list[Signal]] = field(default_factory=dict)
    seen: list[Candle] = field(default_factory=list)
    positions_seen: list[Side | None] = field(default_factory=list)
    fills_seen: list[tuple[Fill, ...]] = field(default_factory=list)

    def on_bar(self, context: Context) -> Sequence[Signal]:
        index = len(self.seen)
        self.seen.append(context.candle)
        self.positions_seen.append(context.position.side if context.position else None)
        self.fills_seen.append(context.fills)
        return self.script.get(index, [])


class FixedRisk:
    """Always the same size, always allowed (unless told otherwise).

    The real sizing arithmetic — percent-risk against the stop distance — is PR-105. Here the
    point is only that the loop asks, and honours the answer.
    """

    def __init__(self, *, volume: Volume = Decimal(1), allow_all: bool = True) -> None:
        self._volume = volume
        self._allow = allow_all
        self.sized: list[Signal] = []
        self.vetoed: list[OrderRequest] = []

    def size(self, signal: Signal, account: AccountState, instrument: InstrumentSpec) -> Volume:  # noqa: ARG002
        self.sized.append(signal)
        return self._volume

    def allow(self, order: OrderRequest, account: AccountState) -> bool:  # noqa: ARG002
        if not self._allow:
            self.vetoed.append(order)
        return self._allow
