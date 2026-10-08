"""Signals: a paper session that tells the world what its setup is doing (signals PR 5).

`SignalBroker` wraps the session's simulated broker and passes every call through untouched —
the fills, the stops, the trades are the backtest's own, bar for bar — while it notes four moments
in a signal's life and hands each to a `SignalSink`:

* **armed** — a pending stop/limit entry was accepted (an order now rests at a price);
* **triggered** — an entry filled;
* **cancelled** — a resting entry went away unfilled: the setup withdrew it, or the market ended it;
* **closed** — the position ended, with its result in R; or, for a setup with **no target**, the
  price reached `no_target_r` R first (his rule of 08/10: 5R, the stop, or the setup's exit).

A wrapper and not a change to the engine: the engine stays the one that backtests (AGENTS §5.3),
and nothing a signal does can move a fill.

Every event of one signal carries the same **number**, drawn at its first event, so a channel
reading "#41 triggered" can find "#41 armed" above it. Messages are never edited (his call), so the
number is the thread.
"""

import datetime as dt
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Protocol

from redis import Redis

from tradeforge_engine.domain import (
    AccountState,
    Candle,
    ClosedTrade,
    Fill,
    OrderRequest,
    OrderResult,
    Position,
    Refusal,
    RefusedBy,
    Side,
    SignalKind,
)
from tradeforge_engine.protocols import Broker

__all__ = [
    "SIGNALS_STREAM",
    "SIGNAL_NUMBER_KEY",
    "NumberedSink",
    "RedisSignalSink",
    "SignalBroker",
    "SignalEvent",
    "SignalKindOf",
    "SignalSink",
]


class SignalKindOf(StrEnum):
    """The four moments a signal is posted at."""

    ARMED = "armed"
    TRIGGERED = "triggered"
    CANCELLED = "cancelled"
    CLOSED = "closed"


@dataclass(frozen=True, slots=True)
class SignalEvent:
    """One moment of one signal, with every number a message needs."""

    kind: SignalKindOf
    number: int
    symbol: str
    side: Side
    time: dt.datetime
    """The bar the event was seen on (UTC)."""
    entry: Decimal | None = None
    """Where it rests (armed) or where it filled (triggered and after)."""
    stop: Decimal | None = None
    target: Decimal | None = None
    """`None` for a setup with no target — the message then says it closes at `no_target_r`."""
    order_type: str | None = None
    """`stop` or `limit` for a resting entry; `market` for one that filled at the next open."""
    exit_price: Decimal | None = None
    result_r: Decimal | None = None
    reason: str = ""


class SignalSink(Protocol):
    """Where events go: a Redis stream in production, a list in tests."""

    def emit(self, event: SignalEvent) -> None:
        """Publish one event. Must not raise for a transient failure it can retry itself."""
        ...


class NumberedSink(SignalSink, Protocol):
    """A sink that also hands out signal numbers — what a session is given."""

    def number(self) -> int:
        """The next signal number."""
        ...


@dataclass(slots=True)
class _Open:
    """A signal whose position is on: what its close is measured against."""

    number: int
    side: Side
    entry: Decimal
    stop: Decimal | None
    target: Decimal | None
    since: dt.datetime
    done: bool = False
    """Closed for the signal (reached `no_target_r`) while the position runs on in the ledger."""


@dataclass(slots=True)
class _Book:
    resting: dict[str, tuple[int, OrderRequest]] = field(default_factory=dict)
    open: dict[str, _Open] = field(default_factory=dict)
    """Per symbol — the ledger holds one position per symbol."""
    trades_seen: int = 0


