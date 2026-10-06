"""Write a sweep's independent event base: every proposal of its unmanaged runs, replayed flat and
traded alone (`replay`), as `out/sweep=<id>/independent-h<horizon>/events.parquet` — the same
columns as the run-based base (`export.EVENT_SCHEMA`), so `features` and `train` read it as they
read that one.

Read-only on the database: each unmanaged run gives its document, its instrument, its costs and
its window; the bars come from the collector's Parquet. A chart's bars are read once and every run
of that chart is replayed over them, the charts spread over processes.

⚠️ **Warm-up: `WARM_BARS` before the run's first day, not the run's own warm-up.** That one lives
in the API (`warm_window`), which a shared package may not import; a thousand bars settle the
longest average the MM9 reads (200) to well under a pip, and entries before the first day are
left out either way.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import uuid
from bisect import bisect_left, bisect_right
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pyarrow.dataset as ds
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy, SweepPoint
from tradeforge_engine.domain import Candle
from tradeforge_engine.strategy import compile_strategy
from tradeforge_ml.events import TradeRecord, distinct_entries, entry_configuration, is_unmanaged
from tradeforge_ml.export import EVENT_SCHEMA, _event_table
from tradeforge_ml.replay import HORIZON, costs_from, instrument_from, replay, swap_from

WARM_BARS = 1000
"""Bars before a run's first day the setup is shown before any entry counts."""

FORMAT_VERSION = 1


@dataclass(frozen=True)
class Job:
    """One unmanaged run, as much of it as a replay needs — picklable, for a process."""

    run_id: str
    entry_id: str
    symbol: str
    timeframe: str
    configuration: dict[str, Any]
    definition: dict[str, Any]
    instrument: dict[str, Any]
    costs: dict[str, Any]
    date_from: dt.datetime
    date_to: dt.datetime


def _spec_document(row: Any) -> dict[str, Any]:  # noqa: ANN401 — a result row
    """The catalogue's instrument as a run would have kept it, for a run that kept none."""
    return {
        "symbol": row.symbol,
        "name": row.name,
        "asset_class": row.asset_class.value,
        "currency_quote": row.currency_quote,
        "currency_base": row.currency_base,
        "tick_size": str(row.tick_size),
        "tick_value": str(row.tick_value),
        "contract_size": str(row.contract_size),
        "digits": row.digits,
        "exchange": row.exchange,
        "server_offset_hours": row.server_offset / dt.timedelta(hours=1),
    }


def jobs_of(session: Session, sweep_id: uuid.UUID) -> list[Job]:
    """The sweep's finished unmanaged runs, one job each."""
    rows = session.execute(
        select(
            Backtest.id,
            Backtest.timeframe,
            Backtest.date_from,
            Backtest.date_to,
            Backtest.cost_model,
            Backtest.instrument_spec,
            Strategy.definition,
            SweepPoint.entry_id,
            SweepPoint.coordinates,
            Instrument.symbol,
            Instrument.name,
            Instrument.asset_class,
            Instrument.currency_quote,
            Instrument.currency_base,
            Instrument.tick_size,
            Instrument.tick_value,
            Instrument.contract_size,
            Instrument.digits,
            Instrument.exchange,
            Instrument.server_offset,
        )
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(
            SweepPoint,
            (SweepPoint.sweep_id == Backtest.sweep_id)
            & (SweepPoint.strategy_id == Backtest.strategy_id)
            & SweepPoint.same_as.is_(None),
        )
        .where(Backtest.sweep_id == sweep_id, Backtest.status == BacktestStatus.DONE)
        .order_by(Instrument.symbol, Backtest.timeframe, SweepPoint.entry_id, Backtest.created_at)
    )
    jobs: dict[str, Job] = {}
    for row in rows:
        if str(row.id) in jobs or not is_unmanaged(row.definition):
            continue
        jobs[str(row.id)] = Job(
            run_id=str(row.id),
            entry_id=row.entry_id,
            symbol=row.symbol,
            timeframe=row.timeframe,
            configuration=entry_configuration(dict(row.coordinates)),
            definition=dict(row.definition),
            instrument=dict(row.instrument_spec or _spec_document(row)),
            costs=dict(row.cost_model),
            date_from=row.date_from,
            date_to=row.date_to,
        )
    return list(jobs.values())


def candles_of(ohlcv: Path, symbol: str, timeframe: str) -> list[Candle]:
    """A chart's bars from the collector's Parquet, in time order, as the engine's candles."""
    directory = ohlcv / f"symbol={symbol}" / f"timeframe={timeframe}"
    if not directory.exists():
        return []
    table = (
        ds.dataset(directory, format="parquet", partitioning="hive")
        .to_table(columns=["time", "open", "high", "low", "close", "tick_volume", "spread"])
        .sort_by("time")
    )
    columns = {name: table.column(name).to_pylist() for name in table.column_names}
    return [
        Candle(
            time=columns["time"][n],
            open=columns["open"][n],
            high=columns["high"][n],
            low=columns["low"][n],
            close=columns["close"][n],
            tick_volume=columns["tick_volume"][n],
            spread=columns["spread"][n],
        )
        for n in range(table.num_rows)
    ]


