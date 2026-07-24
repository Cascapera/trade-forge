"""Percent-risk sizing, and the two ways it declines.

The whole point of sizing against the stop is that the same strategy risks the same fraction
on any account and any stop distance. The golden here is one hand-checked size; the rest is
the boundary behaviour — no stop, no distance — where the honest answer is "no trade", never
a default size nobody chose.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.domain import AccountState, OrderRequest, Side, Signal, SignalKind
from tradeforge_engine.risk import PercentRiskManager
from tradeforge_engine.testing import EURUSD

ACCOUNT = AccountState(balance=Decimal(10_000), equity=Decimal(10_000))


def a_signal(*, reference: str, stop: str | None) -> Signal:
    return Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal(reference),
        stop_loss=Decimal(stop) if stop is not None else None,
    )


def test_size_puts_exactly_the_percent_at_risk() -> None:
    """1% of $10 000 is $100. A stop 100 pips away (1.10000 → 1.09000) loses $1 000 per lot on
    EURUSD, so the size that risks exactly $100 is 0.1 lots."""
    risk = PercentRiskManager(percent=Decimal(1))
    volume = risk.size(a_signal(reference="1.10000", stop="1.09000"), ACCOUNT, EURUSD)
    assert volume == Decimal("0.1")


def test_a_tighter_stop_buys_a_bigger_position() -> None:
    """Half the stop distance, double the size — same money at risk. This is the property that
    makes position size a consequence of the stop, not a free parameter."""
    risk = PercentRiskManager(percent=Decimal(1))
    wide = risk.size(a_signal(reference="1.10000", stop="1.09000"), ACCOUNT, EURUSD)
    tight = risk.size(a_signal(reference="1.10000", stop="1.09500"), ACCOUNT, EURUSD)
    assert tight == wide * 2


def test_no_stop_means_no_trade() -> None:
    """Percent-risk is meaningless with no distance to measure. Zero, not a guessed default."""
    risk = PercentRiskManager(percent=Decimal(1))
    assert risk.size(a_signal(reference="1.10000", stop=None), ACCOUNT, EURUSD) == Decimal(0)


def test_a_zero_distance_stop_means_no_trade() -> None:
    risk = PercentRiskManager(percent=Decimal(1))
    assert risk.size(a_signal(reference="1.10000", stop="1.10000"), ACCOUNT, EURUSD) == Decimal(0)


def test_a_non_positive_percent_is_refused() -> None:
    with pytest.raises(ValueError, match="percent must be positive"):
        PercentRiskManager(percent=Decimal(0))


def test_a_non_positive_lot_step_is_refused() -> None:
    with pytest.raises(ValueError, match="lot step must be positive"):
        PercentRiskManager(percent=Decimal(1), lot_step=Decimal(0))


def test_the_lot_step_floors_the_size() -> None:
    """A raw size of 0.204 lots on a 0.01 step floors to 0.20 — a broker will not fill 0.204,
    and flooring never risks more than the budget."""
    risk = PercentRiskManager(percent=Decimal(1), lot_step=Decimal("0.01"))
    # 1% of 10 200 is $102; a 50-pip stop loses $500/lot ⇒ raw 0.204 ⇒ floored 0.20
    account = AccountState(balance=Decimal("10200"), equity=Decimal("10200"))
    volume = risk.size(a_signal(reference="1.10600", stop="1.10100"), account, EURUSD)
    assert volume == Decimal("0.20")


def test_a_limit_order_is_sized_against_its_limit_not_the_close() -> None:
    """A resting order will fill at its level, so that is the price the stop distance is
    measured from (ADR-0014). Sizing against the close that decided it would size a trade the
    strategy is not placing: here the close is 1.10000 and the limit 1.09500, so the real risk
    is 50 pips ($500/lot) and 1% of $10 000 buys 0.20 lots — not the 0.10 a 100-pip distance
    would give. Every structure setup enters at a level away from the close, so this is the
    normal case for them, not an edge one."""
    risk = PercentRiskManager(percent=Decimal(1))
    signal = Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10000"),
        stop_loss=Decimal("1.09000"),
        limit_price=Decimal("1.09500"),
        client_id="zone-1",
    )
    assert risk.size(signal, ACCOUNT, EURUSD) == Decimal("0.2")
    # and the market version of the same signal still measures from the close
    assert risk.size(a_signal(reference="1.10000", stop="1.09000"), ACCOUNT, EURUSD) == Decimal(
        "0.1"
    )


def test_a_stop_order_is_sized_against_its_trigger_not_the_close() -> None:
    """The mirror of the limit case for a breakout (ADR-0016): a buy stop fills at its trigger, so
    that is the price the stop distance is measured from. Here the close is 1.10000 and the trigger
    1.10500 with the loss at 1.10000 — 50 pips ($500/lot), so 1% of $10 000 buys 0.20 lots.
    Measured from the close the distance is zero and there would be no trade at all, which is
    exactly the silent mis-sizing this guards against."""
    risk = PercentRiskManager(percent=Decimal(1))
    signal = Signal(
        kind=SignalKind.ENTRY,
        side=Side.LONG,
        reference_price=Decimal("1.10000"),
        stop_loss=Decimal("1.10000"),
        stop_price=Decimal("1.10500"),
        client_id="zone-1",
    )
    assert risk.size(signal, ACCOUNT, EURUSD) == Decimal("0.2")


def test_allow_is_the_veto_and_is_open_in_phase_1() -> None:
    risk = PercentRiskManager(percent=Decimal(1))
    order = OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(1),
        decided_at=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
    )
    assert risk.allow(order, ACCOUNT) is True
