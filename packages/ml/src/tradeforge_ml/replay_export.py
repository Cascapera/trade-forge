"""Write a sweep's independent event base: every proposal of every entry it tried, replayed flat
and traded alone (`replay`), as `out/sweep=<id>/independent-h<horizon>/events.parquet` — the same
columns as the run-based base (`export.EVENT_SCHEMA`), so `features` and `train` read it as they
read that one.

Read-only on the database: one run per entry (`choose_jobs`) gives its document, its instrument,
its window and its market's measured spread; the bars come from the collector's Parquet. The
label charges that spread whatever the sweep was charged (`replay.measured_costs`, 14/10), and
the manifest says what each chart paid. A chart's bars are read once and every run of that chart
is replayed over them, the charts spread over processes.

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
from collections.abc import Iterable, Sequence
from concurrent.futures import ProcessPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import pyarrow.dataset as ds
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy, SweepPoint
from tradeforge_engine.domain import AssetClass, Candle
from tradeforge_engine.strategy import compile_strategy
from tradeforge_ml.events import (
    TradeRecord,
    distinct_entries,
    entry_configuration,
    is_unmanaged,
    without_management,
)
from tradeforge_ml.export import EVENT_SCHEMA, _event_table
from tradeforge_ml.replay import (
    HORIZON,
    costs_from,
    instrument_from,
    measured_costs,
    replay,
    swap_from,
)

WARM_BARS = 1000
"""Bars before a run's first day the setup is shown before any entry counts."""

FORMAT_VERSION = 2
"""2 (14/10): the label charges each market's measured spread, and the manifest says what."""

Costs = Literal["measured", "run"]
"""`measured`: each market's measured spread, the run's commission and swap (the default since
14/10). `run`: what the run was charged, as before — for a base to be compared with an old one."""


@dataclass(frozen=True)
class Job:
    """One entry of a sweep, as much of the run standing for it as a replay needs — picklable, for
    a process. `definition` is that run's document with its management taken off."""

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
    spread_ticks: str | None = None
    """The market's measured spread (`instruments.default_spread_points`); none when unmeasured."""


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
    """The sweep's finished runs, one job per entry (`choose_jobs`)."""
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
            Instrument.default_spread_points,
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
        # ⚠️ `Backtest.id` last (12/10): a sweep writes all its runs in one transaction, so
        # `created_at` ties across the whole sweep, and the order — which run stands for an entry,
        # whose outcome a shared entry keeps — was the query plan's to choose.
        .order_by(
            Instrument.symbol,
            Backtest.timeframe,
            SweepPoint.entry_id,
            Backtest.created_at,
            Backtest.id,
        )
    )
    return choose_jobs(rows)