class SignalBroker:
    """A `Broker` that is `inner` exactly, and reports the four moments to `sink`."""

    def __init__(
        self,
        inner: Broker,
        sink: SignalSink,
        *,
        number: Callable[[], int],
        take_profit_rr: Decimal | None,
        no_target_r: Decimal,
    ) -> None:
        self._inner = inner
        self._sink = sink
        self._number = number
        self._rr = take_profit_rr
        self._no_target_r = no_target_r
        self._book = _Book()

    # --- the four moments -------------------------------------------------------------------

    def submit(self, order: OrderRequest) -> OrderResult:
        """Pass through; a resting entry accepted is **armed**."""
        result = self._inner.submit(order)
        level = order.stop_price if order.stop_price is not None else order.limit_price
        if result.accepted and order.client_id is not None and level is not None:
            number = self._number()
            self._book.resting[order.client_id] = (number, order)
            self._sink.emit(
                SignalEvent(
                    kind=SignalKindOf.ARMED,
                    number=number,
                    symbol=order.symbol,
                    side=order.side,
                    time=order.decided_at,
                    entry=level,
                    stop=order.stop_loss,
                    target=self._target(order, level),
                    order_type="stop" if order.stop_price is not None else "limit",
                    reason=order.reason,
                )
            )
        return result

    def cancel(self, client_id: str) -> bool:
        """Pass through; a resting entry withdrawn is **cancelled**."""
        cancelled = self._inner.cancel(client_id)
        if cancelled:
            self._withdrawn(client_id, reason="the setup withdrew it", time=None)
        return cancelled

    def refusals(self) -> Sequence[Refusal]:
        """Pass through; a resting entry the market ended is **cancelled**."""
        refusals = self._inner.refusals()
        for refusal in refusals:
            if refusal.refused_by is RefusedBy.MARKET and refusal.client_id is not None:
                self._withdrawn(refusal.client_id, reason=refusal.reason, time=None)
        return refusals

    def on_bar(self, candle: Candle) -> Sequence[Fill]:
        """Pass through; then entries filled are **triggered**, trades ended are **closed**, and a
        target-less signal that reached `no_target_r` is closed for the channel."""
        fills = self._inner.on_bar(candle)
        for fill in fills:
            if fill.order.intent is SignalKind.ENTRY:
                self._triggered(fill)
        trades = self._inner.trades()
        for trade in trades[self._book.trades_seen :]:
            self._closed(trade)
        self._book.trades_seen = len(trades)
        self._reached_r(candle)
        return fills

    # --- untouched --------------------------------------------------------------------------

    def modify_stop(self, symbol: str, stop_loss: Decimal, decided_at: dt.datetime) -> bool:
        """Pass through. A trailed stop changes no message: the signal keeps its initial risk."""
        return self._inner.modify_stop(symbol, stop_loss, decided_at)

    def positions(self, symbol: str) -> Sequence[Position]:
        """Pass through."""
        return self._inner.positions(symbol)

    def account(self) -> AccountState:
        """Pass through."""
        return self._inner.account()

    def trades(self) -> Sequence[ClosedTrade]:
        """Pass through."""
        return self._inner.trades()

    # --- bookkeeping ------------------------------------------------------------------------

    def _target(self, order: OrderRequest, entry: Decimal) -> Decimal | None:
        if order.take_profit is not None:
            return order.take_profit
        if self._rr is None or order.stop_loss is None:
            return None
        risk = abs(entry - order.stop_loss)
        return entry + risk * self._rr if order.side is Side.LONG else entry - risk * self._rr

    def _withdrawn(self, client_id: str, *, reason: str, time: dt.datetime | None) -> None:
        found = self._book.resting.pop(client_id, None)
        if found is None:
            return
        number, order = found
        self._sink.emit(
            SignalEvent(
                kind=SignalKindOf.CANCELLED,
                number=number,
                symbol=order.symbol,
                side=order.side,
                time=time or order.decided_at,
                entry=order.stop_price if order.stop_price is not None else order.limit_price,
                stop=order.stop_loss,
                reason=reason,
            )
        )

    def _triggered(self, fill: Fill) -> None:
        order = fill.order
        rested = (
            self._book.resting.pop(order.client_id, None) if order.client_id is not None else None
        )
        number = rested[0] if rested is not None else self._number()
        target = self._target(order, fill.price)
        self._book.open[order.symbol] = _Open(
            number=number,
            side=order.side,
            entry=fill.price,
            stop=order.stop_loss,
            target=target,
            since=fill.time,
        )
        self._sink.emit(
            SignalEvent(
                kind=SignalKindOf.TRIGGERED,
                number=number,
                symbol=order.symbol,
                side=order.side,
                time=fill.time,
                entry=fill.price,
                stop=order.stop_loss,
                target=target,
                order_type=(
                    "stop"
                    if order.stop_price is not None
                    else "limit"
                    if order.limit_price is not None
                    else "market"
                ),
                reason=order.reason,
            )
        )

    def _closed(self, trade: ClosedTrade) -> None:
        signal = self._book.open.pop(trade.symbol, None)
        if signal is None or signal.done:
            return
        self._sink.emit(
            SignalEvent(
                kind=SignalKindOf.CLOSED,
                number=signal.number,
                symbol=trade.symbol,
                side=trade.side,
                time=trade.exit_time,
                entry=trade.entry_price,
                stop=trade.stop_loss,
                target=signal.target,
                exit_price=trade.exit_price,
                result_r=trade.r_multiple,
                reason=trade.reason,
            )
        )

    def _reached_r(self, candle: Candle) -> None:
        for symbol, signal in self._book.open.items():
            if signal.done or signal.target is not None or signal.stop is None:
                continue
            if candle.time <= signal.since:
                continue  # the fill bar's own range is not known to come after the fill
            risk = abs(signal.entry - signal.stop)
            if risk == 0:
                continue
            reach = self._no_target_r * risk
            level = signal.entry + reach if signal.side is Side.LONG else signal.entry - reach
            hit = candle.high >= level if signal.side is Side.LONG else candle.low <= level
            if not hit:
                continue
            signal.done = True
            self._sink.emit(
                SignalEvent(
                    kind=SignalKindOf.CLOSED,
                    number=signal.number,
                    symbol=symbol,
                    side=signal.side,
                    time=candle.time,
                    entry=signal.entry,
                    stop=signal.stop,
                    exit_price=level,
                    result_r=self._no_target_r,
                    reason=f"reached {self._no_target_r:g}R with no target",
                )
            )


