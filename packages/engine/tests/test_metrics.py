"""Backtest metrics, checked by hand and at the edges where they go undefined.

The spec singles out expectancy and profit factor for hand verification; they are here, next
to the rest of the golden. The other half of the file is the boundaries — zero trades, no
losing trade, a single trade — where the honest answer is `None`, not a fabricated zero that
would rank a strategy that never traded alongside one that broke even.
"""

import datetime as dt
from decimal import Decimal

from tradeforge_engine.domain import ClosedTrade, EquityPoint, Side
from tradeforge_engine.metrics import compute_metrics
from tradeforge_engine.testing import START

HOUR = dt.timedelta(hours=1)


def a_trade(
    *,
    net: str,
    side: Side = Side.LONG,
    entry_hour: int = 0,
    exit_hour: int = 1,
) -> ClosedTrade:
    net_pnl = Decimal(net)
    return ClosedTrade(
        symbol="EURUSD",
        side=side,
        volume=Decimal(1),
        entry_time=START + entry_hour * HOUR,
        entry_price=Decimal("1.10000"),
        exit_time=START + exit_hour * HOUR,
        exit_price=Decimal("1.10000") + net_pnl / 100_000,
        gross_pnl=net_pnl,
        costs=Decimal(0),
        net_pnl=net_pnl,
    )


def a_curve(*equities: str) -> tuple[EquityPoint, ...]:
    return tuple(
        EquityPoint(time=START + index * HOUR, equity=Decimal(value))
        for index, value in enumerate(equities)
    )


def test_expectancy_and_profit_factor_by_hand() -> None:
    """One +$200 win, one -$100 loss (the golden's two trades).

    gross_profit 200, gross_loss -100, net 100. win_rate 1/2 = 0.5.
    payoff = avg_win / |avg_loss| = 200 / 100 = 2. profit_factor = 200 / 100 = 2.
    expectancy = net / trades = 100 / 2 = 50.
    """
    metrics = compute_metrics(
        trades=[a_trade(net="200"), a_trade(net="-100")],
        equity_curve=a_curve("10000", "10200", "10100"),
        initial_capital=Decimal(10_000),
    )
    assert metrics.net_profit == Decimal("100")
    assert metrics.gross_profit == Decimal("200")
    assert metrics.gross_loss == Decimal("-100")
    assert metrics.win_rate == Decimal("0.5")
    assert metrics.payoff == Decimal("2")
    assert metrics.profit_factor == Decimal("2")
    assert metrics.expectancy == Decimal("50")


def test_zero_trades_leaves_every_ratio_undefined() -> None:
    """No trades: win_rate is 0 (no wins), but payoff, profit factor, expectancy, Sharpe,
    Sortino and CAGR are `None` — undefined, not zero."""
    metrics = compute_metrics(
        trades=[], equity_curve=a_curve("10000", "10000"), initial_capital=Decimal(10_000)
    )
    assert metrics.total_trades == 0
    assert metrics.win_rate == Decimal(0)
    assert metrics.payoff is None
    assert metrics.profit_factor is None
    assert metrics.expectancy is None
    assert metrics.sharpe is None
    assert metrics.sortino is None
    assert metrics.avg_trade_duration is None


def test_no_losing_trade_makes_profit_factor_and_sortino_undefined() -> None:
    """A perfect run has no downside to divide by — profit factor and Sortino are `None`, not
    infinity dressed up as a number."""
    metrics = compute_metrics(
        trades=[a_trade(net="200"), a_trade(net="150")],
        equity_curve=a_curve("10000", "10200", "10350"),
        initial_capital=Decimal(10_000),
    )
    assert metrics.gross_loss == Decimal(0)
    assert metrics.profit_factor is None
    assert metrics.sortino is None
    assert metrics.win_rate == Decimal("1")


def test_a_single_trade_has_no_risk_adjusted_ratio() -> None:
    """Sharpe needs dispersion, and one trade has none. `None`, not 0."""
    metrics = compute_metrics(
        trades=[a_trade(net="200")],
        equity_curve=a_curve("10000", "10200"),
        initial_capital=Decimal(10_000),
    )
    assert metrics.sharpe is None
    assert metrics.sortino is None


def test_drawdown_is_peak_to_trough_on_equity() -> None:
    """Rise to 10 500, fall to 10 100, recover. The drawdown is 400 off the 10 500 peak =
    0.0381, and it lasts from the peak until equity climbs back above it."""
    metrics = compute_metrics(
        trades=[],
        equity_curve=a_curve("10000", "10500", "10300", "10100", "10600"),
        initial_capital=Decimal(10_000),
    )
    assert metrics.max_drawdown_abs == Decimal("400")
    assert metrics.max_drawdown_pct == Decimal("400") / Decimal("10500")
    # underwater from the peak at hour 1 to the recovery bar at hour 4 = 3 hours
    assert metrics.max_drawdown_duration == dt.timedelta(hours=3)


def test_cagr_annualises_only_over_a_full_year() -> None:
    """Doubling the account in exactly one year is a CAGR of 100%. A sub-year span is `None` —
    annualising a few hours would report a growth rate in the billions of percent."""
    year = dt.timedelta(days=365, hours=6)  # 365.25 days
    long_curve = (
        EquityPoint(time=START, equity=Decimal(10_000)),
        EquityPoint(time=START + year, equity=Decimal(20_000)),
    )
    metrics = compute_metrics(trades=[], equity_curve=long_curve, initial_capital=Decimal(10_000))
    assert metrics.cagr is not None
    assert abs(metrics.cagr - Decimal(1)) < Decimal("0.0001")

    short = compute_metrics(
        trades=[], equity_curve=a_curve("10000", "20000"), initial_capital=Decimal(10_000)
    )
    assert short.cagr is None


def test_cagr_is_undefined_for_a_single_point_or_a_blown_account() -> None:
    """One equity point has no span; an account that reached zero has no growth rate through
    it. Both `None`, and neither crashes the rest of the metrics."""
    one_point = compute_metrics(
        trades=[], equity_curve=a_curve("10000"), initial_capital=Decimal(10_000)
    )
    assert one_point.cagr is None

    year = dt.timedelta(days=365, hours=6)
    blown = (
        EquityPoint(time=START, equity=Decimal(10_000)),
        EquityPoint(time=START + year, equity=Decimal(0)),
    )
    assert (
        compute_metrics(trades=[], equity_curve=blown, initial_capital=Decimal(10_000)).cagr is None
    )


def test_long_and_short_trades_are_counted_separately() -> None:
    metrics = compute_metrics(
        trades=[a_trade(net="100", side=Side.LONG), a_trade(net="-50", side=Side.SHORT)],
        equity_curve=a_curve("10000", "10100", "10050"),
        initial_capital=Decimal(10_000),
    )
    assert metrics.long_trades == 1
    assert metrics.short_trades == 1
    assert metrics.total_trades == 2
