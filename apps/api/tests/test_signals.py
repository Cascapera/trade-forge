"""The signal broker: the backtest's own fills, and the four moments posted (signals PR 5)."""

import datetime as dt
from decimal import Decimal

from tradeforge_api.live.signals import SignalBroker, SignalEvent, SignalKindOf
from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.domain import (
    AssetClass,
    Candle,
    InstrumentSpec,
    OrderRequest,
    Side,
    SignalKind,
)

T0 = dt.datetime(2026, 10, 8, 13, 0, tzinfo=dt.UTC)
STEP = dt.timedelta(hours=1)
WIN = InstrumentSpec(
    symbol="WIN",
    name="IBOVESPA MINI",
    asset_class=AssetClass.FUTURE,
    currency_quote="BRL",
    tick_size=Decimal(1),
    tick_value=Decimal("0.2"),
    contract_size=Decimal(1),
    digits=0,
)


class Sink:
    def __init__(self) -> None:
        self.events: list[SignalEvent] = []
        self.counter = 40

    def emit(self, event: SignalEvent) -> None:
        self.events.append(event)

    def number(self) -> int:
        self.counter += 1
        return self.counter


def bar(i: int, low: int, high: int) -> Candle:
    o = c = Decimal((low + high) // 2)
    return Candle(T0 + i * STEP, o, Decimal(high), Decimal(low), c)


def armed_long(**fields: object) -> OrderRequest:
    """Buy stop at 1000, stop 900: one R is 100 points."""
    order: dict[str, object] = {
        "symbol": "WIN",
        "side": Side.LONG,
        "intent": SignalKind.ENTRY,
        "volume": Decimal(1),
        "decided_at": T0,
        "stop_loss": Decimal(900),
        "stop_price": Decimal(1000),
        "client_id": "c1",
        "reason": "CHOCH",
    }
    order.update(fields)
    return OrderRequest(**order)  # type: ignore[arg-type]


def broker(rr: Decimal | None = None) -> tuple[SignalBroker, Sink]:
    sink = Sink()
    inner = BacktestBroker(instrument=WIN, initial_capital=Decimal(100_000), take_profit_rr=rr)
    wrapped = SignalBroker(
        inner, sink, number=sink.number, take_profit_rr=rr, no_target_r=Decimal(5)
    )
    return wrapped, sink


def kinds(sink: Sink) -> list[tuple[SignalKindOf, int]]:
    return [(event.kind, event.number) for event in sink.events]


def test_armed_then_withdrawn_is_cancelled_under_one_number() -> None:
    signals, sink = broker()
    signals.submit(armed_long())
    assert signals.cancel("c1")

    assert kinds(sink) == [(SignalKindOf.ARMED, 41), (SignalKindOf.CANCELLED, 41)]
    armed = sink.events[0]
    assert (armed.entry, armed.stop, armed.target, armed.order_type) == (
        Decimal(1000),
        Decimal(900),
        None,
        "stop",
    )


def test_armed_triggered_and_stopped_out_closes_at_minus_one_r() -> None:
    signals, sink = broker()
    signals.submit(armed_long())
    signals.on_bar(bar(1, 950, 1010))  # reaches 1000: filled
    signals.on_bar(bar(2, 890, 990))  # reaches 900: stopped

    assert kinds(sink) == [
        (SignalKindOf.ARMED, 41),
        (SignalKindOf.TRIGGERED, 41),
        (SignalKindOf.CLOSED, 41),
    ]
    closed = sink.events[-1]
    assert closed.result_r == Decimal(-1)
    assert closed.exit_price == Decimal(900)


def test_a_setup_with_a_target_closes_at_its_target() -> None:
    signals, sink = broker(rr=Decimal(2))
    signals.submit(armed_long())
    signals.on_bar(bar(1, 950, 1010))
    signals.on_bar(bar(2, 1050, 1250))  # target 1200

    triggered, closed = sink.events[1], sink.events[2]
    assert triggered.target == Decimal(1200)
    assert (closed.kind, closed.result_r, closed.exit_price) == (
        SignalKindOf.CLOSED,
        Decimal(2),
        Decimal(1200),
    )


def test_with_no_target_the_signal_closes_at_five_r_and_says_nothing_after() -> None:
    signals, sink = broker()
    signals.submit(armed_long())
    signals.on_bar(bar(1, 950, 1010))
    signals.on_bar(bar(2, 1100, 1400))  # 4R: still open
    signals.on_bar(bar(3, 1300, 1520))  # 5R = 1500: closed for the channel
    signals.on_bar(bar(4, 890, 1300))  # the ledger's own stop later: no second close

    assert kinds(sink) == [
        (SignalKindOf.ARMED, 41),
        (SignalKindOf.TRIGGERED, 41),
        (SignalKindOf.CLOSED, 41),
    ]
    assert (sink.events[-1].result_r, sink.events[-1].exit_price) == (Decimal(5), Decimal(1500))
    assert len(signals.trades()) == 1, "the simulation ran on underneath, untouched"


def test_the_wrapper_moves_no_fill() -> None:
    """The same bars through a bare broker and through the wrapper: the same trades."""
    bars = [bar(1, 950, 1010), bar(2, 1100, 1400), bar(3, 890, 1300)]
    bare = BacktestBroker(instrument=WIN, initial_capital=Decimal(100_000))
    bare.submit(armed_long())
    wrapped, _ = broker()
    wrapped.submit(armed_long())
    for candle in bars:
        bare.on_bar(candle)
        wrapped.on_bar(candle)

    assert wrapped.trades() == bare.trades()
    assert wrapped.account() == bare.account()


def test_each_signal_gets_its_own_number() -> None:
    signals, sink = broker()
    signals.submit(armed_long())
    signals.cancel("c1")
    signals.submit(armed_long(client_id="c2"))

    assert [number for _, number in kinds(sink)] == [41, 41, 42]


def test_a_picture_opens_on_the_warm_up_bars_not_on_the_one_seen_live() -> None:
    """09/10: the first ACIONADO ever posted was drawn on one candle — the session had just
    opened, and the bars it warmed over were never handed to the wrapper."""
    drawn: list[int] = []

    def picture(candles: object, **_: object) -> bytes:
        drawn.append(len(list(candles)))  # type: ignore[call-overload]
        return b"png"

    sink = Sink()
    inner = BacktestBroker(instrument=WIN, initial_capital=Decimal(100_000))
    signals = SignalBroker(
        inner,
        sink,
        number=sink.number,
        take_profit_rr=None,
        no_target_r=Decimal(5),
        picture=picture,
        recent=[bar(-index, 950, 990) for index in range(30, 0, -1)],
    )
    signals.submit(armed_long())
    signals.on_bar(bar(1, 950, 1010))  # filled: the picture of the trigger

    triggered = sink.events[-1]
    assert triggered.kind is SignalKindOf.TRIGGERED
    assert triggered.image == b"png"
    assert drawn[-1] == 31
