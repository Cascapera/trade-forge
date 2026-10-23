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
    """What "best" means on the page — his four (02/10), and six more (05/10)."""

    RECOVERY_R = "recovery_r"
    """Net R over the deepest drawdown in R (`holdout.recovery_r`)."""
    NET_R = "net_r"
    NET_R_PER_YEAR = "net_r_per_year"
    """Net R over the years of the run's own window, so windows of other lengths compare."""
    POSITIVE_YEARS = "positive_years"
    POSITIVE_MONTHS = "positive_months"
    """The share of months with a trade that ended above zero R — only runs recorded from 05/10."""
    RETURN_PCT = "return_pct"
    """The net profit over the starting capital — the account's own return, compounded."""
    CAGR = "cagr"
    """That return a year, compounded; none for a window under a year or an account at zero."""
    PROFIT_FACTOR = "profit_factor"
    """Gross profit over gross loss; unbounded for a run that won and never lost."""
    WIN_RATE = "win_rate"
    SHARPE = "sharpe"
    WORST_YEAR_R = "worst_year_r"
    """The run's worst calendar year in R — the higher, the milder its worst stretch."""


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


def score(  # noqa: PLR0911 — one answer per metric
    metric: BestMetric,
    metrics: BacktestMetrics,
    date_from: dt.datetime,
    date_to: dt.datetime,
    initial_capital: Decimal,
) -> Decimal | None:
    """The run's value under `metric`, or `None` when it has none — never ranked as zero.
    Infinite for a value with no bound (a drawdown ratio with no drawdown, a profit factor with no
    loss), which ranks above every finite one."""
    if metric is BestMetric.RECOVERY_R:
        return recovery_r(metrics)
    if metric is BestMetric.NET_R:
        return metrics.net_r
    if metric is BestMetric.NET_R_PER_YEAR:
        return net_r_per_year(metrics, date_from, date_to)
    if metric is BestMetric.POSITIVE_YEARS:
        return metrics.positive_year_share
    if metric is BestMetric.POSITIVE_MONTHS:
        return metrics.positive_month_share
    if metric is BestMetric.RETURN_PCT:
        return None if initial_capital <= 0 else metrics.net_profit / initial_capital
    if metric is BestMetric.CAGR:
        return metrics.cagr
    if metric is BestMetric.PROFIT_FACTOR:
        return profit_factor(metrics)
    if metric is BestMetric.WIN_RATE:
        return metrics.win_rate
    if metric is BestMetric.SHARPE:
        return metrics.sharpe
    return worst_year_r(metrics)


def profit_factor(metrics: BacktestMetrics) -> Decimal | None:
    """The stored profit factor, or infinite for a run that won and never lost — the engine stores
    none there, and ranking it as missing would put the cleanest record last."""
    if metrics.profit_factor is not None:
        return metrics.profit_factor
    if metrics.gross_profit > 0 and metrics.gross_loss == 0:
        return Decimal("Infinity")
    return None


def worst_year_r(metrics: BacktestMetrics) -> Decimal | None:
    """The worst calendar year in R, or `None` for a run with no year recorded."""
    years = [Decimal(str(one)) for one in (metrics.yearly_r or {}).values() if one is not None]
    return min(years, default=None)


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
