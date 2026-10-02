"""The best runs by market — what `GET /best` ranks on, with no database and no HTTP (02/10).

His ask: see, market by market, which setup did best on which chart, grouped so the page is not a
wall of numbers, to pick ideas worth validating later. Nothing here is a validation: a sweep's best
run is the best of many draws over one window, and the page says so on every point it shows.

⚠️ **Windows of different lengths are compared.** One sweep searched fifteen years and another six;
a total in R rewards the longer window for being longer. So the total stands beside R per year,
and the drawdown ratio and the share of positive years — which a longer window barely moves — are
offered too. Which one ranks is his choice on the page.
"""

import datetime as dt
from decimal import Decimal
from enum import StrEnum

from tradeforge_api.holdout import recovery_r
from tradeforge_db.models import BacktestMetrics
from tradeforge_engine.domain import AssetClass

_YEAR = Decimal(str(365.25 * 86_400))


class BestMetric(StrEnum):
    """What "best" means on the page — his four (02/10)."""

    RECOVERY_R = "recovery_r"
    """Net R over the deepest drawdown in R (`holdout.recovery_r`)."""
    NET_R = "net_r"
    NET_R_PER_YEAR = "net_r_per_year"
    """Net R over the years of the run's own window, so windows of other lengths compare."""
    POSITIVE_YEARS = "positive_years"


def years_of(date_from: dt.datetime, date_to: dt.datetime) -> Decimal:
    """The run's window in years of 365.25 days — what R per year divides by."""
    return Decimal(str((date_to - date_from).total_seconds())) / _YEAR


def net_r_per_year(
    metrics: BacktestMetrics, date_from: dt.datetime, date_to: dt.datetime
) -> Decimal | None:
    """Net R a year over the run's window; `None` for a run with no R or no window."""
    years = years_of(date_from, date_to)
    if metrics.net_r is None or years <= 0:
        return None
    return metrics.net_r / years


def score(
    metric: BestMetric, metrics: BacktestMetrics, date_from: dt.datetime, date_to: dt.datetime
) -> Decimal | None:
    """The run's value under `metric`, or `None` when it has none — never ranked as zero."""
    if metric is BestMetric.RECOVERY_R:
        return recovery_r(metrics)
    if metric is BestMetric.NET_R:
        return metrics.net_r
    if metric is BestMetric.NET_R_PER_YEAR:
        return net_r_per_year(metrics, date_from, date_to)
    return metrics.positive_year_share


_MARKET_OF_CLASS = {
    AssetClass.FOREX: "Forex",
    AssetClass.STOCK: "Stocks",
    AssetClass.INDEX: "Indices",
    AssetClass.FUTURE: "Futures",
    AssetClass.CRYPTO: "Crypto",
}


def market_of(path: str | None, asset_class: AssetClass) -> str:
    """The market a symbol is grouped under: the first folder of the broker's own tree, when the
    symbol is in its list, else the asset class.

    ⚠️ **The broker's tree, not the asset class, first.** Gold and silver are collected as forex
    (the five classes have no metal), and grouping by class would bury them among the pairs he asked
    to see apart. The tree says `Metals`, `Cash Indices`, `Cryptocurrency` as the broker does.
    """
    if path:
        root = path.split("\\", 1)[0].strip()
        if root:
            return root
    return _MARKET_OF_CLASS[asset_class]
