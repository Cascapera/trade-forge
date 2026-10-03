"""Export a sweep as the base meta-labeling reads (ADR-0031): its events and its runs, in Parquet.

Two files and a manifest under `out/sweep=<id>/`:

* `events.parquet` — one row per distinct entry of the sweep's **unmanaged** runs (no target, no
  breakeven), with its outcome in R, how far it went (MFE, MAE) and the configurations that took
  it (`events.distinct_entries`);
* `runs.parquet` — every finished run's coordinates and measures, managed or not: the
  configurations a model or a person compares;
* `manifest.json` — what was read, what was written, and whether every exported run reconciles
  with its recorded metrics (`events.reconciles`). A base that does not reconcile is not a base.

⚠️ **Read a group at a time** — one setup on one market and chart. A sweep holds tens of millions of
trades; the unmanaged ones of a group are a few thousand, and deduplicating them needs only them.
"""

import datetime as dt
import hashlib
import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    Instrument,
    Strategy,
    SweepPoint,
    Trade,
)
from tradeforge_ml.events import (
    TradeRecord,
    distinct_entries,
    entry_configuration,
    is_unmanaged,
    reconciles,
)

RUN_BLOCK = 5000
"""Runs written to `runs.parquet` at a time."""

FORMAT_VERSION = 1
"""Bumped when a column changes meaning: a model records the version of the base it learned on."""

EVENT_SCHEMA = pa.schema(
    [
        ("entry_id", pa.string()),
        ("symbol", pa.string()),
        ("timeframe", pa.string()),
        ("side", pa.string()),
        ("entry_time", pa.timestamp("us", tz="UTC")),
        ("entry_price", pa.float64()),
        ("stop_loss", pa.float64()),
        ("exit_time", pa.timestamp("us", tz="UTC")),
        ("exit_price", pa.float64()),
        ("exit_reason", pa.string()),
        ("r_multiple", pa.float64()),
        ("mfe_r", pa.float64()),
        ("mae_r", pa.float64()),
        ("gross_pnl", pa.float64()),
        ("costs", pa.float64()),
        ("swap", pa.float64()),
        ("net_pnl", pa.float64()),
        ("volume", pa.float64()),
        ("first_run_id", pa.string()),
        ("configurations", pa.string()),
        ("n_configurations", pa.int32()),
        ("outcomes_agree", pa.bool_()),
    ]
)

RUN_SCHEMA = pa.schema(
    [
        ("run_id", pa.string()),
        ("entry_id", pa.string()),
        ("symbol", pa.string()),
        ("timeframe", pa.string()),
        ("date_from", pa.timestamp("us", tz="UTC")),
        ("date_to", pa.timestamp("us", tz="UTC")),
        ("label", pa.string()),
        ("coordinates", pa.string()),
        ("unmanaged", pa.bool_()),
        ("total_trades", pa.int64()),
        ("net_r", pa.float64()),
        ("max_drawdown_r", pa.float64()),
        ("positive_year_share", pa.float64()),
        ("yearly_r", pa.string()),
        ("ruined_at", pa.timestamp("us", tz="UTC")),
    ]
)


@dataclass(frozen=True, slots=True)
class RunInfo:
    run_id: uuid.UUID
    entry_id: str
    symbol: str
    timeframe: str
    label: str
    coordinates: dict[str, Any]
    unmanaged: bool
    date_from: dt.datetime
    date_to: dt.datetime
    total_trades: int
    net_r: Decimal | None
    max_drawdown_r: Decimal | None
    positive_year_share: Decimal | None
    yearly_r: dict[str, Any] | None
    ruined_at: dt.datetime | None


@dataclass(slots=True)
class Report:
    """What an export read and wrote, and what did not reconcile."""

    sweep_id: str
    runs: int = 0
    unmanaged_runs: int = 0
    trades_read: int = 0
    events: int = 0
    events_disagreeing: int = 0
    unreconciled: list[str] = field(default_factory=list)


def _float(value: Decimal | float | None) -> float | None:
    return None if value is None else float(value)


