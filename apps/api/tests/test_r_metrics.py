"""A run's risk in R — every number worked by hand from the R column."""

import datetime as dt
from decimal import Decimal

from tradeforge_api.r_metrics import r_metrics
from tradeforge_engine.domain import ClosedTrade, Side


def trade(when: dt.datetime, r: str | None) -> ClosedTrade:
    net = Decimal(0) if r is None else Decimal(r) * 100
    return ClosedTrade(
        symbol="EURUSD",
        side=Side.LONG,
        volume=Decimal("0.1"),
        entry_time=when,
        entry_price=Decimal("1.1"),
        exit_time=when + dt.timedelta(hours=1),
        exit_price=Decimal("1.1"),
        gross_pnl=net,
        costs=Decimal(0),
        net_pnl=net,
        stop_loss=None if r is None else Decimal("1.09"),
        r_multiple=None if r is None else Decimal(r),
    )


def day(n: int, year: int = 2020) -> dt.datetime:
    return dt.datetime(year, 1, 1, tzinfo=dt.UTC) + dt.timedelta(days=n)


def run(*rs: str | None) -> list[ClosedTrade]:
    return [trade(day(n), r) for n, r in enumerate(rs)]


def test_the_drawdown_is_the_deepest_fall_from_the_running_peak() -> None:
    """Cumulative: 2, 3, 1, 0, 2, -1, 3. Peak 3 after trade 2, trough -1 after trade 6 → 4."""
    found = r_metrics(run("2", "1", "-2", "-1", "2", "-3", "4"))

    assert found.net_r == Decimal("3")
    assert found.max_drawdown_r == Decimal("4")


def test_the_peak_starts_at_zero_so_an_opening_loss_is_a_drawdown() -> None:
    found = r_metrics(run("-1", "-1", "3"))

    assert found.max_drawdown_r == Decimal("2")


def test_the_losing_streak_is_counted_and_its_deepest_cost_summed_apart() -> None:
    """Streaks: [-1, -1, -1] = 3 trades, -3R; then [-2.5, -1] = 2 trades, -3.5R. The longest and
    the deepest are different streaks, and each is reported for what it is."""
    found = r_metrics(run("-1", "-1", "-1", "2", "-2.5", "-1", "1"))

    assert found.losing_streak == 3
    assert found.losing_streak_r == Decimal("-3.5")


def test_break_even_ends_a_streak() -> None:
    assert r_metrics(run("-1", "0", "-1")).losing_streak == 1


def test_each_year_is_summed_and_the_share_counts_years_with_a_trade() -> None:
    trades = [
        trade(day(0, 2020), "2"),
        trade(day(5, 2020), "-1"),
        trade(day(0, 2021), "-3"),
        trade(day(0, 2023), "0.5"),
    ]

    found = r_metrics(trades)

    # 2022 had no trade: not a year that lost, not in the share.
    assert found.yearly_r == {2020: Decimal("1"), 2021: Decimal("-3"), 2023: Decimal("0.5")}
    assert found.positive_year_share == Decimal(2) / Decimal(3)


def test_one_year_has_no_share() -> None:
    assert r_metrics(run("1", "1")).positive_year_share is None


def test_the_trades_are_read_in_entry_order_whatever_order_they_come_in() -> None:
    assert r_metrics(list(reversed(run("2", "-3", "1")))).max_drawdown_r == Decimal("3")


def test_a_trade_with_no_stop_is_left_out_of_everything() -> None:
    found = r_metrics(run("-1", None, "-1"))

    assert found.losing_streak == 2
    assert found.net_r == Decimal("-2")


def test_no_trades_is_zero_risk_and_no_share() -> None:
    found = r_metrics([])

    assert (found.net_r, found.max_drawdown_r, found.losing_streak) == (Decimal(0), Decimal(0), 0)
    assert found.losing_streak_r == Decimal(0)
    assert found.yearly_r == {}
    assert found.positive_year_share is None
