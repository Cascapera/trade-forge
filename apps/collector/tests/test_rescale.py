"""Telling a broker that rewrote a series' past from one that did not (14/10, XP's `WIN$`)."""

import datetime as dt
from dataclasses import replace
from decimal import Decimal

from tradeforge_collector.rescale import ENOUGH, TOLERANCE, compare
from tradeforge_engine.domain import Candle

START = dt.datetime(2026, 10, 9, 12, tzinfo=dt.UTC)


def bars(closes: list[Decimal]) -> list[Candle]:
    return [
        Candle(
            time=START + dt.timedelta(minutes=5 * n),
            open=close,
            high=close,
            low=close,
            close=close,
            tick_volume=1,
            spread=0,
        )
        for n, close in enumerate(closes)
    ]


STORED = bars([Decimal(208_000 + 37 * n) for n in range(60)])


def scaled(candles: list[Candle], factor: Decimal) -> list[Candle]:
    return bars([candle.close * factor for candle in candles])


def test_the_same_series_is_not_rescaled() -> None:
    same = compare(STORED, STORED)

    assert same.conclusive
    assert same.ratio == 1.0
    assert not same.differs
    assert not same.rescaled


def test_the_roll_of_14_10_is_a_rescale() -> None:
    """XP moved the whole of `WIN$` up 2.1 % when WINV26 expired."""
    roll = compare(STORED, scaled(STORED, Decimal("1.0213")))

    assert roll.rescaled
    assert abs(roll.ratio - 1.0213) < 1e-9


def test_a_dividend_sized_adjustment_is_a_rescale_too() -> None:
    assert compare(STORED, scaled(STORED, Decimal("0.995"))).rescaled


def test_a_difference_below_the_tolerance_is_the_same_series() -> None:
    assert not compare(STORED, scaled(STORED, Decimal(1 + TOLERANCE / 2))).differs


def with_close(candle: Candle, close: Decimal) -> Candle:
    return replace(candle, open=close, high=close, low=close, close=close)


def test_the_newest_bar_on_disk_is_left_out_it_may_have_been_stored_forming() -> None:
    """Until 14/10 a collection could write the bar still forming; its close is not final."""
    forming = [*STORED[:-1], with_close(STORED[-1], STORED[-1].close * 2)]

    seen = compare(forming, STORED)

    assert seen.matched == len(STORED) - 1
    assert not seen.differs


def test_one_bar_the_broker_revised_does_not_pass_for_a_rescale() -> None:
    revised = [*STORED]
    revised[10] = with_close(STORED[10], STORED[10].close * Decimal("1.05"))

    assert not compare(STORED, revised).differs


def test_bars_that_differ_each_by_its_own_amount_are_not_one_factor() -> None:
    """Another feed, or a series rebuilt from another start: not a rescale, and collecting every
    year again would not mend it."""
    # 2 % away on the whole, as a roll would be, but each bar by its own amount: 0 to 6 %.
    wandering = bars([candle.close * Decimal(1 + n % 7 / 100) for n, candle in enumerate(STORED)])

    seen = compare(STORED, wandering)

    assert seen.differs
    assert not seen.rescaled


def test_too_few_shared_bars_say_nothing() -> None:
    few = compare(STORED[: ENOUGH - 1], scaled(STORED[: ENOUGH - 1], Decimal("1.0213")))
    none = compare(STORED, bars([]))

    assert not few.conclusive
    assert not few.rescaled
    assert none.matched == 0
    assert not none.rescaled