def runs_of(session: Session, sweep_id: uuid.UUID) -> Iterator[RunInfo]:
    """The sweep's finished runs with their own point, grouped by setup, market and chart.

    ⚠️ **Columns, never entities.** A sweep's runs are read as a stream, and loading each as an
    ORM object would fill the session with hundreds of thousands of them; columns keep it empty.
    """
    rows = session.execute(
        select(
            Backtest.id,
            Backtest.timeframe,
            Backtest.date_from,
            Backtest.date_to,
            BacktestMetrics.total_trades,
            BacktestMetrics.net_r,
            BacktestMetrics.max_drawdown_r,
            BacktestMetrics.positive_year_share,
            BacktestMetrics.yearly_r,
            BacktestMetrics.ruined_at,
            Instrument.symbol,
            Strategy.definition,
            SweepPoint.entry_id,
            SweepPoint.label,
            SweepPoint.coordinates,
        )
        .join(BacktestMetrics, BacktestMetrics.backtest_id == Backtest.id)
        .join(Instrument, Instrument.id == Backtest.instrument_id)
        .join(Strategy, Strategy.id == Backtest.strategy_id)
        .join(
            SweepPoint,
            (SweepPoint.sweep_id == Backtest.sweep_id)
            & (SweepPoint.strategy_id == Backtest.strategy_id)
            & SweepPoint.same_as.is_(None),
        )
        .where(Backtest.sweep_id == sweep_id, Backtest.status == BacktestStatus.DONE)
        .order_by(SweepPoint.entry_id, Instrument.symbol, Backtest.timeframe, Backtest.created_at)
        .execution_options(yield_per=2000)
    )
    seen: set[uuid.UUID] = set()
    for row in rows:
        # A strategy can own more than one point of a sweep; its run is one run.
        if row.id in seen:
            continue
        seen.add(row.id)
        yield RunInfo(
            run_id=row.id,
            entry_id=row.entry_id,
            symbol=row.symbol,
            timeframe=row.timeframe,
            label=row.label,
            coordinates=dict(row.coordinates),
            unmanaged=is_unmanaged(row.definition),
            date_from=row.date_from,
            date_to=row.date_to,
            total_trades=row.total_trades,
            net_r=row.net_r,
            max_drawdown_r=row.max_drawdown_r,
            positive_year_share=row.positive_year_share,
            yearly_r=row.yearly_r,
            ruined_at=row.ruined_at,
        )


def trades_of(session: Session, run: RunInfo) -> list[TradeRecord]:
    """An unmanaged run's trades, in entry order, as the event base reads them."""
    configuration = entry_configuration(run.coordinates)
    return [
        TradeRecord(
            run_id=str(run.run_id),
            entry_id=run.entry_id,
            symbol=run.symbol,
            timeframe=run.timeframe,
            configuration=configuration,
            side=str(trade.direction.value),
            entry_time=trade.entry_time,
            entry_price=trade.entry_price,
            stop_loss=trade.stop_loss,
            exit_time=trade.exit_time,
            exit_price=trade.exit_price,
            exit_reason=None if trade.exit_reason is None else trade.exit_reason.value,
            r_multiple=trade.r_multiple,
            mfe_r=trade.mfe_r,
            mae_r=trade.mae_r,
            gross_pnl=trade.gross_pnl,
            costs=trade.costs,
            swap=trade.swap,
            net_pnl=trade.net_pnl,
            volume=trade.volume,
        )
        for trade in session.execute(
            select(
                Trade.direction,
                Trade.entry_time,
                Trade.entry_price,
                Trade.stop_loss,
                Trade.exit_time,
                Trade.exit_price,
                Trade.exit_reason,
                Trade.r_multiple,
                Trade.mfe_r,
                Trade.mae_r,
                Trade.gross_pnl,
                Trade.costs,
                Trade.swap,
                Trade.net_pnl,
                Trade.volume,
            )
            .where(Trade.backtest_id == run.run_id)
            .order_by(Trade.entry_time, Trade.id)
        )
    ]