def _window(candles: Sequence[Candle], job: Job) -> list[Candle]:
    """The run's bars and the `WARM_BARS` before them."""
    times = [candle.time for candle in candles]
    first, last = bisect_left(times, job.date_from), bisect_right(times, job.date_to)
    return list(candles[max(0, first - WARM_BARS) : last])


def replay_job(job: Job, candles: Sequence[Candle], horizon: int) -> list[TradeRecord]:
    """One run's proposals, each traded alone, as the event base's records."""
    instrument = instrument_from(job.instrument)
    strategy = compile_strategy(job.definition, server_offset=instrument.server_offset)
    proposals = replay(
        strategy=strategy,
        candles=_window(candles, job),
        instrument=instrument,
        cost_model=costs_from(job.costs),
        swap=swap_from(job.costs),
        horizon=horizon,
        entries_from=job.date_from,
    )
    return [
        TradeRecord(
            run_id=job.run_id,
            entry_id=job.entry_id,
            symbol=job.symbol,
            timeframe=job.timeframe,
            configuration=job.configuration,
            side=str(proposal.trade.side.value),
            entry_time=proposal.trade.entry_time,
            entry_price=proposal.trade.entry_price,
            stop_loss=proposal.trade.stop_loss,
            exit_time=proposal.trade.exit_time,
            exit_price=proposal.trade.exit_price,
            exit_reason=proposal.exit_reason,
            r_multiple=proposal.trade.r_multiple,
            mfe_r=proposal.trade.mfe_r,
            mae_r=proposal.trade.mae_r,
            gross_pnl=proposal.trade.gross_pnl,
            costs=proposal.trade.costs,
            swap=proposal.trade.swap,
            net_pnl=proposal.trade.net_pnl,
            volume=proposal.trade.volume,
        )
        for proposal in proposals
        if proposal.trade.entry_time <= job.date_to
    ]


def _chart(task: tuple[Path, int, list[Job]]) -> tuple[list[dict[str, Any]], int]:
    """Every job of one chart over its bars, read once; the distinct entries and the proposals."""
    ohlcv, horizon, jobs = task
    candles = candles_of(ohlcv, jobs[0].symbol, jobs[0].timeframe)
    records: list[TradeRecord] = []
    for job in jobs:
        records.extend(replay_job(job, candles, horizon))
    return distinct_entries(records), len(records)


@dataclass
class ReplayReport:
    sweep_id: str
    horizon: int
    runs: int = 0
    charts: int = 0
    proposals: int = 0
    events: int = 0
    events_disagreeing: int = 0
    exits: dict[str, int] = field(default_factory=dict)


def write_independent(  # noqa: PLR0913 — keyword-only; the base, where, and how to run it
    jobs: Sequence[Job],
    *,
    sweep_id: str,
    out: Path,
    ohlcv: Path,
    horizon: int = HORIZON,
    processes: int | None = None,
) -> ReplayReport:
    """Replay every job, chart by chart over `processes`, and write the base under
    `out/sweep=<id>/independent-h<horizon>/`."""
    target = out / f"sweep={sweep_id}" / f"independent-h{horizon}"
    target.mkdir(parents=True, exist_ok=True)
    charts: defaultdict[tuple[str, str], list[Job]] = defaultdict(list)
    for job in jobs:
        charts[(job.symbol, job.timeframe)].append(job)
    report = ReplayReport(sweep_id=sweep_id, horizon=horizon, runs=len(jobs), charts=len(charts))
    tasks = [(ohlcv, horizon, chart_jobs) for chart_jobs in charts.values()]
    writer = pq.ParquetWriter(target / "events.parquet", EVENT_SCHEMA, compression="zstd")
    try:
        workers = processes or max(1, (os.cpu_count() or 2) // 2)
        # One process is this one: no pool to spawn, which is also what a test runs.
        pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
        with pool or nullcontext():
            for rows, proposals in (pool.map if pool else map)(_chart, tasks):
                report.proposals += proposals
                if not rows:
                    continue
                writer.write_table(_event_table(rows))
                report.events += len(rows)
                report.events_disagreeing += sum(1 for row in rows if not row["outcomes_agree"])
                for row in rows:
                    reason = str(row["exit_reason"])
                    report.exits[reason] = report.exits.get(reason, 0) + 1
    finally:
        writer.close()
    manifest = {
        "format_version": FORMAT_VERSION,
        "kind": "independent",
        "sweep_id": sweep_id,
        "horizon_bars": horizon,
        "warm_bars": WARM_BARS,
        "written_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "runs": report.runs,
        "charts": report.charts,
        "proposals": report.proposals,
        "events": report.events,
        "events_disagreeing": report.events_disagreeing,
        "exits": report.exits,
        "events_sha256": hashlib.sha256((target / "events.parquet").read_bytes()).hexdigest(),
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return report


__all__ = [
    "WARM_BARS",
    "Job",
    "ReplayReport",
    "candles_of",
    "jobs_of",
    "replay_job",
    "write_independent",
]
