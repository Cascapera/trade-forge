"""Cost models, checked leg by leg.

A cost is money leaving the account, and it leaves on both legs of a trade. Each number here
is small enough to verify against the instrument's tick value by hand — because a cost model
that is subtly wrong does not crash, it just quietly reports a strategy as profitable that is
not, or unprofitable that is.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.costs import CommissionCostModel, NoCostModel, SpreadCostModel
from tradeforge_engine.domain import OrderRequest, Side, SignalKind
from tradeforge_engine.testing import AAPL, EURUSD

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def an_order(volume: str = "1") -> OrderRequest:
    return OrderRequest(
        symbol="EURUSD",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(volume),
        decided_at=T0,
    )


def test_spread_charges_half_on_each_leg() -> None:
    """A 10-point spread on EURUSD (tick_value 1): half is 5 points = $5 per lot, entry and
    exit alike, so the round trip pays the whole $10."""
    model = SpreadCostModel(spread_points=Decimal(10))
    entry = model.entry_cost(an_order(), EURUSD, Decimal("1.10000"))
    exit_ = model.exit_cost(an_order(), EURUSD, Decimal("1.10500"))
    assert entry == Decimal("5")
    assert exit_ == Decimal("5")


def test_spread_scales_with_volume() -> None:
    model = SpreadCostModel(spread_points=Decimal(10))
    assert model.entry_cost(an_order("0.25"), EURUSD, Decimal("1.1")) == Decimal("1.25")


def test_commission_is_per_unit_on_each_leg() -> None:
    """AAPL at $0.005 per share, 100 shares: $0.50 in, $0.50 out."""
    model = CommissionCostModel(commission_per_unit=Decimal("0.005"))
    order = OrderRequest(
        symbol="AAPL",
        side=Side.LONG,
        intent=SignalKind.ENTRY,
        volume=Decimal(100),
        decided_at=T0,
    )
    assert model.entry_cost(order, AAPL, Decimal("190.00")) == Decimal("0.500")
    assert model.exit_cost(order, AAPL, Decimal("195.00")) == Decimal("0.500")


def test_no_cost_model_charges_nothing() -> None:
    model = NoCostModel()
    assert model.entry_cost(an_order(), EURUSD, Decimal("1.1")) == Decimal(0)
    assert model.exit_cost(an_order(), EURUSD, Decimal("1.1")) == Decimal(0)


def test_a_negative_spread_is_refused() -> None:
    """A negative cost is money appearing from nowhere; it would read as free edge."""
    with pytest.raises(ValueError, match="magnitude"):
        SpreadCostModel(spread_points=Decimal(-1))


def test_a_negative_commission_is_refused() -> None:
    with pytest.raises(ValueError, match="magnitude"):
        CommissionCostModel(commission_per_unit=Decimal("-0.01"))
