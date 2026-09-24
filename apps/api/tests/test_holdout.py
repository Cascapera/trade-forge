"""Choosing a sweep's best points to test on a reserved window — the arithmetic, no database."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.holdout import (
    Candidate,
    choose,
    floor_of,
    median_of,
    overlaps,
    positive_share,
)
from tradeforge_db.models import BacktestMetrics, SelectionMetric

FLOORS = {"M15": 30, "H4": 0}


def a_candidate(
    order: int,
    *,
    net: str,
    trades: int = 50,
    group: tuple[str, str, str] = ("e1", "M15", "EURUSD"),
    profit_factor: str | None = "1.5",
) -> Candidate:
    return Candidate(
        group=group,
        order=order,
        metrics=BacktestMetrics(
            net_profit=Decimal(net),
            total_trades=trades,
            profit_factor=None if profit_factor is None else Decimal(profit_factor),
            sharpe=None,
            expectancy=None,
        ),
    )


class TestChoose:
    def test_the_best_of_each_group_by_the_metric(self) -> None:
        pool = [
            a_candidate(0, net="10"),
            a_candidate(1, net="30"),
            a_candidate(2, net="20"),
            a_candidate(3, net="5", group=("e1", "H4", "EURUSD"), trades=3),
        ]

        chosen = choose(pool, metric=SelectionMetric.NET_PROFIT, top_n=2, floors=FLOORS)

        # Two from M15 — the two best — and the H4 one, alone in its group; in launch order.
        assert [one.order for one in chosen] == [1, 2, 3]

    def test_never_across_groups(self) -> None:
        """Entries are alternative methods and charts and markets are where the question is asked:
        one top N over them all would test the luckiest corner and call it the sweep's answer."""
        pool = [
            a_candidate(0, net="100", group=("e1", "M15", "EURUSD")),
            a_candidate(1, net="1", group=("e2", "M15", "EURUSD")),
            a_candidate(2, net="1", group=("e1", "M15", "GBPUSD")),
        ]

        chosen = choose(pool, metric=SelectionMetric.NET_PROFIT, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [0, 1, 2]

    def test_a_run_below_its_charts_trade_floor_is_not_ranked(self) -> None:
        pool = [a_candidate(0, net="100", trades=29), a_candidate(1, net="1", trades=30)]

        chosen = choose(pool, metric=SelectionMetric.NET_PROFIT, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [1]

    def test_a_run_that_never_traded_is_not_ranked_even_where_the_floor_is_zero(self) -> None:
        pool = [a_candidate(0, net="0", trades=0, group=("e1", "H4", "EURUSD"))]

        assert choose(pool, metric=SelectionMetric.NET_PROFIT, top_n=1, floors=FLOORS) == []
        assert floor_of("H4", FLOORS) == 1
        assert floor_of("W1", FLOORS) == 1

    def test_a_run_with_no_value_for_the_metric_is_skipped_not_ranked_as_zero(self) -> None:
        """A profit factor with no losing trade is null: as a zero it would sit above every run
        that lost, which is the reverse of what it measured."""
        pool = [
            a_candidate(0, net="5", profit_factor=None),
            a_candidate(1, net="5", profit_factor="0.4"),
        ]

        chosen = choose(pool, metric=SelectionMetric.PROFIT_FACTOR, top_n=2, floors=FLOORS)

        assert [one.order for one in chosen] == [1]

    def test_a_tie_goes_to_launch_order(self) -> None:
        pool = [a_candidate(1, net="7"), a_candidate(0, net="7")]

        chosen = choose(pool, metric=SelectionMetric.NET_PROFIT, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [0]


class TestTheWindow:
    SEARCHED = (dt.datetime(2020, 1, 1, tzinfo=dt.UTC), dt.datetime(2026, 1, 1, tzinfo=dt.UTC))

    @pytest.mark.parametrize(
        ("start", "end", "shares"),
        [
            ((2026, 1, 1), (2026, 9, 1), False),  # starts where the search ended
            ((2019, 1, 1), (2020, 1, 1), False),  # ends where the search began
            ((2025, 12, 31), (2026, 9, 1), True),  # one day inside
            ((2019, 1, 1), (2027, 1, 1), True),  # around it
            ((2022, 1, 1), (2023, 1, 1), True),  # inside it
        ],
    )
    def test_any_shared_bar_disqualifies_it(
        self, start: tuple[int, int, int], end: tuple[int, int, int], shares: bool
    ) -> None:
        window = (dt.datetime(*start, tzinfo=dt.UTC), dt.datetime(*end, tzinfo=dt.UTC))
        assert overlaps(*window, *self.SEARCHED) is shares


class TestSummaries:
    def test_nothing_to_summarise_is_none_never_zero(self) -> None:
        assert median_of([]) is None
        assert positive_share([]) is None

    def test_the_share_counts_strictly_positive(self) -> None:
        values = [Decimal("0.1"), Decimal(0), Decimal("-0.2"), Decimal("0.3")]
        assert positive_share(values) == Decimal("0.5")
        assert median_of(values) == Decimal("0.05")