def choose_jobs(rows: Iterable[Any]) -> list[Job]:
    """One job per entry the sweep tried — the same setup, chart and window — whatever target and
    breakeven its runs managed it with (12/10).

    The replay trades every proposal with its initial stop and a time barrier only (`replay`): a
    run's target and breakeven never reach it, and the targets are scored afterwards from each
    trade's excursion. So the runs that differ only in management are one entry replayed many
    times over — and a grid with no unmanaged point left nothing at all to replay: every CHOCH and
    CONTINUATION grid of 08/10 to 11/10 had targets of 3, 5, 7 or 10R and no "none".

    ⚠️ **The run that stands for an entry is its unmanaged one when the grid has it**, so the
    entries a base already had are replayed from the very runs they were before; otherwise the
    first one, with its management taken off (`without_management`) — the document its unmanaged
    sibling would have carried. `run_id` names that run either way.
    """
    jobs: dict[tuple[str, ...], Job] = {}
    stand_ins: set[tuple[str, ...]] = set()
    for row in rows:
        configuration = entry_configuration(dict(row.coordinates))
        entry = (
            str(row.entry_id),
            row.symbol,
            row.timeframe,
            json.dumps(configuration, sort_keys=True, default=str),
            row.date_from.isoformat(),
            row.date_to.isoformat(),
        )
        unmanaged = is_unmanaged(row.definition)
        if entry in jobs and not (unmanaged and entry in stand_ins):
            continue
        if unmanaged:
            stand_ins.discard(entry)
        else:
            stand_ins.add(entry)
        jobs[entry] = Job(
            run_id=str(row.id),
            entry_id=row.entry_id,
            symbol=row.symbol,
            timeframe=row.timeframe,
            configuration=configuration,
            definition=without_management(row.definition),
            instrument=dict(row.instrument_spec or _spec_document(row)),
            costs=dict(row.cost_model),
            date_from=row.date_from,
            date_to=row.date_to,
            spread_ticks=(
                None if row.default_spread_points is None else str(row.default_spread_points)
            ),
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


def charged(job: Job, candles: Sequence[Candle], costs: Costs) -> dict[str, Any]:
    """The cost document `job` is replayed with: its market's measured spread, or its run's."""
    if costs == "run":
        return job.costs
    if job.spread_ticks is None:
        raise ValueError(f"{job.symbol} has no measured spread")
    return measured_costs(
        job.costs,
        spread_ticks=Decimal(job.spread_ticks),
        asset_class=AssetClass(job.instrument["asset_class"]),
        last_close=candles[-1].close,
    )


def replay_job(
    job: Job, candles: Sequence[Candle], horizon: int, costs: dict[str, Any] | None = None
) -> list[TradeRecord]:
    """One run's proposals, each traded alone, as the event base's records — charged `costs`, or
    what the run was."""
    instrument = instrument_from(job.instrument)
    strategy = compile_strategy(job.definition, server_offset=instrument.server_offset)
    document = job.costs if costs is None else costs
    proposals = replay(
        strategy=strategy,
        candles=_window(candles, job),
        instrument=instrument,
        cost_model=costs_from(document),
        swap=swap_from(document),
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


def _chart(
    task: tuple[Path, int, Costs, list[Job]],
) -> tuple[list[dict[str, Any]], int, dict[str, Any] | None]:
    """Every job of one chart over its bars, read once: the distinct entries, the proposals, and
    what the chart's first job was charged (with measured costs, what every job of it was)."""
    ohlcv, horizon, costs, jobs = task
    candles = candles_of(ohlcv, jobs[0].symbol, jobs[0].timeframe)
    if not candles:
        return [], 0, None
    records: list[TradeRecord] = []
    first: dict[str, Any] | None = None
    for job in jobs:
        document = charged(job, candles, costs)
        first = first or document
        records.extend(replay_job(job, candles, horizon, document))
    return distinct_entries(records), len(records), first


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
    costs: Costs = "measured",
) -> ReplayReport:
    """Replay every job, chart by chart over `processes`, and write the base under
    `out/sweep=<id>/independent-h<horizon>/`.

    ⚠️ With `measured` costs, a market without a measured spread refuses the base before any bar
    is read — as the sweep's API does — rather than label its trades as free."""
    if costs == "measured":
        unmeasured = sorted({job.symbol for job in jobs if job.spread_ticks is None})
        if unmeasured:
            raise ValueError(
                "no measured spread for: " + ", ".join(unmeasured) + " (catalogue them while "
                "their market is open, or replay with the runs' own costs)"
            )
    target = out / f"sweep={sweep_id}" / f"independent-h{horizon}"
    target.mkdir(parents=True, exist_ok=True)
    charts: defaultdict[tuple[str, str], list[Job]] = defaultdict(list)
    for job in jobs:
        charts[(job.symbol, job.timeframe)].append(job)
    report = ReplayReport(sweep_id=sweep_id, horizon=horizon, runs=len(jobs), charts=len(charts))
    tasks = [(ohlcv, horizon, costs, chart_jobs) for chart_jobs in charts.values()]
    paid: dict[str, dict[str, Any]] = {}
    writer = pq.ParquetWriter(target / "events.parquet", EVENT_SCHEMA, compression="zstd")
    try:
        workers = processes or max(1, (os.cpu_count() or 2) // 2)
        # One process is this one: no pool to spawn, which is also what a test runs.
        pool = ProcessPoolExecutor(max_workers=workers) if workers > 1 else None
        with pool or nullcontext():
            for (symbol, timeframe), (rows, proposals, document) in zip(
                charts, (pool.map if pool else map)(_chart, tasks), strict=True
            ):
                if document is not None:
                    paid[f"{symbol} {timeframe}"] = document
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
        "costs": costs,
        "charged": paid,
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
    "Costs",
    "Job",
    "ReplayReport",
    "candles_of",
    "charged",
    "choose_jobs",
    "jobs_of",
    "replay_job",
    "write_independent",
]
