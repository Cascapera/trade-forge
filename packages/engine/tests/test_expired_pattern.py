"""A pattern whose order price has already been passed is dropped — his option A, 2026-09-23.

Found in the 23/09 sweep: 144 runs of `mme9_breakout` with the hammer entry and a long-average
filter died on `a long stop at 1.16221 is on the wrong side of 1.16281`. The hammer closed at
05:30 (high 1.16220), the long average was still warming up on that bar — the data starts at
2022-09-09 05:00, a hundred M15 bars earlier — so the filter held the order back; at 05:45 the
average was ready, the hammer's window was still open, and the order was offered on a bar that had
already gone to 1.16284 and closed at 1.16281. A buy stop below the market: the signal refused it
by raising, and the run ended.

His call: the trade the pattern described no longer exists, so nothing is placed and the pattern's
clock starts over. These tests drive the shared reconciliation directly with a clock that offers a
fixed order, because the rule belongs to the reconciliation, not to any one host.
"""

from dataclasses import dataclass, field
from decimal import Decimal, localcontext

import pytest

from tradeforge_engine.average_setups import AverageEntryPoint, PatternOrder
from tradeforge_engine.domain import Candle, Context, Side, SignalKind
from tradeforge_engine.loop import ENGINE_CONTEXT
from tradeforge_engine.swing import _reconcile_pattern
from tradeforge_engine.testing import AAPL, ImmediateFillBroker, bar

_ACCOUNT = ImmediateFillBroker(instrument=AAPL).account()


@dataclass
class _Clock:
    """Offers the same order on every bar until reset, and counts the resets."""

    order: PatternOrder
    resets: int = 0
    offered: list[Candle] = field(default_factory=list)

    def observe(self, candle: Candle, average: Decimal, *, tick: Decimal) -> PatternOrder | None:
        self.offered.append(candle)
        return self.order

    def reset(self) -> None:
        self.resets += 1


@dataclass
class _Filter:
    """A long-average filter that says what it is told to — the one knob these tests turn."""

    allowing: bool

    def allows(self, entry: Decimal) -> bool:
        return self.allowing

    def value(self) -> Decimal:
        return Decimal(90)

    def series(self) -> tuple[()]:
        return ()


def _reconcile(
    clock: _Clock, candle: Candle, side: Side, long_average: _Filter | None = None
) -> tuple[list[object], object]:
    with localcontext(ENGINE_CONTEXT):
        signals, armed, _ = _reconcile_pattern(
            clock,  # type: ignore[arg-type]
            Context(candle=candle, instrument=AAPL, account=_ACCOUNT),
            average=Decimal(95),
            side=side,
            name="mme9",
            entry_point=AverageEntryPoint.MARTELO,
            armed=None,
            count=0,
            series=(),
            long_average=long_average,  # type: ignore[arg-type]
        )
    return list(signals), armed


def _order(
    side: Side, *, stop_price: Decimal | None = None, limit_price: Decimal | None = None
) -> PatternOrder:
    """A pattern's order, its protective stop on the far side of the market."""
    return PatternOrder(
        side=side,
        stop_loss=Decimal(95) if side is Side.LONG else Decimal(105),
        stop_price=stop_price,
        limit_price=limit_price,
    )


def _closing_at(close: str) -> Candle:
    """A bar closing at `close` that opened somewhere else: the rule reads the close, and a bar
    whose open equals its close could not tell the two apart."""
    price = Decimal(close)
    return bar(
        0,
        open_=str(price - Decimal("0.5")),
        close=close,
        high=str(price + Decimal(1)),
        low=str(price - Decimal(1)),
    )


@pytest.mark.parametrize(
    ("side", "stop", "close"),
    [
        # The case that found it, in AAPL's prices: a buy stop at 100.11 on a bar closing at 100.81.
        (Side.LONG, "100.11", "100.81"),
        (Side.SHORT, "99.89", "99.19"),
    ],
)
def test_a_stop_the_close_has_already_gone_through_is_dropped(
    side: Side, stop: str, close: str
) -> None:
    """Nothing placed, no error raised, and the clock reset: the pattern is over, not paused."""
    clock = _Clock(_order(side, stop_price=Decimal(stop)))
    signals, armed = _reconcile(clock, _closing_at(close), side)

    assert signals == []
    assert armed is None
    assert clock.resets == 1


@pytest.mark.parametrize(
    ("side", "limit", "close"),
    [
        (Side.LONG, "100.50", "100.20"),  # a buy limit above the market
        (Side.SHORT, "99.50", "99.80"),  # a sell limit below it
    ],
)
def test_a_limit_the_close_has_already_gone_through_is_dropped(
    side: Side, limit: str, close: str
) -> None:
    clock = _Clock(_order(side, limit_price=Decimal(limit)))
    signals, armed = _reconcile(clock, _closing_at(close), side)

    assert signals == []
    assert armed is None
    assert clock.resets == 1


@pytest.mark.parametrize(
    ("side", "stop", "close"),
    [
        (Side.LONG, "100.11", "100.05"),
        (
            Side.LONG,
            "100.11",
            "100.11",
        ),  # exactly at the close: the signal accepts it, and so do we
        (Side.SHORT, "99.89", "99.95"),
        (Side.SHORT, "99.89", "99.89"),
    ],
)
def test_an_order_still_ahead_of_price_is_placed_as_before(
    side: Side, stop: str, close: str
) -> None:
    clock = _Clock(_order(side, stop_price=Decimal(stop)))
    signals, armed = _reconcile(clock, _closing_at(close), side)

    [entry] = signals
    assert entry.kind is SignalKind.ENTRY  # type: ignore[attr-defined]
    assert entry.stop_price == Decimal(stop)  # type: ignore[attr-defined]
    assert armed is not None
    assert clock.resets == 0


@pytest.mark.parametrize(
    ("side", "limit", "close"),
    [(Side.LONG, "100.20", "100.20"), (Side.SHORT, "99.80", "99.80")],
)
def test_a_limit_exactly_at_the_close_is_placed_as_the_signal_accepts_it(
    side: Side, limit: str, close: str
) -> None:
    clock = _Clock(_order(side, limit_price=Decimal(limit)))
    signals, armed = _reconcile(clock, _closing_at(close), side)

    [entry] = signals
    assert entry.limit_price == Decimal(limit)  # type: ignore[attr-defined]
    assert armed is not None
    assert clock.resets == 0


def test_while_the_filter_holds_the_order_back_the_pattern_stays_alive() -> None:
    """⚠️ His option (a), 23/09. The order has been passed by the close, but the filter is still
    holding it: the filter gates placing and never takes the setup apart, so the pattern is kept —
    no reset — exactly as before this change. It is dropped only on the bar the filter lets it
    through, which is the one bar the old code raised on."""
    clock = _Clock(_order(Side.LONG, stop_price=Decimal("100.11")))
    passed = _closing_at("100.81")

    held, armed = _reconcile(clock, passed, Side.LONG, _Filter(allowing=False))
    assert held == []
    assert armed is None
    assert clock.resets == 0

    released, armed = _reconcile(clock, passed, Side.LONG, _Filter(allowing=True))
    assert released == []
    assert armed is None
    assert clock.resets == 1
