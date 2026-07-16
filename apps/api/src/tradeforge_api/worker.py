"""The backtest worker: where the CPU-bound work the API refuses to do actually runs.

arq drains the queue and calls `run_backtest` in a process separate from the API. The job is
a single row id — everything else is read from the database, which is what makes the worker
idempotent and the enqueue cheap: re-running the same id re-runs the same backtest.

The lifecycle is a state machine written into the `backtests` row: `queued → running → done`
(or `→ failed`, with the reason). Each transition is committed and announced on the run's
progress channel, so a WebSocket subscriber sees the same story the database tells. A failure
is *recorded*, not re-raised — a wrong strategy is a result to report (`GET /backtests/{id}`),
not a job for arq to retry forever.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

from redis.asyncio import Redis
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.queue import progress_channel, redis_settings
from tradeforge_api.runner import execute_backtest
from tradeforge_collector import read_candles
from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy
from tradeforge_db.results import to_rows
from tradeforge_db.session import create_db_engine, create_session_factory


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def _announce(redis: Redis, backtest_id: uuid.UUID, payload: dict[str, Any]) -> None:
    """Publish one progress event. Best-effort: a subscriber that missed it can still read the
    final state from the database, so a publish failure must never fail the run."""
    await redis.publish(progress_channel(backtest_id), json.dumps(payload))


async def process_backtest(
    *,
    session: Session,
    redis: Redis,
    parquet_root: Path,
    backtest_id: uuid.UUID,
) -> None:
    """Run one backtest end to end, driving its row through the status state machine.

    Split from the arq entry point so it can be exercised inline in a test — pass a real
    session and Redis, and this is the whole worker without a running arq process.
    """
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        return  # the row was deleted between enqueue and pickup; nothing to run

    try:
        backtest.status = BacktestStatus.RUNNING
        backtest.started_at = _now()
        session.commit()
        await _announce(redis, backtest_id, {"status": "running", "progress": 0.0})

        strategy = session.get(Strategy, backtest.strategy_id)
        instrument = session.get(Instrument, backtest.instrument_id)
        if strategy is None or instrument is None:
            raise ValueError("backtest references a missing strategy or instrument")

        candles = read_candles(parquet_root, instrument.symbol, backtest.timeframe)
        trades, metrics = execute_backtest(
            definition=strategy.definition,
            instrument=instrument,
            timeframe=backtest.timeframe,
            date_from=backtest.date_from,
            date_to=backtest.date_to,
            initial_capital=backtest.initial_capital,
            cost_model=backtest.cost_model,
            slippage_ticks=Decimal(0),
            candles=candles,
        )

        metrics_row, trade_rows = to_rows(
            trades=trades,
            metrics=metrics,
            backtest_id=backtest.id,
            instrument_id=instrument.id,
        )
        session.add(metrics_row)
        session.add_all(trade_rows)
        backtest.status = BacktestStatus.DONE
        backtest.finished_at = _now()
        session.commit()
        await _announce(redis, backtest_id, {"status": "done", "progress": 1.0})

    except Exception as exc:  # noqa: BLE001 — a failed run is a recorded result, not a crash
        session.rollback()
        _record_failure(session, backtest_id, exc)
        await _announce(redis, backtest_id, {"status": "failed", "error": _reason(exc)})


def _record_failure(session: Session, backtest_id: uuid.UUID, exc: Exception) -> None:
    backtest = session.get(Backtest, backtest_id)
    if backtest is None:
        return
    backtest.status = BacktestStatus.FAILED
    backtest.error = _reason(exc)
    # `finished_at` may only be set once the run has started (a DB CHECK). If we failed before
    # even marking it running, stamp the start now so the row stays internally consistent.
    if backtest.started_at is None:
        backtest.started_at = _now()
    backtest.finished_at = _now()
    session.commit()


def _reason(exc: Exception) -> str:
    """A driver or engine error can be a multi-line essay; the `error` column wants one line."""
    return " ".join(str(exc).split()) or exc.__class__.__name__


# --------------------------------------------------------------------------- #
# arq wiring                                                                    #
# --------------------------------------------------------------------------- #


async def run_backtest(ctx: dict[str, Any], backtest_id: str) -> None:
    """The registered job. arq passes the run's id as a string; everything else is read from
    the database inside `process_backtest`."""
    session: Session = ctx["session_factory"]()
    settings: Settings = ctx["settings"]
    try:
        await process_backtest(
            session=session,
            redis=ctx["redis"],
            parquet_root=settings.parquet_root,
            backtest_id=uuid.UUID(backtest_id),
        )
    finally:
        session.close()


async def startup(ctx: dict[str, Any]) -> None:
    settings = Settings()
    engine = create_db_engine(settings.sqlalchemy_dsn)
    ctx["settings"] = settings
    ctx["engine"] = engine
    ctx["session_factory"] = create_session_factory(engine)


async def shutdown(ctx: dict[str, Any]) -> None:
    ctx["engine"].dispose()


class WorkerSettings:
    """`arq tradeforge_api.worker.WorkerSettings` starts the worker from this."""

    functions = (run_backtest,)
    redis_settings = redis_settings(Settings())
    on_startup = startup
    on_shutdown = shutdown


__all__ = ["WorkerSettings", "process_backtest", "run_backtest"]
