"""A run whose measurement was already made is copied, not run again (28/09).

His ask: "if I already have that backtest, take it". A sweep's run is fully determined by its
strategy document, market, chart, window, capital, costs, the instrument it executes with and the
engine — the engine is deterministic — so a finished run with all of those equal *is* the answer.
The worker asks before every sweep run (`worker.process_backtest`, `worker.process_batch`); a hit
is written as the new run's own rows, a copy, and `reused_from` names where it came from.

⚠️ **Copied, never linked.** Every reader of a sweep — its summary, runs page, dataset, test,
slices, Monte Carlo, clusters — finds its runs by `sweep_id`. A run that pointed elsewhere for its
result would need each of them taught to follow the pointer; a copy is read by all of them as is.

⚠️ **Only under the same engine version** (`ENGINE_VERSION`). A fix that changes a trade must
raise it, or this hands back results the engine no longer gives — it sat at 0.1.0 through many
such fixes, and was raised to 0.2.0 with this module so that nothing from before is reused.

⚠️ **Only when the original kept what the copy must keep.** Whether a sweep's run keeps its trades
depends on where it is (`retention.recorded_for`): a reserved-window test keeps them always, a
sweep only for a run that made money. An original that kept only its metrics cannot stand for a
run that must keep its trades — that one runs.

⚠️ **Only over the same candles.** The window asked for is not the data in it: a chart collected
later with more history, or with a gap filled, gives the same request other bars — and the engine
other trades. The original must have read exactly what the run would read now: as many bars, from
the same first to the same last (`window_of`, measured on the disk as it is). ⚠️ A bar corrected
in place — same count, same ends, another price — is not seen; that would take a hash of the data.

Pieces of a longer run (2020-25 answering 2022-24) are not reused: warm-up, a trade open across
the cut and the balance make them differ, by an amount not yet measured.
"""

import datetime as dt
from bisect import bisect_left, bisect_right
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.retention import recorded_for
from tradeforge_api.runner import ENGINE_VERSION
from tradeforge_api.warm_window import WarmUp, warm_start
from tradeforge_db.models import Backtest, BacktestMetrics, BacktestStatus, Recorded, Trade
from tradeforge_engine.domain import Candle

_KEEPS: Mapping[Recorded, int] = {Recorded.METRICS: 0, Recorded.TRADES: 1, Recorded.FULL: 2}
"""How much each level keeps: an original stands for a copy that keeps as much or less."""

_CANDIDATES = 5
"""Originals looked at per run, newest first. Identical measurements have identical results, so
they differ only in what they kept; five is plenty to find one that kept enough."""


def window_of(
    candles: Sequence[Candle], date_from: dt.datetime, date_to: dt.datetime, warmup: WarmUp
) -> tuple[int, dt.datetime, dt.datetime, int] | None:
    """The bars a run over this window reads — how many, the first and the last, and how many
    before it to warm up (ADR-0030) — or `None` for none. The same bounds as the run's own clip
    (`runner._candles_to_run`: both ends included) and warm-up (`warm_window.warm_start`), by
    binary search: the reader hands candles in time order, and a sweep asks this per run."""
    start = bisect_left(candles, date_from, key=lambda candle: candle.time)
    end = bisect_right(candles, date_to, key=lambda candle: candle.time)
    if end <= start:
        return None
    warmed = start - warm_start(candles, warmup, date_from)
    return end - start, candles[start].time, candles[end - 1].time, warmed


def needs(run: Backtest, metrics: BacktestMetrics) -> Recorded:
    """What `run` would keep if it produced these metrics — the worker's own rule, read from the
    original's results, which are the run's results."""
    targets: Mapping[str, Any] = metrics.targets or {}
    return recorded_for(
        in_sweep=run.sweep_id is not None,
        timeframe=run.timeframe,
        net_profit=metrics.net_profit,
        total_trades=metrics.total_trades,
        target_net_r=[
            None if rung is None else Decimal(str(rung["net_r"])) for rung in targets.values()
        ],
        reserved_test=run.sweep is not None and run.sweep.holdout_rule is not None,
    )