def _event_table(rows: Sequence[dict[str, Any]]) -> pa.Table:
    columns: dict[str, list[Any]] = {name: [] for name in EVENT_SCHEMA.names}
    for row in rows:
        for name in EVENT_SCHEMA.names:
            if name == "configurations":
                columns[name].append(json.dumps(row["configurations"], sort_keys=True, default=str))
            elif name == "n_configurations":
                columns[name].append(len(row["configurations"]))
            elif EVENT_SCHEMA.field(name).type == pa.float64():
                columns[name].append(_float(row[name]))
            else:
                columns[name].append(row[name])
    return pa.table(columns, schema=EVENT_SCHEMA)


def _run_row(run: RunInfo) -> dict[str, Any]:
    return {
        "run_id": str(run.run_id),
        "entry_id": run.entry_id,
        "symbol": run.symbol,
        "timeframe": run.timeframe,
        "date_from": run.date_from,
        "date_to": run.date_to,
        "label": run.label,
        "coordinates": json.dumps(run.coordinates, sort_keys=True, default=str),
        "unmanaged": run.unmanaged,
        "total_trades": run.total_trades,
        "net_r": _float(run.net_r),
        "max_drawdown_r": _float(run.max_drawdown_r),
        "positive_year_share": _float(run.positive_year_share),
        "yearly_r": json.dumps(run.yearly_r or {}, sort_keys=True, default=str),
        "ruined_at": run.ruined_at,
    }


def export_sweep(session: Session, sweep_id: uuid.UUID, out: Path) -> Report:
    """Write the sweep's base under `out/sweep=<id>/` and say what it did. Read-only on the
    database: nothing is deleted or changed here — the clean-up is a separate, later step."""
    target = out / f"sweep={sweep_id}"
    target.mkdir(parents=True, exist_ok=True)
    report = Report(sweep_id=str(sweep_id))
    events_writer = pq.ParquetWriter(target / "events.parquet", EVENT_SCHEMA, compression="zstd")
    runs_writer = pq.ParquetWriter(target / "runs.parquet", RUN_SCHEMA, compression="zstd")
    try:
        group: tuple[str, str, str] | None = None
        pending: list[TradeRecord] = []
        run_rows: list[dict[str, Any]] = []

        def flush_events() -> None:
            rows = distinct_entries(pending)
            if rows:
                events_writer.write_table(_event_table(rows))
            report.events += len(rows)
            report.events_disagreeing += sum(1 for row in rows if not row["outcomes_agree"])
            pending.clear()

        for run in runs_of(session, sweep_id):
            report.runs += 1
            run_rows.append(_run_row(run))
            if len(run_rows) >= RUN_BLOCK:
                runs_writer.write_table(pa.Table.from_pylist(run_rows, schema=RUN_SCHEMA))
                run_rows.clear()
            this = (run.entry_id, run.symbol, run.timeframe)
            if this != group:
                flush_events()
                group = this
            if not run.unmanaged:
                continue
            report.unmanaged_runs += 1
            trades = trades_of(session, run)
            report.trades_read += len(trades)
            if not reconciles(trades, total_trades=run.total_trades, net_r=run.net_r):
                report.unreconciled.append(str(run.run_id))
            pending.extend(trades)
        flush_events()
        if run_rows:
            runs_writer.write_table(pa.Table.from_pylist(run_rows, schema=RUN_SCHEMA))
    finally:
        events_writer.close()
        runs_writer.close()

    manifest = {
        "format_version": FORMAT_VERSION,
        "sweep_id": report.sweep_id,
        "written_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "runs": report.runs,
        "unmanaged_runs": report.unmanaged_runs,
        "trades_read": report.trades_read,
        "events": report.events,
        "events_disagreeing": report.events_disagreeing,
        "unreconciled_runs": report.unreconciled,
        "files": {
            name: hashlib.sha256((target / name).read_bytes()).hexdigest()
            for name in ("events.parquet", "runs.parquet")
        },
    }
    (target / "manifest.json").write_text(json.dumps(manifest, indent=2))
    return report


__all__ = ["EVENT_SCHEMA", "FORMAT_VERSION", "RUN_SCHEMA", "Report", "export_sweep"]
