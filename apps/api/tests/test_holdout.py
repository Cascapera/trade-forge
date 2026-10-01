"""Choosing a sweep's best points to test on a reserved window — the arithmetic, no database."""

import datetime as dt
import uuid
from decimal import Decimal

import pytest

from tradeforge_api.holdout import (
    BEHAVIOUR_FIELDS,
    Bounds,
    Candidate,
    HoldoutRank,
    WindowUse,
    behaviour,
    choose,
    floor_of,
    keys_of,
    median_of,
    overlaps,
    positive_share,
    recovery_r,
    reusing,
)
from tradeforge_db.models import BacktestMetrics

FLOORS = {"M15": 30, "H4": 0}
GROUP = ("e1", "M15", "EURUSD")


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

        chosen = choose(pool, metric=HoldoutRank.NET_PROFIT, top_n=2, floors=FLOORS)

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

        chosen = choose(pool, metric=HoldoutRank.NET_PROFIT, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [0, 1, 2]

    def test_a_run_below_its_charts_trade_floor_is_not_ranked(self) -> None:
        pool = [a_candidate(0, net="100", trades=29), a_candidate(1, net="1", trades=30)]

        chosen = choose(pool, metric=HoldoutRank.NET_PROFIT, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [1]

    def test_a_run_that_never_traded_is_not_ranked_even_where_the_floor_is_zero(self) -> None:
        pool = [a_candidate(0, net="0", trades=0, group=("e1", "H4", "EURUSD"))]

        assert choose(pool, metric=HoldoutRank.NET_PROFIT, top_n=1, floors=FLOORS) == []
        assert floor_of("H4", FLOORS) == 1
        assert floor_of("W1", FLOORS) == 1

    def test_a_run_with_no_value_for_the_metric_is_skipped_not_ranked_as_zero(self) -> None:
        """A profit factor with no losing trade is null: as a zero it would sit above every run
        that lost, which is the reverse of what it measured."""
        pool = [
            a_candidate(0, net="5", profit_factor=None),
            a_candidate(1, net="5", profit_factor="0.4"),
        ]

        chosen = choose(pool, metric=HoldoutRank.PROFIT_FACTOR, top_n=2, floors=FLOORS)

        assert [one.order for one in chosen] == [1]

    def test_a_tie_goes_to_launch_order(self) -> None:
        pool = [a_candidate(1, net="7"), a_candidate(0, net="7")]

        chosen = choose(pool, metric=HoldoutRank.NET_PROFIT, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [0]


def a_record(
    order: int, *, net_r: str, y2021: str, group: tuple[str, str, str] = GROUP
) -> Candidate:
    """A run with a full record in R — what `behaviour` reads."""
    return Candidate(
        group=group,
        order=order,
        metrics=BacktestMetrics(
            net_profit=Decimal(net_r) * 100,
            total_trades=31,
            long_trades=31,
            short_trades=0,
            net_r=Decimal(net_r),
            max_drawdown_r=Decimal("9.9"),
            yearly_r={"2020": "-9.9", "2021": y2021},
        ),
    )


class TestDistinct:
    """28/09: the first real chain chose five points of which three were one run — points that
    differ in a target or a breakeven no trade reached make the very same trades."""

    def test_a_clone_of_a_better_run_gives_its_place_to_the_next_distinct_one(self) -> None:
        pool = [
            a_record(0, net_r="7.7", y2021="17.6"),
            a_record(1, net_r="7.7", y2021="17.6"),  # the same record: a clone of 0
            a_record(2, net_r="4.5", y2021="14.4"),
        ]

        chosen = choose(pool, metric=HoldoutRank.NET_R, top_n=2, floors=FLOORS)

        assert [one.order for one in chosen] == [0, 2]

    def test_asked_not_to_it_keeps_the_clones(self) -> None:
        pool = [a_record(0, net_r="7.7", y2021="17.6"), a_record(1, net_r="7.7", y2021="17.6")]

        chosen = choose(pool, metric=HoldoutRank.NET_R, top_n=2, floors=FLOORS, distinct=False)

        assert [one.order for one in chosen] == [0, 1]

    def test_one_year_apart_is_not_a_clone(self) -> None:
        """Exact, never close: the same total reached through different years is another run."""
        pool = [a_record(0, net_r="7.7", y2021="17.6"), a_record(1, net_r="7.7", y2021="17.5")]

        chosen = choose(pool, metric=HoldoutRank.NET_R, top_n=2, floors=FLOORS)

        assert [one.order for one in chosen] == [0, 1]

    def test_the_same_record_in_another_group_is_not_a_clone(self) -> None:
        other = ("e1", "M15", "GBPUSD")
        pool = [
            a_record(0, net_r="7.7", y2021="17.6"),
            a_record(1, net_r="7.7", y2021="17.6", group=other),
        ]

        chosen = choose(pool, metric=HoldoutRank.NET_R, top_n=1, floors=FLOORS)

        assert [one.order for one in chosen] == [0, 1]

    def test_the_years_are_read_in_any_order(self) -> None:
        first = BacktestMetrics(total_trades=3, yearly_r={"2020": "1", "2021": "2"})
        second = BacktestMetrics(total_trades=3, yearly_r={"2021": "2", "2020": "1"})

        assert behaviour(first) == behaviour(second)

    def test_every_field_it_reads_is_a_column_and_tells_runs_apart(self) -> None:
        """01/10: the ranked page groups clones in SQL on `BEHAVIOUR_FIELDS`, so every name must be
        a column of the metrics, and a change in any one of them must make another run."""
        columns = set(BacktestMetrics.__table__.columns.keys())
        assert set(BEHAVIOUR_FIELDS) <= columns
        base = a_record(0, net_r="7.7", y2021="17.6").metrics
        for name in BEHAVIOUR_FIELDS:
            changed = BacktestMetrics(
                **{field: getattr(base, field) for field in BEHAVIOUR_FIELDS},
            )
            setattr(
                changed,
                name,
                {"2020": "0"} if name == "yearly_r" else getattr(base, name) + 1,
            )
            assert behaviour(changed) != behaviour(base), name


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


def a_use(
    start: int,
    end: int,
    *,
    walk: uuid.UUID | None = None,
    fold: int | None = None,
    launched: int = 1,
) -> WindowUse:
    return WindowUse(
        test_id=None if walk is not None else uuid.uuid4(),
        walk_forward_id=walk,
        fold=fold,
        date_from=dt.datetime(start, 1, 1, tzinfo=dt.UTC),
        date_to=dt.datetime(end, 1, 1, tzinfo=dt.UTC),
        created_at=dt.datetime(2026, 10, launched, tzinfo=dt.UTC),
    )


class TestTheWindowIsUsedOnce:
    """01/10: the earlier looks a new test of the same sweep would repeat."""

    def test_only_the_uses_sharing_a_bar_oldest_first(self) -> None:
        later, touching, earlier = (
            a_use(2025, 2026, launched=3),
            a_use(2024, 2025),
            a_use(2025, 2027),
        )
        found = reusing(
            dt.datetime(2025, 6, 1, tzinfo=dt.UTC),
            dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
            [later, touching, earlier],
        )
        assert found == [earlier, later]

    def test_a_walk_forward_is_one_earlier_test_however_many_folds_overlap(self) -> None:
        walk = uuid.uuid4()
        alone = a_use(2025, 2026)
        uses = [a_use(2024, 2025, walk=walk, fold=0), alone, a_use(2025, 2026, walk=walk, fold=1)]
        assert keys_of(uses) == [str(walk), str(alone.key)]

    def test_a_use_is_a_test_or_a_fold(self) -> None:
        with pytest.raises(ValueError, match="a use is a test or a fold"):
            _ = WindowUse(
                test_id=None,
                walk_forward_id=None,
                fold=None,
                date_from=dt.datetime(2025, 1, 1, tzinfo=dt.UTC),
                date_to=dt.datetime(2026, 1, 1, tzinfo=dt.UTC),
                created_at=dt.datetime(2026, 10, 1, tzinfo=dt.UTC),
            ).key


class TestSummaries:
    def test_nothing_to_summarise_is_none_never_zero(self) -> None:
        assert median_of([]) is None
        assert positive_share([]) is None

    def test_the_share_counts_strictly_positive(self) -> None:
        values = [Decimal("0.1"), Decimal(0), Decimal("-0.2"), Decimal("0.3")]
        assert positive_share(values) == Decimal("0.5")
        assert median_of(values) == Decimal("0.05")


def in_r(
    net_r: str | None, drawdown_r: str | None, years: str | None = None, order: int = 0
) -> Candidate:
    """A candidate carrying its risk in R (25/09); `None` is a run recorded before it existed."""
    return Candidate(
        group=("e1", "M15", "EURUSD"),
        order=order,
        metrics=BacktestMetrics(
            net_profit=Decimal(1),
            total_trades=50,
            net_r=None if net_r is None else Decimal(net_r),
            max_drawdown_r=None if drawdown_r is None else Decimal(drawdown_r),
            positive_year_share=None if years is None else Decimal(years),
        ),
    )


class TestRecoveryR:
    def test_net_r_over_the_deepest_drawdown(self) -> None:
        assert recovery_r(in_r("12", "4").metrics) == Decimal(3)

    def test_a_gain_with_no_drawdown_is_the_best_there_is(self) -> None:
        # The profit factor's no-loss case, the same way: something for nothing ranks first.
        assert recovery_r(in_r("2", "0").metrics) == Decimal("Infinity")

    def test_nothing_made_and_no_drawdown_has_nothing_to_say(self) -> None:
        assert recovery_r(in_r("0", "0").metrics) is None

    def test_a_run_recorded_before_r_has_no_score(self) -> None:
        assert recovery_r(in_r(None, None).metrics) is None


class TestChoosingByRiskInR:
    def test_recovery_ranks_the_steady_run_above_the_bigger_but_deeper_one(self) -> None:
        """+20 R through a 25 R hole against +12 R through a 4 R one: 0.8 against 3."""
        chosen = choose(
            [in_r("20", "25", order=0), in_r("12", "4", order=1)],
            metric=HoldoutRank.RECOVERY_R,
            top_n=1,
            floors=FLOORS,
        )

        assert [one.order for one in chosen] == [1]

    def test_the_bounds_filter_before_ranking(self) -> None:
        pool = [
            in_r("30", "20", years="0.8", order=0),  # too deep
            in_r("10", "5", years="0.4", order=1),  # too few positive years
            in_r("8", "5", years="0.6", order=2),
            in_r(None, None, order=3),  # no risk in R: never passes a bound
        ]

        chosen = choose(
            pool,
            metric=HoldoutRank.NET_PROFIT,
            top_n=5,
            floors=FLOORS,
            bounds=Bounds(max_drawdown_r=Decimal(10), min_positive_year_share=Decimal("0.5")),
        )

        assert [one.order for one in chosen] == [2]

    def test_no_bounds_admit_a_run_with_no_risk_in_r(self) -> None:
        chosen = choose(
            [in_r(None, None)],
            metric=HoldoutRank.NET_PROFIT,
            top_n=1,
            floors=FLOORS,
            bounds=Bounds(),
        )

        assert len(chosen) == 1