SIGNALS_STREAM = "signals.events"
"""The stream the notifier reads (signals PR 6). One stream for every session: one channel."""

SIGNAL_NUMBER_KEY = "signals:number"
"""The counter a signal's number is drawn from — shared, so numbers never repeat across sessions."""


class RedisSignalSink:
    """Publishes each event to `signals.events`, with the session's own context on every entry.

    `context` is what a message needs and the event does not carry: the setup's name, the
    timeframe, the broker, the session and watch item ids. Strings, because a stream holds strings.
    """

    def __init__(self, client: Redis, context: dict[str, str]) -> None:
        self._client = client
        self._context = dict(context)

    def number(self) -> int:
        """The next signal number, shared by every session."""
        return int(str(self._client.incr(SIGNAL_NUMBER_KEY)))

    def emit(self, event: SignalEvent) -> None:
        """One stream entry per event; empty values are left out rather than written as ''."""
        fields = {
            **self._context,
            "kind": event.kind.value,
            "number": str(event.number),
            "symbol": event.symbol,
            "side": event.side.value,
            "time": event.time.isoformat(),
            "entry": "" if event.entry is None else str(event.entry),
            "stop": "" if event.stop is None else str(event.stop),
            "target": "" if event.target is None else str(event.target),
            "order_type": event.order_type or "",
            "exit_price": "" if event.exit_price is None else str(event.exit_price),
            "result_r": "" if event.result_r is None else str(event.result_r),
            "reason": event.reason,
        }
        self._client.xadd(
            SIGNALS_STREAM,
            {key: value for key, value in fields.items() if value != ""},
            maxlen=100_000,
        )
