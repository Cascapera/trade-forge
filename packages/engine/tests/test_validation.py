"""What the domain refuses at construction.

None of these raise on their own. A `tick_size` of zero divides by zero at the bottom of the
P&L; a negative cost is money appearing in the balance out of nowhere; a naive datetime is a
backtest quietly displaced by the broker's timezone. Each of them produces a *plausible*
result, which is the only failure mode this system genuinely fears.

The `instruments` table has had CHECK constraints since PR-101. The engine — which is where
the arithmetic actually happens — now has the same ones.
"""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.domain import (
    AssetClass,
    Candle,
    Fill,
    InstrumentSpec,
    OrderRequest,
    Side,
    SignalKind,
)

T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


def an_instrument(**overrides: object) -> InstrumentSpec:
    values: dict[str, object] = {
        "symbol": "EURUSD",
        "name": "Euro vs US Dollar",
        "asset_class": AssetClass.FOREX,
        "currency_quote": "USD",
        "tick_size": Decimal("0.00001"),
        "tick_value": Decimal("1"),
        "contract_size": Decimal("100000"),
        "digits": 5,
    }
    values.update(overrides)
    return InstrumentSpec(**values)  # type: ignore[arg-type]


def an_order(**overrides: object) -> OrderRequest:
    values: dict[str, object] = {
        "symbol": "EURUSD",
        "side": Side.LONG,
        "intent": SignalKind.ENTRY,
        "volume": Decimal(1),
        "decided_at": T0,
    }
    values.update(overrides)
    return OrderRequest(**values)  # type: ignore[arg-type]


def test_a_tick_size_of_zero_is_refused() -> None:
    """It divides by zero at the bottom of every P&L in the system."""
    with pytest.raises(ValueError, match="tick_size must be positive"):
        an_instrument(tick_size=Decimal(0))


def test_a_negative_tick_value_is_refused() -> None:
    """It would flip the sign of every trade in the instrument. Silently."""
    with pytest.raises(ValueError, match="tick_value must be positive"):
        an_instrument(tick_value=Decimal(-1))


def test_a_zero_contract_size_is_refused() -> None:
    with pytest.raises(ValueError, match="contract_size must be positive"):
        an_instrument(contract_size=Decimal(0))


def test_an_order_for_no_volume_is_refused() -> None:
    with pytest.raises(ValueError, match="volume must be positive"):
        an_order(volume=Decimal(0))


def test_an_order_for_a_negative_volume_is_refused() -> None:
    """A position of minus three lots is not a short. It is a bug that behaves like one."""
    with pytest.raises(ValueError, match="volume must be positive"):
        an_order(volume=Decimal(-3))


def test_a_negative_cost_is_refused() -> None:
    """Costs are a magnitude. A negative one is free money — and it looks like a good strategy."""
    with pytest.raises(ValueError, match="cannot be negative"):
        Fill(
            order=an_order(),
            time=T0 + dt.timedelta(hours=1),
            price=Decimal("1.10000"),
            volume=Decimal(1),
            costs=Decimal(-1000),
        )


def test_a_fill_at_a_price_of_zero_is_refused() -> None:
    with pytest.raises(ValueError, match="price must be positive"):
        Fill(
            order=an_order(),
            time=T0,
            price=Decimal(0),
            volume=Decimal(1),
            costs=Decimal(0),
        )


def test_a_naive_candle_is_refused() -> None:
    """ "2024-01-01 09:00" is not an instant until you say where.

    Worse than wrong: a run whose candles are naive and whose fills are aware raises a
    `TypeError` on the first comparison — ten years into a backtest.
    """
    with pytest.raises(ValueError, match="must be timezone-aware"):
        Candle(
            time=dt.datetime(2024, 1, 1),  # noqa: DTZ001 — the mistake under test
            open=Decimal("1.10000"),
            high=Decimal("1.10100"),
            low=Decimal("1.09900"),
            close=Decimal("1.10050"),
        )


def test_a_naive_decision_time_is_refused() -> None:
    with pytest.raises(ValueError, match="must be timezone-aware"):
        an_order(decided_at=dt.datetime(2024, 1, 1))  # noqa: DTZ001


def test_a_candle_whose_high_is_below_its_body_is_refused() -> None:
    """A bar at which a stop would trigger where price never went."""
    with pytest.raises(ValueError, match="does not contain its own body"):
        Candle(
            time=T0,
            open=Decimal("1.10000"),
            high=Decimal("1.09000"),  # below the open
            low=Decimal("1.08000"),
            close=Decimal("1.08500"),
        )


def test_a_candle_whose_low_is_above_its_body_is_refused() -> None:
    with pytest.raises(ValueError, match="does not contain its own body"):
        Candle(
            time=T0,
            open=Decimal("1.10000"),
            high=Decimal("1.11000"),
            low=Decimal("1.10500"),  # above the open
            close=Decimal("1.10800"),
        )


def test_a_doji_is_a_perfectly_good_candle() -> None:
    """Open == high == low == close. It happens, and the check must not reject it."""
    flat = Candle(
        time=T0,
        open=Decimal("1.10000"),
        high=Decimal("1.10000"),
        low=Decimal("1.10000"),
        close=Decimal("1.10000"),
    )

    assert flat.close == Decimal("1.10000")