def original_for(
    session: Session,
    run: Backtest,
    spec: Mapping[str, Any],
    window: tuple[int, dt.datetime, dt.datetime, int],
) -> tuple[Backtest, Recorded] | None:
    """A finished run of the very same measurement that kept enough to stand for `run`, and what
    `run` will keep — or `None`, and `run` runs.

    `spec` is the instrument `run` executes with (`runner.spec_document`). An original that kept
    none ran before 25/09 — under 0.1.0, so it is never a candidate anyway. `window` is what the
    run would read from the disk now (`window_of`).
    """
    candles, first, last, warmed = window
    found = session.scalars(
        select(Backtest)
        .where(
            Backtest.strategy_id == run.strategy_id,
            Backtest.instrument_id == run.instrument_id,
            Backtest.timeframe == run.timeframe,
            Backtest.date_from == run.date_from,
            Backtest.date_to == run.date_to,
            Backtest.initial_capital == run.initial_capital,
            Backtest.cost_model == run.cost_model,
            Backtest.engine_version == ENGINE_VERSION,
            Backtest.status == BacktestStatus.DONE,
            Backtest.instrument_spec == dict(spec),
            Backtest.candles_seen == candles,
            Backtest.first_candle == first,
            Backtest.last_candle == last,
            # Warmed on as many bars (ADR-0030): a run that starts where the history starts
            # warms on less, and its first trades may differ from one that warmed in full.
            Backtest.warmup_bars == warmed,
            Backtest.id != run.id,
        )
        .order_by(Backtest.finished_at.desc())
        .limit(_CANDIDATES)
    ).all()
    for original in found:
        if original.metrics is None:
            continue
        kept = needs(run, original.metrics)
        if _KEEPS[original.recorded] >= _KEEPS[kept]:
            return original, kept
    return None


def copy_into(session: Session, original: Backtest, run: Backtest, recorded: Recorded) -> None:
    """Write `original`'s result as `run`'s own: its metrics, the trades `recorded` keeps, and what
    it read. The caller commits.

    ⚠️ **Only what the copy keeps.** A copy that keeps metrics gets no trades, one that keeps
    trades gets them without their pictures and without the equity curve — the same rows the
    worker would have written for it (`results.to_rows`).
    """
    source = original.metrics
    if source is None:
        raise ValueError(f"run {original.id} has no metrics to copy")
    columns = {
        column.key: getattr(source, column.key)
        for column in BacktestMetrics.__table__.columns
        if column.key != "backtest_id"
    }
    if recorded is not Recorded.FULL:
        columns["equity_curve"] = None
    session.add(BacktestMetrics(backtest_id=run.id, **columns))

    if recorded is not Recorded.METRICS:
        trade_columns = [
            column.key
            for column in Trade.__table__.columns
            if column.key not in ("id", "backtest_id")
        ]
        for trade in session.scalars(select(Trade).where(Trade.backtest_id == original.id)):
            values = {key: getattr(trade, key) for key in trade_columns}
            if recorded is not Recorded.FULL:
                values["snapshot"] = None
            session.add(Trade(backtest_id=run.id, **values))

    now = dt.datetime.now(tz=dt.UTC)
    run.recorded = recorded
    run.reused_from = original.id
    run.candles_seen = original.candles_seen
    run.first_candle = original.first_candle
    run.last_candle = original.last_candle
    # What it warmed on, too (ADR-0030): `original_for` matched on it, so the copy read as many.
    run.warmup_bars = original.warmup_bars
    run.status = BacktestStatus.DONE
    run.started_at = run.started_at or now
    run.finished_at = now


__all__ = ["copy_into", "needs", "original_for", "window_of"]
