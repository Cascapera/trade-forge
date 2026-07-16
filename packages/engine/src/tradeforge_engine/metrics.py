"""Backtest metrics (sdd.md §5): what a run *meant*, computed from what it *did*.

A `RunResult` is a ledger — trades and an equity curve. These are the numbers a human reads
to decide whether a strategy is worth trading, and each one hides a way to be fooled:

* **Win rate lies alone.** Ninety percent winners is ruin if the one loser erases twenty
  wins. That is why `payoff` (average win over average loss) and `expectancy` (expected
  result per trade) sit next to it — a strategy is the *product* of how often and how much.
* **Drawdown is peak-to-trough on equity, not balance.** The deepest fall from a high-water
  mark, and how long it lasted. It is what closes an account before the recovery it would
  have had — measured on equity, because an open loss is real money even before it is booked.
* **Sharpe and Sortino divide reward by risk.** Mean return over its volatility; Sortino
  counts only the *downside* volatility, since upside is not what hurts. Both are noise on a
  handful of trades — which is exactly when a backtest is most tempting to believe.
* **Undefined is not zero.** Sharpe over zero trades is undefined, not 0; a profit factor
  with no losses is not a number. Those come back `None`, never a 0 that would rank a
  strategy that never traded alongside one that broke even.

Pure and dependency-free like the rest of the engine, and pinned to `ENGINE_CONTEXT` so the
square roots and divisions land in the same place every run.
"""

import datetime as dt
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext

from tradeforge_engine.domain import ZERO, ClosedTrade, EquityPoint, Money, Side
from tradeforge_engine.loop import ENGINE_CONTEXT

_ONE_YEAR = Decimal("365.25")

# Sharpe needs a spread and CAGR needs a span; both take at least two data points to exist.
_MIN_POINTS = 2


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    """The summary of a finished run. Nullable fields are genuinely undefined, not zero."""

    net_profit: Money
    gross_profit: Money  # sum of winning trades, >= 0
    gross_loss: Money  # sum of losing trades, <= 0

    total_trades: int
    long_trades: int
    short_trades: int

    win_rate: Money  # a fraction in [0, 1]
    payoff: Money | None  # avg win / |avg loss|
    profit_factor: Money | None  # gross profit / |gross loss|
    expectancy: Money | None  # net profit / trade

    max_drawdown_abs: Money
    max_drawdown_pct: Money  # a fraction in [0, 1]
    max_drawdown_duration: dt.timedelta

    sharpe: Money | None
    sortino: Money | None
    cagr: Money | None

    avg_trade_duration: dt.timedelta | None

    equity_curve: tuple[EquityPoint, ...]


def compute_metrics(
    *,
    trades: Sequence[ClosedTrade],
    equity_curve: Sequence[EquityPoint],
    initial_capital: Money,
) -> BacktestMetrics:
    """Fold a run's trades and equity curve into the §5 metrics.

    `initial_capital` is needed for the return series (Sharpe, Sortino) and the growth rate:
    a $200 profit means one thing on a $1 000 account and another on a $1 000 000 one.
    """
    with localcontext(ENGINE_CONTEXT):
        return _compute(trades=trades, equity_curve=equity_curve, initial_capital=initial_capital)


def _compute(
    *,
    trades: Sequence[ClosedTrade],
    equity_curve: Sequence[EquityPoint],
    initial_capital: Money,
) -> BacktestMetrics:
    wins = [t for t in trades if t.net_pnl > ZERO]
    losses = [t for t in trades if t.net_pnl < ZERO]
    total = len(trades)

    gross_profit = sum((t.net_pnl for t in wins), ZERO)
    gross_loss = sum((t.net_pnl for t in losses), ZERO)  # <= 0
    net_profit = gross_profit + gross_loss

    win_rate = Decimal(len(wins)) / total if total else ZERO
    avg_win = gross_profit / len(wins) if wins else None
    avg_loss = gross_loss / len(losses) if losses else None  # <= 0
    payoff = avg_win / -avg_loss if avg_win is not None and avg_loss is not None else None
    profit_factor = gross_profit / -gross_loss if losses else None
    expectancy = net_profit / total if total else None

    dd_abs, dd_pct, dd_duration = _drawdown(equity_curve)
    sharpe, sortino = _risk_adjusted(trades, initial_capital)
    cagr = _cagr(equity_curve, initial_capital)
    avg_duration = _avg_duration(trades)

    return BacktestMetrics(
        net_profit=net_profit,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
        total_trades=total,
        long_trades=sum(1 for t in trades if t.side is Side.LONG),
        short_trades=sum(1 for t in trades if t.side is Side.SHORT),
        win_rate=win_rate,
        payoff=payoff,
        profit_factor=profit_factor,
        expectancy=expectancy,
        max_drawdown_abs=dd_abs,
        max_drawdown_pct=dd_pct,
        max_drawdown_duration=dd_duration,
        sharpe=sharpe,
        sortino=sortino,
        cagr=cagr,
        avg_trade_duration=avg_duration,
        equity_curve=tuple(equity_curve),
    )


