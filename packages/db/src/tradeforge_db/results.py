"""Translate an engine run into the rows that persist it (sdd.md §5).

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

**A backtest reports at the end; a live session reports as it goes.** `to_rows` translates a
finished run in one call. `open_trade_row` and `close_trade_values` translate the same domain
objects one trade at a time, because a session that has been holding a position for three days
has something true to say and no ending in which to say it. They live in this module rather
than beside the session so that `_money`, `_context`, `_snapshot` and `_exit_reason` have one
implementation — a rounding rule or an enum mapping that existed twice would agree until the
day one of them was fixed, and the divergence would arrive as a paper trade that does not match
the backtest it was supposed to reproduce.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from tradeforge_db.models import BacktestMetrics, ExitReason, Trade
from tradeforge_engine.domain import ClosedTrade, EntrySnapshot, EquityPoint, Position
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

# The scale of `MONEY` (`base.MONEY` is `NUMERIC(20, 8)`). Kept as a quantum rather than read
# from the column so the rounding is visible where it happens; `test_results.py` pins the two
# together, because a widened column with this constant left behind would silently go back to
# letting Postgres do the rounding.
_MONEY_QUANTUM = Decimal(1).scaleb(-8)


def _money(value: Decimal) -> Decimal:
    """A money amount at the exact scale the column stores it.

    The engine computes in unbounded `Decimal` — a `gross_pnl` carries ten decimal places and
    a `net_pnl` twelve, because both come from a chain of price, tick value and volume. Postgres
    rounds each of those to eight **independently** on the way in, and independent rounding does
    not distribute over addition: `round(a) + round(b)` differs from `round(a + b)` whenever the
    digits past the eighth carry. That is a millionth of a cent of arithmetic, and it is enough
    to make the row contradict itself.

    Two CHECK constraints state an identity between money columns — `net_profit_balances` and
    `net_pnl_balances` — and both reject a row where that happens. It is *intermittent*: only
    the minority of runs whose tails happen to carry hit it, which is why every backtest before
    the AUDCAD acceptance run passed. Rounding here, and deriving each total from the already
    rounded parts, makes the identity hold by construction: two eight-place numbers add to an
    eight-place number, so Postgres stores the total verbatim and has nothing left to round.

    The error lands on the total, never on the parts, and that is the deliberate half. Either
    way some column is off by 1e-8 from the unbounded arithmetic; the one kept true is the one
    the constraint — and every reader of the row — actually relies on. A row whose stated parts
    do not add up to its stated total lies about itself; a total that is a hundred-millionth of
    a currency unit from the ideal does not.
    """
    return value.quantize(_MONEY_QUANTUM)


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
    trade_rows = [_trade_row(trade, instrument_id, backtest_id=backtest_id) for trade in trades]
    return metrics_row, trade_rows


def closed_trade_row(
    trade: ClosedTrade, live_session_id: uuid.UUID, instrument_id: uuid.UUID
) -> Trade:
    """A live trade that **opened and closed without a bar in between**, as one finished row.

    The ordinary live path is two writes: `open_trade_row` at the fill and `close_trade_values`
    when it ends. A round trip that both starts and finishes inside a single bar never has a
    moment where anybody could have observed it open, so there is nothing for the first write to
    say — and inserting an open row only to update it in the same transaction would be inventing
    a state the market never had.

    ⚠️ Not a shortcut worth taking for *every* live trade. Writing only at the close is exactly
    the design `specs/fase-3.md` rejected: a session holding a position for three days would
    show nothing, which is indistinguishable from a session that never traded.
    """
    return _trade_row(trade, instrument_id, live_session_id=live_session_id)


def _trade_row(
    trade: ClosedTrade,
    instrument_id: uuid.UUID,
    *,
    backtest_id: uuid.UUID | None = None,
    live_session_id: uuid.UUID | None = None,
) -> Trade:
    # ⚠️ Exactly one parent, checked here as well as by `trade_has_one_parent` on the table. The
    # database would catch it, but only at flush — by then the caller is a bar into a live
    # session and the error names a constraint rather than the call that built the row.
    if (backtest_id is None) == (live_session_id is None):
        raise ValueError("a trade belongs to exactly one of a backtest or a live session")
    # Every trade here is a *closed* round trip — an open position at the end of a run never
    # enters `RunResult.trades` — so all four exit columns are always present, which is what
    # the `exit_is_all_or_nothing` CHECK on the table demands.
    #
    # `net_pnl` is derived from the two rounded legs rather than rounded itself, so the row
    # satisfies `net_pnl_balances` by construction. See `_money`.
    gross_pnl = _money(trade.gross_pnl)
    costs = _money(trade.costs)
    return Trade(
        backtest_id=backtest_id,
        live_session_id=live_session_id,
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
        gross_pnl=gross_pnl,
        costs=costs,
        net_pnl=gross_pnl - costs,
        r_multiple=trade.r_multiple,
        context=_context(trade.context),
        snapshot=_snapshot(trade.snapshot),
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


def _snapshot(snapshot: EntrySnapshot | None) -> dict[str, Any]:
    """The entry's picture as JSONB: the bars around it, and the rectangles drawn over them.

    A nested object where `context` is a flat map, because this is a time series and that is a
    set of named scalars — see the `snapshot` column on `Trade`. Prices are stringified for the
    same reason as everywhere else here: a JSON number is a float, and a chart drawn from floats
    would disagree in the last place with the trade printed beside it.

    `filled_at` is written out even though it is the last bar's time. It is what the chart marks
    the entry on, and a reader of this column should not have to know that the list is ordered
    to find it. It cannot drift — the engine derives it from the same tuple.

    A trade whose engine recorded no window becomes `{}`, the column's NOT NULL default, which
    is the same answer `_context` gives and means "nothing was recorded", not "an empty chart".
    """
    if snapshot is None:
        return {}
    return {
        "decided_at": snapshot.decided_at.isoformat(),
        "filled_at": snapshot.filled_at.isoformat(),
        "bars": [
            {
                "time": bar.time.isoformat(),
                "open": str(bar.open),
                "high": str(bar.high),
                "low": str(bar.low),
                "close": str(bar.close),
            }
            for bar in snapshot.bars
        ],
        "regions": [
            {
                "label": region.label,
                "top": str(region.top),
                "bottom": str(region.bottom),
                "from_time": region.from_time.isoformat(),
            }
            for region in snapshot.regions
        ],
        # Points as `[time, value]` pairs rather than `{"time": ..., "value": ...}`: a fifty
        # point curve repeats those two keys fifty times for nothing, and unlike the bars —
        # which have five fields and are read by a human debugging a chart — a point has two
        # and is only ever consumed by the drawing code. The order is the same as the tuple.
        "series": [
            {
                "label": series.label,
                "points": [[point.time.isoformat(), str(point.value)] for point in series.points],
            }
            for series in snapshot.series
        ],
        # Bounded at both ends, unlike a region: a broken level stops being structure the moment
        # it is crossed, and `to_time` is where. See `SnapshotLevel`.
        "levels": [
            {
                "label": level.label,
                "price": str(level.price),
                "from_time": level.from_time.isoformat(),
                "to_time": level.to_time.isoformat(),
            }
            for level in snapshot.levels
        ],
    }


def _metrics_row(metrics: RunMetrics, backtest_id: uuid.UUID) -> BacktestMetrics:
    # `net_profit` is derived from the two rounded halves rather than rounded itself, so the
    # row satisfies `net_profit_balances` by construction. See `_money`.
    #
    # Only these three of the row's money columns are rounded here, because only these three
    # are bound by an identity the database enforces. Rounding `expectancy` or
    # `max_drawdown_abs` too would change nothing observable — Postgres rounds them to the
    # same value on the way in — so it would be ceremony that reads like a rule.
    gross_profit = _money(metrics.gross_profit)
    gross_loss = _money(metrics.gross_loss)
    return BacktestMetrics(
        backtest_id=backtest_id,
        net_profit=gross_profit + gross_loss,
        gross_profit=gross_profit,
        gross_loss=gross_loss,
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


# --------------------------------------------------------------------------- #
# A session that has not finished: rows written as it goes                      #
# --------------------------------------------------------------------------- #


def open_trade_row(
    position: Position, live_session_id: uuid.UUID, instrument_id: uuid.UUID
) -> Trade:
    """The row a live session writes the moment a trade **opens**.

    All four exit columns are absent, which is what `exit_is_all_or_nothing` means by an open
    position — and `close_trade_values` below is what fills them in later. The alternative,
    writing nothing until the trade closes, would leave a session holding a position for three
    days indistinguishable from one that never traded (`specs/fase-3.md`).

    ⚠️ **`initial_stop_loss`, not `stop_loss`.** The column means *the level the position was
    sized against*, which is what `r_multiple` divides by. A strategy that trails its stop
    (ADR-0018) changes `Position.stop_loss` while the trade runs, so writing that one would
    make the recorded risk drift with the trailing — and every R multiple computed afterwards
    would be measured against a denominator the trade never risked. `ClosedTrade.stop_loss` is
    already the initial one, so the two writes agree by construction.

    ⚠️ **`context` and `snapshot` are written here and never rewritten.** They describe what the
    strategy read *at the entry*; re-deriving them at the exit would mean asking a warmed-up
    indicator what it thought three days ago, and it would answer with today's number.
    """
    return Trade(
        live_session_id=live_session_id,
        instrument_id=instrument_id,
        direction=position.side,
        entry_time=position.entry_time,
        entry_price=position.entry_price,
        volume=position.volume,
        stop_loss=position.initial_stop_loss,
        take_profit=position.take_profit,
        context=_context(position.context),
        snapshot=_snapshot(position.snapshot),
    )


def close_trade_values(trade: ClosedTrade) -> dict[str, Any]:
    """The columns an UPDATE sets when a live trade closes, found by `(session, entry_time)`.

    A mapping rather than a `Trade`, because this is the half of a row that arrives later: the
    caller has an open row and needs the fields to overwrite, not a second object claiming to
    be the same trade. Returning a `Trade` here would invite `session.merge`, and a merge that
    missed the correlation would insert a duplicate instead of failing.

    ⚠️ **`entry_price`, `volume` and `stop_loss` are deliberately absent.** They were settled at
    the fill and this function must not be able to move them. A close that could rewrite the
    entry is a close that can rewrite history — and the R multiple that `r_multiple` reports
    would then have been computed against a stop the row no longer shows.

    `net_pnl` is derived from the two **rounded** legs rather than rounded itself, so the row
    satisfies `net_pnl_balances` by construction — the same reason `_trade_row` does it.
    """
    gross_pnl = _money(trade.gross_pnl)
    costs = _money(trade.costs)
    return {
        "exit_time": trade.exit_time,
        "exit_price": trade.exit_price,
        "exit_reason": _exit_reason(trade.reason),
        "take_profit": trade.take_profit,
        "gross_pnl": gross_pnl,
        "costs": costs,
        "net_pnl": gross_pnl - costs,
        "r_multiple": trade.r_multiple,
    }


__all__ = ["close_trade_values", "closed_trade_row", "open_trade_row", "to_rows"]
