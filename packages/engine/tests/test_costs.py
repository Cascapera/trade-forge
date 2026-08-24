"""Cost models, checked leg by leg.

A cost is money leaving the account, and it leaves on both legs of a trade. Each number here
is small enough to verify against the instrument's tick value by hand — because a cost model
that is subtly wrong does not crash, it just quietly reports a strategy as profitable that is
not, or unprofitable that is.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.costs import (
    BarSpreadCostModel,
    CommissionCostModel,
    NoCostModel,
    SpreadCostModel,
)
from tradeforge_engine.domain import OrderRequest, Side, SignalKind
from tradeforge_engine.testing import AAPL, EURUSD, bar

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)

# ⚠️ 40, not 10. The models that ignore the bar are checked against a spread that is *not*
# the one they were configured with, so a model that quietly started reading the bar would
# return 20 where the test demands 5. A fixture carrying the configured value would agree
# with both implementations and prove neither.
A_BAR = bar(0, open_="1.10000", close="1.10100", spread=40)


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
    entry = model.entry_cost(an_order(), EURUSD, Decimal("1.10000"), A_BAR)
    exit_ = model.exit_cost(an_order(), EURUSD, Decimal("1.10500"), A_BAR)
    assert entry == Decimal("5")
    assert exit_ == Decimal("5")


def test_spread_scales_with_volume() -> None:
    model = SpreadCostModel(spread_points=Decimal(10))
    assert model.entry_cost(an_order("0.25"), EURUSD, Decimal("1.1"), A_BAR) == Decimal("1.25")


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
    assert model.entry_cost(order, AAPL, Decimal("190.00"), A_BAR) == Decimal("0.500")
    assert model.exit_cost(order, AAPL, Decimal("195.00"), A_BAR) == Decimal("0.500")


def test_no_cost_model_charges_nothing() -> None:
    model = NoCostModel()
    assert model.entry_cost(an_order(), EURUSD, Decimal("1.1"), A_BAR) == Decimal(0)
    assert model.exit_cost(an_order(), EURUSD, Decimal("1.1"), A_BAR) == Decimal(0)


def test_a_negative_spread_is_refused() -> None:
    """A negative cost is money appearing from nowhere; it would read as free edge."""
    with pytest.raises(ValueError, match="magnitude"):
        SpreadCostModel(spread_points=Decimal(-1))


def test_a_negative_commission_is_refused() -> None:
    with pytest.raises(ValueError, match="magnitude"):
        CommissionCostModel(commission_per_unit=Decimal("-0.01"))


def test_the_bar_spread_model_charges_what_the_bar_quoted() -> None:
    """A bar quoting 40 points on EURUSD (tick_value 1): half is 20 points = $20 per leg."""
    model = BarSpreadCostModel()
    assert model.entry_cost(an_order(), EURUSD, Decimal("1.10000"), A_BAR) == Decimal(20)
    assert model.exit_cost(an_order(), EURUSD, Decimal("1.10000"), A_BAR) == Decimal(20)


def test_the_bar_spread_model_scales_with_volume() -> None:
    model = BarSpreadCostModel()
    assert model.entry_cost(an_order("0.25"), EURUSD, Decimal("1.1"), A_BAR) == Decimal(5)


def test_the_bar_spread_model_follows_the_bar_and_the_fixed_one_does_not() -> None:
    """The separating scenario: two bars quoting different spreads, one order, one instrument.

    Without this, every assertion above is satisfied by a `BarSpreadCostModel` that ignored the
    bar and happened to be configured with 40 — which is exactly the implementation this class
    exists to *not* be. Two bars is the cheapest market that tells them apart: the fixed model
    returns one number twice, the bar model returns two.
    """
    tight = bar(1, open_="1.10000", close="1.10100", spread=10)
    wide = bar(2, open_="1.10100", close="1.10200", spread=80)

    live = BarSpreadCostModel()
    fixed = SpreadCostModel(spread_points=Decimal(10))
    order = an_order()

    assert live.entry_cost(order, EURUSD, Decimal("1.1"), tight) == Decimal(5)
    assert live.entry_cost(order, EURUSD, Decimal("1.1"), wide) == Decimal(40)

    # Same two bars, and the fixed model cannot see the difference — by design.
    assert fixed.entry_cost(order, EURUSD, Decimal("1.1"), tight) == Decimal(5)
    assert fixed.entry_cost(order, EURUSD, Decimal("1.1"), wide) == Decimal(5)


def test_a_bar_with_no_spread_stops_the_session() -> None:
    """`Candle.spread` defaults to 0, so "no spread" and "free execution" have the same shape.

    Charging nothing would be a paper session reporting an edge the live account will not have,
    and it would report it in silence. The refusal names the bar so the operator can go look at
    it, rather than at a P&L that is merely too good.
    """
    silent = bar(3, open_="1.10000", close="1.10100")
    with pytest.raises(ValueError, match="carries no spread"):
        BarSpreadCostModel().entry_cost(an_order(), EURUSD, Decimal("1.1"), silent)


def test_a_bar_with_no_spread_is_free_only_when_that_is_asserted() -> None:
    """The escape hatch, and it has to be asked for by name."""
    silent = bar(3, open_="1.10000", close="1.10100")
    model = BarSpreadCostModel(require_spread=False)
    assert model.entry_cost(an_order(), EURUSD, Decimal("1.1"), silent) == Decimal(0)
    assert model.exit_cost(an_order(), EURUSD, Decimal("1.1"), silent) == Decimal(0)
