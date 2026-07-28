"""Smoke test: the engine is importable from the installed workspace package.

This is deliberately not ``assert True``. It fails if the uv workspace wiring or
the src-layout packaging is broken, which is exactly what PR-001 must guarantee.

It also holds the conformance check for the `Broker` seam. That check lives at package level
rather than beside either implementation because it is a statement about *all* of them: the
protocol grew a fifth verb in ADR-0018, and the cost of a new verb is that every adapter —
including the `MT5Broker` that does not exist yet — has to answer for it. A protocol nobody
asserts against is a protocol that is quietly optional.
"""

import datetime as dt
from decimal import Decimal

import tradeforge_engine
from tradeforge_engine.backtest_broker import BacktestBroker
from tradeforge_engine.protocols import Broker
from tradeforge_engine.testing import EURUSD, START, ImmediateFillBroker


def test_engine_exposes_a_version() -> None:
    assert tradeforge_engine.__version__ == "0.1.0"


def test_both_brokers_satisfy_the_broker_protocol() -> None:
    """Structural typing is checked at the type checker, and `runtime_checkable` lets a test
    check it too. mypy would already catch a missing method where a `Broker` is *passed*; this
    catches it in an implementation nobody has wired up yet."""
    assert isinstance(BacktestBroker(instrument=EURUSD, initial_capital=Decimal(10_000)), Broker)
    assert isinstance(ImmediateFillBroker(instrument=EURUSD), Broker)


def test_a_broker_missing_a_verb_does_not_satisfy_the_protocol() -> None:
    """The assertion above only means something if the protocol can actually say no. Without
    this, a `Broker` that had lost every method would still look like it passed."""

    class HalfABroker:
        def submit(self) -> None: ...
        def cancel(self) -> None: ...
        def on_bar(self) -> None: ...
        def positions(self) -> None: ...
        def account(self) -> None: ...
        def trades(self) -> None: ...

    assert not isinstance(HalfABroker(), Broker)


def test_the_immediate_broker_has_nothing_to_cancel_or_protect() -> None:
    """Everything pending fills at the very next open — nothing rests, and no position it holds
    carries a protective level. Both answers are `False`, which is what a real broker says about
    an order or a position it cannot find, so the loop needs no special case for either."""
    broker = ImmediateFillBroker(instrument=EURUSD)

    assert broker.cancel("never-existed") is False
    assert broker.modify_stop("EURUSD", Decimal("1.09500"), START + dt.timedelta(hours=1)) is False
