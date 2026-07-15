"""The vocabulary: the arithmetic in it, and the immutability of it."""

import dataclasses
import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_engine.domain import Candle, Side
from tradeforge_engine.testing import AAPL, EURUSD


def test_the_side_carries_its_own_sign() -> None:
    """Every P&L formula is written once, for a long, and multiplied by this.

    The alternative — an `if side is LONG` in each of them — is how a codebase ends up
    with a short-selling bug in one function and not in the other.
    """
    assert Side.LONG.sign == 1
    assert Side.SHORT.sign == -1


def test_a_side_knows_what_closes_it() -> None:
    assert Side.LONG.opposite is Side.SHORT
    assert Side.SHORT.opposite is Side.LONG


def test_a_hundred_pips_of_eurusd_on_one_lot_is_a_thousand_dollars() -> None:
    """By hand: 0.01000 / 0.00001 = 1000 ticks, at $1 a tick, on 1 lot."""
    assert EURUSD.money_for(Decimal("0.01000"), Decimal(1)) == Decimal(1000)


def test_the_same_formula_on_a_stock_gives_cents_per_share() -> None:
    """$2.50 on 100 shares is $250. Same line of code; the instrument supplies the rest."""
    assert AAPL.money_for(Decimal("2.50"), Decimal(100)) == Decimal(250)


def test_a_move_against_the_position_is_negative_money() -> None:
    assert EURUSD.money_for(Decimal("-0.00050"), Decimal(1)) == Decimal(-50)


def test_a_half_lot_earns_half_as_much() -> None:
    assert EURUSD.money_for(Decimal("0.01000"), Decimal("0.5")) == Decimal(500)


def test_a_candle_cannot_be_rewritten_after_the_fact() -> None:
    """An indicator that has already read a candle must be reading the same candle forever.

    Mutable domain objects turn "the same input produces the same output" into a hope: any
    holder of a reference can quietly change history, and a determinism test would still
    pass because both runs were corrupted identically.
    """
    candle = Candle(
        time=dt.datetime(2024, 1, 1, tzinfo=dt.UTC),
        open=Decimal("1.10000"),
        high=Decimal("1.10100"),
        low=Decimal("1.09900"),
        close=Decimal("1.10050"),
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        candle.close = Decimal("9.99999")  # type: ignore[misc]
