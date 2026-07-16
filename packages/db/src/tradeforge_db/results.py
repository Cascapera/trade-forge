"""Translate a finished engine run into the rows that persist it (sdd.md §5).

The engine speaks in its own frozen dataclasses — `ClosedTrade`, `BacktestMetrics`,
`EquityPoint` — and knows nothing about SQLAlchemy (invariant §5.4: the core is agnostic
to infrastructure, so it cannot import the ORM). This module is the one-way bridge that
lives on the database side, where importing the engine's domain is allowed (ADR-0009) and
importing the ORM is the whole point.

Two properties are deliberate:

* **Pure.** `to_rows` builds ORM instances and touches no `Session`. The part of persistence
  that actually has bugs is the field-by-field mapping — signs, enums, precision — and
  keeping it session-free means it is tested in milliseconds without Postgres. The caller
  (the phase-1 worker, PR-107) owns the transaction: `session.add_all(...)` then `commit()`.
* **Precision-preserving.** Every `Decimal` that lands in JSONB is stringified, never written
  as a JSON number. A JSON number is a float, and the exact-decimal discipline the whole
  engine runs in would be lost the instant a value round-tripped through the database as one.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal

from tradeforge_db.models import BacktestMetrics, ExitReason, Trade
from tradeforge_engine.domain import ClosedTrade, EquityPoint
from tradeforge_engine.metrics import BacktestMetrics as RunMetrics

# Every `ClosedTrade` carries the reason of its *exit* fill, and the engine emits exactly
# three: a protective stop ("sl"), a protective target ("tp") and a strategy-condition close
# ("exit.condition"). An unmapped reason raises rather than defaulting — the same doctrine as
# `build_indicator` refusing an unknown indicator type. A silent default would mislabel the
# training material the phase-3 analysis reads; a loud failure is a one-line fix here plus the
# DB enum. Phase 2's live exits (kill, manual) get added when they can actually occur.
_EXIT_REASONS: dict[str, ExitReason] = {
    "sl": ExitReason.STOP_LOSS,
    "tp": ExitReason.TAKE_PROFIT,
    "exit.condition": ExitReason.CONDITION,
}


def to_rows(
    *,
    trades: Sequence[ClosedTrade],
    metrics: RunMetrics,
    backtest_id: uuid.UUID,
    instrument_id: uuid.UUID,
) -> tuple[BacktestMetrics, list[Trade]]:
    """The finished run as persistable rows: one metrics row, one trade row per round trip.

    `metrics` is the already-computed engine summary (`compute_metrics`), not recomputed here
    — this function only translates. `backtest_id` and `instrument_id` are the foreign keys
    the run belongs to; the engine's `ClosedTrade` carries a symbol, but the database keys on
    the instrument's UUID, which only the caller knows.
    """
    metrics_row = _metrics_row(metrics, backtest_id)
    trade_rows = [_trade_row(trade, backtest_id, instrument_id) for trade in trades]
    return metrics_row, trade_rows


def _trade_row(trade: ClosedTrade, backtest_id: uuid.UUID, instrument_id: uuid.UUID) -> Trade:
    # Every trade here is a *closed* round trip — an open position at the end of a run never
    # enters `RunResult.trades` — so all four exit columns are always present, which is what
    # the `exit_is_all_or_nothing` CHECK on the table demands.
    return Trade(
        backtest_id=backtest_id,
        instrument_id=instrument_id,
        direction=trade.side,
        entry_time=trade.entry_time,
        entry_price=trade.entry_price,
        volume=trade.volume,
        exit_time=trade.exit_time,
        exit_price=trade.exit_price,
        exit_reason=_exit_reason(trade.reason),
        stop_loss=trade.stop_loss,
        take_profit=trade.take_profit,
        gross_pnl=trade.gross_pnl,
        costs=trade.costs,
        net_pnl=trade.net_pnl,
        r_multiple=trade.r_multiple,
        context=_context(trade.context),
    )


def _exit_reason(reason: str) -> ExitReason:
    try:
        return _EXIT_REASONS[reason]
    except KeyError:
        raise ValueError(
            f"no exit reason mapped for {reason!r}; the engine emits {sorted(_EXIT_REASONS)}"
            " — map the new reason here and add it to the DB enum before it can be persisted"
        ) from None


def _context(context: Mapping[str, Decimal | None] | None) -> dict[str, str | None]:
    """The entry indicator snapshot as JSONB: `{id: str(value) | None}`.

    Values are stringified to keep them exact (a JSON number would be a float). `None` values
    survive — a warming-up indicator read `None`, and that is a fact worth storing, not a zero.
    A strategy with no indicators has no snapshot at all (`None`), which becomes `{}`, the
    column's NOT NULL default.
    """
    if context is None:
        return {}
    return {name: (str(value) if value is not None else None) for name, value in context.items()}


def _metrics_row(metrics: RunMetrics, backtest_id: uuid.UUID) -> BacktestMetrics:
    return BacktestMetrics(
        backtest_id=backtest_id,
        net_profit=metrics.net_profit,
        gross_profit=metrics.gross_profit,
        gross_loss=metrics.gross_loss,
        total_trades=metrics.total_trades,
        long_trades=metrics.long_trades,
        short_trades=metrics.short_trades,
        win_rate=metrics.win_rate,
        payoff=metrics.payoff,
        profit_factor=metrics.profit_factor,
        expectancy=metrics.expectancy,
        max_drawdown_abs=metrics.max_drawdown_abs,
        max_drawdown_pct=metrics.max_drawdown_pct,
        # The column is granular to the day (PR-101); a sub-day drawdown maps to 0.
        max_dd_duration_days=metrics.max_drawdown_duration.days,
        sharpe=metrics.sharpe,
        sortino=metrics.sortino,
        cagr=metrics.cagr,
        avg_trade_duration=metrics.avg_trade_duration,
        equity_curve=_equity_curve(metrics.equity_curve),
    )


def _equity_curve(curve: Sequence[EquityPoint]) -> list[dict[str, str]]:
    """The curve as a JSONB array — read whole, never queried by element. Time as ISO-8601,
    equity stringified for the same precision reason as the context above."""
    return [{"time": point.time.isoformat(), "equity": str(point.equity)} for point in curve]


__all__ = ["to_rows"]