def _drawdown(equity_curve: Sequence[EquityPoint]) -> tuple[Money, Money, dt.timedelta]:
    """Deepest peak-to-trough fall on equity, as money and as a fraction, and the longest time
    spent below a prior high-water mark.

    Duration is measured from a peak to the bar that first climbs back above it — the recovery,
    not merely the last bar seen underwater. A drawdown that never recovers runs to the end of
    the series. A stretch that only rose has no duration; the `in_drawdown` flag keeps a fresh
    all-time high from being counted as a zero-depth drawdown.
    """
    peak = None
    peak_time = None
    max_abs = ZERO
    max_pct = ZERO
    max_duration = dt.timedelta(0)
    in_drawdown = False
    for point in equity_curve:
        if peak is None or point.equity >= peak:
            if in_drawdown and peak_time is not None:  # recovered on this bar
                max_duration = max(max_duration, point.time - peak_time)
            peak = point.equity
            peak_time = point.time
            in_drawdown = False
            continue
        in_drawdown = True
        drop = peak - point.equity
        max_abs = max(max_abs, drop)
        if peak > ZERO and drop / peak > max_pct:
            max_pct = drop / peak
        if peak_time is not None:  # still underwater — covers the never-recovered case
            max_duration = max(max_duration, point.time - peak_time)
    return max_abs, max_pct, max_duration


def _risk_adjusted(
    trades: Sequence[ClosedTrade], initial_capital: Money
) -> tuple[Money | None, Money | None]:
    """Sharpe and Sortino over the per-trade return series (`net_pnl / initial_capital`).

    Sample standard deviation (n-1): with fewer than two trades there is no dispersion to
    speak of, so both are `None` rather than a fabricated 0. Sortino divides by the downside
    deviation only — the volatility of the losing trades, since the winning ones are not the
    risk anyone is compensated for.
    """
    if len(trades) < _MIN_POINTS or initial_capital <= ZERO:
        return None, None

    returns = [t.net_pnl / initial_capital for t in trades]
    n = len(returns)
    mean = sum(returns, ZERO) / n

    variance = sum(((r - mean) ** 2 for r in returns), ZERO) / (n - 1)
    stdev = variance.sqrt()
    sharpe = mean / stdev if stdev > ZERO else None

    downside = [r for r in returns if r < ZERO]
    if downside:
        downside_var = sum((r**2 for r in downside), ZERO) / (n - 1)
        downside_dev = downside_var.sqrt()
        sortino = mean / downside_dev if downside_dev > ZERO else None
    else:
        sortino = None

    return sharpe, sortino


def _cagr(equity_curve: Sequence[EquityPoint], initial_capital: Money) -> Money | None:
    """Compound annual growth rate. `None` for a span too short or a non-positive equity —
    annualising a few hours produces an astronomically large, meaningless number, and there
    is no honest growth rate through zero."""
    if len(equity_curve) < _MIN_POINTS or initial_capital <= ZERO:
        return None
    span = equity_curve[-1].time - equity_curve[0].time
    span_days = Decimal(span.total_seconds()) / Decimal(86_400)
    if span_days < _ONE_YEAR:
        return None  # sub-year spans are not annualised; the number would only mislead
    final = equity_curve[-1].equity
    if final <= ZERO:
        return None
    years = span_days / _ONE_YEAR
    # (final / initial) ** (1 / years) - 1, via exp/ln to keep a non-integer exponent exact.
    ratio = final / initial_capital
    return (ratio.ln() / years).exp() - 1


def _avg_duration(trades: Sequence[ClosedTrade]) -> dt.timedelta | None:
    if not trades:
        return None
    total = sum((t.exit_time - t.entry_time for t in trades), dt.timedelta(0))
    return total / len(trades)


__all__ = ["BacktestMetrics", "compute_metrics"]
