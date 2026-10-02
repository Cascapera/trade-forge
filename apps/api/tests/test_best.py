"""The best runs by market — the arithmetic, no database (02/10)."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.best import BestMetric, market_of, net_r_per_year, score, years_of
from tradeforge_api.holdout import Candidate, HoldoutRank, choose
from tradeforge_db.models import BacktestMetrics
from tradeforge_engine.domain import AssetClass

START = dt.datetime(2019, 1, 1, tzinfo=dt.UTC)


def metrics(
    net_r: str | None, drawdown: str | None = "5", share: str | None = "0.5"
) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=40,
        net_r=None if net_r is None else Decimal(net_r),
        max_drawdown_r=None if drawdown is None else Decimal(drawdown),
        positive_year_share=None if share is None else Decimal(share),
    )


class TestScore:
    def test_r_per_year_divides_by_the_runs_own_window(self) -> None:
        """Windows of six and fifteen years compare: the total rewards the longer one."""
        six = START.replace(year=2025)
        fifteen = START.replace(year=2034)

        assert years_of(START, six) == pytest.approx(Decimal(6), rel=Decimal("0.001"))
        short = net_r_per_year(metrics("30"), START, six)
        long = net_r_per_year(metrics("60"), START, fifteen)
        assert short is not None
        assert long is not None
        assert short > long  # 5 R a year beats 4, though 30 R is half of 60

    def test_each_metric_reads_its_own_measure(self) -> None:
        one = metrics("20", drawdown="4", share="0.75")
        end = START.replace(year=2023)

        assert score(BestMetric.NET_R, one, START, end) == Decimal(20)
        assert score(BestMetric.RECOVERY_R, one, START, end) == Decimal(5)
        assert score(BestMetric.POSITIVE_YEARS, one, START, end) == Decimal("0.75")
        per_year = score(BestMetric.NET_R_PER_YEAR, one, START, end)
        assert per_year is not None
        assert per_year == pytest.approx(Decimal(5), rel=Decimal("0.001"))

    def test_a_run_with_no_measure_has_no_value_never_zero(self) -> None:
        none = metrics(None, drawdown=None, share=None)
        end = START.replace(year=2023)

        for metric in BestMetric:
            assert score(metric, none, START, end) is None, metric

    def test_no_window_is_no_r_per_year(self) -> None:
        assert net_r_per_year(metrics("10"), START, START) is None


class TestMarket:
    @pytest.mark.parametrize(
        ("path", "market"),
        [
            ("Forex\\Majors\\EURUSD", "Forex"),
            ("Metals\\GOLD", "Metals"),
            ("Cash Indices\\Usa500", "Cash Indices"),
            ("Cryptocurrency\\BTCUSD", "Cryptocurrency"),
        ],
    )
    def test_the_brokers_tree_names_the_market(self, path: str, market: str) -> None:
        """Gold is collected as forex (no metal among the five classes): the tree keeps it apart."""
        assert market_of(path, AssetClass.FOREX) == market

    def test_without_the_brokers_tree_the_asset_class_does(self) -> None:
        assert market_of(None, AssetClass.INDEX) == "Indices"
        assert market_of("", AssetClass.CRYPTO) == "Crypto"


class TestChooseByScore:
    def test_a_score_ranks_in_place_of_the_metrics_column(self) -> None:
        group = ("e1", "H1", "EURUSD")
        pool = [
            Candidate(group=group, order=0, metrics=metrics("10")),
            Candidate(group=group, order=1, metrics=metrics("20")),
        ]
        # By net R order 1 wins; the score says order 0 (a shorter window, more per year).
        per_year = {0: Decimal(5), 1: Decimal(2)}

        chosen = choose(
            pool,
            metric=HoldoutRank.NET_R,
            top_n=1,
            floors={},
            score=lambda one: per_year[one.order],
        )

        assert [one.order for one in chosen] == [0]
