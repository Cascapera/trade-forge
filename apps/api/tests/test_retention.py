"""His bar for what a sweep's run keeps (2026-09-23): profit above zero, and a floor of trades per
chart — none on H4 and above, 30 on H1 to M15, 60 on M5 and M1.

The edges are what is pinned: a run exactly on the floor keeps its trades, one trade short does
not, and a run that broke even never does."""

from decimal import Decimal

import pytest

from tradeforge_api.retention import MIN_TRADES, recorded_for
from tradeforge_db import Recorded
from tradeforge_schema.models import TIMEFRAMES


def test_every_chart_the_dsl_names_has_a_floor() -> None:
    """A chart added to the DSL without a line here would raise on its first sweep — this says
    so first, in a test, instead of in a failed run."""
    assert set(MIN_TRADES) == set(TIMEFRAMES)


def test_the_floors_are_his() -> None:
    assert MIN_TRADES == {
        "M1": 60,
        "M5": 60,
        "M15": 30,
        "M30": 30,
        "H1": 30,
        "H4": 0,
        "D1": 0,
        "W1": 0,
    }


@pytest.mark.parametrize("timeframe", ["M1", "M5", "M15", "M30", "H1"])
def test_on_the_floor_the_trades_are_kept_and_one_short_they_are_not(timeframe: str) -> None:
    floor = MIN_TRADES[timeframe]

    def kept(trades: int) -> Recorded:
        return recorded_for(
            in_sweep=True, timeframe=timeframe, net_profit=Decimal(1), total_trades=trades
        )

    assert kept(floor) is Recorded.TRADES
    assert kept(floor + 1) is Recorded.TRADES
    assert kept(floor - 1) is Recorded.METRICS


@pytest.mark.parametrize("timeframe", ["H4", "D1", "W1"])
def test_on_the_long_charts_any_profit_keeps_the_trades(timeframe: str) -> None:
    """His reason: a wide window on these charts gives few trades anyway."""
    assert (
        recorded_for(in_sweep=True, timeframe=timeframe, net_profit=Decimal(1), total_trades=1)
        is Recorded.TRADES
    )


@pytest.mark.parametrize("net_profit", ["0", "-0.01", "-1000"])
@pytest.mark.parametrize("timeframe", ["M1", "H1", "D1"])
def test_a_run_that_did_not_make_money_keeps_only_its_metrics(
    net_profit: str, timeframe: str
) -> None:
    """⚠️ Strictly above zero: breaking even is not a run worth reading trade by trade."""
    assert (
        recorded_for(
            in_sweep=True, timeframe=timeframe, net_profit=Decimal(net_profit), total_trades=500
        )
        is Recorded.METRICS
    )


def test_a_run_that_is_not_a_sweeps_keeps_everything_whatever_it_did() -> None:
    """A single backtest, a study, a basket are read run by run: the bar is not theirs."""
    assert (
        recorded_for(in_sweep=False, timeframe="M1", net_profit=Decimal(-5), total_trades=0)
        is Recorded.FULL
    )


def test_a_chart_with_no_floor_is_refused_rather_than_guessed() -> None:
    with pytest.raises(KeyError):
        recorded_for(in_sweep=True, timeframe="M2", net_profit=Decimal(1), total_trades=100)
