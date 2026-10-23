"""The best runs by market — the arithmetic, no database (02/10)."""

import datetime as dt
from decimal import Decimal

import pytest

from tradeforge_api.best import BestMetric, market_of, net_r_per_year, score, years_of
from tradeforge_api.holdout import Candidate, HoldoutRank, choose
from tradeforge_db.models import BacktestMetrics
from tradeforge_engine.domain import AssetClass

START = dt.datetime(2019, 1, 1, tzinfo=dt.UTC)
CAPITAL = Decimal(10_000)


def metrics(
    net_r: str | None, drawdown: str | None = "5", share: str | None = "0.5"
) -> BacktestMetrics:
    return BacktestMetrics(
        total_trades=40,
        net_profit=Decimal(0),
        gross_profit=Decimal(0),
        gross_loss=Decimal(0),
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

        assert score(BestMetric.NET_R, one, START, end, CAPITAL) == Decimal(20)
        assert score(BestMetric.RECOVERY_R, one, START, end, CAPITAL) == Decimal(5)
        assert score(BestMetric.POSITIVE_YEARS, one, START, end, CAPITAL) == Decimal("0.75")
        per_year = score(BestMetric.NET_R_PER_YEAR, one, START, end, CAPITAL)
        assert per_year is not None
        assert per_year == pytest.approx(Decimal(5), rel=Decimal("0.001"))

    def test_a_run_with_no_measure_has_no_value_never_zero(self) -> None:
        none = metrics(None, drawdown=None, share=None)
        end = START.replace(year=2023)

        # The return in money is always recorded — every run has a net profit, zero or not.
        for metric in set(BestMetric) - {BestMetric.RETURN_PCT}:
            assert score(metric, none, START, end, CAPITAL) is None, metric

    def test_the_account_measures_read_the_stored_metrics(self) -> None:
        """05/10: return, CAGR, profit factor, win rate, Sharpe, worst year."""
        end = START.replace(year=2023)
        one = BacktestMetrics(
            total_trades=40,
            net_profit=Decimal(2_500),
            gross_profit=Decimal(5_000),
            gross_loss=Decimal(-2_500),
            profit_factor=Decimal(2),
            win_rate=Decimal("0.45"),
            sharpe=Decimal("1.2"),
            cagr=Decimal("0.06"),
            yearly_r={"2019": "4.5", "2020": "-3.25", "2021": "1"},
        )

        assert score(BestMetric.RETURN_PCT, one, START, end, CAPITAL) == Decimal("0.25")
        assert score(BestMetric.CAGR, one, START, end, CAPITAL) == Decimal("0.06")
        assert score(BestMetric.PROFIT_FACTOR, one, START, end, CAPITAL) == Decimal(2)
        assert score(BestMetric.WIN_RATE, one, START, end, CAPITAL) == Decimal("0.45")
        assert score(BestMetric.SHARPE, one, START, end, CAPITAL) == Decimal("1.2")
        assert score(BestMetric.WORST_YEAR_R, one, START, end, CAPITAL) == Decimal("-3.25")
        assert score(BestMetric.RETURN_PCT, one, START, end, Decimal(0)) is None

    def test_a_run_that_never_lost_has_an_unbounded_profit_factor(self) -> None:
        """The engine stores no profit factor without a loss: ranked as missing, the cleanest
        record would come last."""
        clean = BacktestMetrics(
            total_trades=3, gross_profit=Decimal(300), gross_loss=Decimal(0), profit_factor=None
        )
        flat = BacktestMetrics(
            total_trades=0, gross_profit=Decimal(0), gross_loss=Decimal(0), profit_factor=None
        )
        end = START.replace(year=2023)

        unbounded = score(BestMetric.PROFIT_FACTOR, clean, START, end, CAPITAL)
        assert unbounded is not None
        assert unbounded.is_infinite()
        assert score(BestMetric.PROFIT_FACTOR, flat, START, end, CAPITAL) is None

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
