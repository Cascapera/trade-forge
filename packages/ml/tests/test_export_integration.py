"""Exporting a sweep's event base from real Postgres (02/10, ADR-0031)."""

import datetime as dt
import json
import uuid
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq
import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import upgrade
from tradeforge_db.models import (
    Backtest,
    BacktestMetrics,
    BacktestStatus,
    ExitReason,
    Instrument,
    Strategy,
    Sweep,
    SweepPoint,
    Trade,
)
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_db.testing import truncate
from tradeforge_engine.domain import AssetClass, Side
from tradeforge_ml.cli import main
from tradeforge_ml.export import export_sweep

pytestmark = pytest.mark.integration

TABLES = (
    "trades",
    "backtest_metrics",
    "sweep_points",
    "backtests",
    "sweeps",
    "strategies",
    "instruments",
)
JAN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    dsn = PostgresSettings().sqlalchemy_dsn
    upgrade("head", dsn=dsn)
    created = create_db_engine(dsn)
    try:
        yield created
    finally:
        created.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with engine.begin() as connection:
        truncate(connection, TABLES)
    db = create_session_factory(engine)()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def a_document(name: str, *, target: float | None = None) -> dict[str, Any]:
    document: dict[str, Any] = {
        "schema_version": "1.0",
        "name": name,
        "timeframe": "H1",
        "setup": {"type": "mme9_breakout", "params": {"side": "long", "breakeven_at_r": None}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }
    if target is not None:
        document["exit"] = {"take_profit": {"type": "fixed_rr", "params": {"rr": target}}}
    return document


def a_trade(run: Backtest, instrument: Instrument, *, hour: int, r: str) -> Trade:
    entered = JAN + dt.timedelta(hours=hour)
    pnl = Decimal(r) * 100
    return Trade(
        backtest_id=run.id,
        instrument_id=instrument.id,
        direction=Side.LONG,
        entry_time=entered,
        entry_price=Decimal("1.10000"),
        volume=Decimal("0.10"),
        stop_loss=Decimal("1.09000"),
        exit_time=entered + dt.timedelta(hours=2),
        exit_price=Decimal("1.10000") + Decimal(r) / 100,
        exit_reason=ExitReason.STOP_LOSS,
        gross_pnl=pnl,
        costs=Decimal("0"),
        net_pnl=pnl,
        r_multiple=Decimal(r),
        mfe_r=Decimal("3"),
        mae_r=Decimal("1"),
    )


def a_sweep(session: Session) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    """Three points of one setup on EURUSD H1: two unmanaged ones that share an entry, and a
    managed one (a 2 R target) whose trades never reach the base."""
    instrument = Instrument(
        symbol="EURUSD",
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    sweep = Sweep(
        entry_ids=["e1"],
        symbols=["EURUSD"],
        timeframes=["H1"],
        date_from=JAN,
        date_to=JAN + dt.timedelta(days=30),
        initial_capital=Decimal("10000"),
        skipped=[],
    )
    session.add_all([instrument, sweep])
    session.flush()
    plan = {
        "a": ({"setup.params.long_average_period": 100}, None, [(1, "2"), (5, "-1")]),
        "b": ({"setup.params.long_average_period": 200}, None, [(1, "2"), (9, "1")]),
        "c": ({"exit.take_profit.params.rr": 2}, 2.0, [(1, "1")]),
    }
    runs: dict[str, uuid.UUID] = {}
    for position, (key, (coordinates, target, trades)) in enumerate(plan.items()):
        strategy = Strategy(definition=a_document(f"point {key}", target=target), version=1)
        session.add(strategy)
        session.flush()
        session.add(
            SweepPoint(
                sweep_id=sweep.id,
                position=position,
                strategy_id=strategy.id,
                entry_id="e1",
                label=f"H1 · {key}",
                coordinates={"timeframe": "H1", **coordinates},
            )
        )
        run = Backtest(
            strategy_id=strategy.id,
            instrument_id=instrument.id,
            sweep_id=sweep.id,
            timeframe="H1",
            date_from=sweep.date_from,
            date_to=sweep.date_to,
            initial_capital=Decimal("10000"),
            cost_model={"type": "none"},
            status=BacktestStatus.DONE,
            engine_version="0.5.0",
        )
        session.add(run)
        session.flush()
        net = sum(Decimal(r) for _hour, r in trades)
        session.add(
            BacktestMetrics(
                backtest_id=run.id,
                net_profit=net * 100,
                gross_profit=max(net * 100, Decimal(0)),
                gross_loss=min(net * 100, Decimal(0)),
                total_trades=len(trades),
                long_trades=len(trades),
                short_trades=0,
                win_rate=Decimal("0.5"),
                max_drawdown_abs=Decimal("100"),
                max_drawdown_pct=Decimal("0.01"),
                max_dd_duration_days=1,
                equity_curve=[],
                net_r=net,
                yearly_r={"2024": str(net)},
            )
        )
        session.add_all(a_trade(run, instrument, hour=hour, r=r) for hour, r in trades)
        runs[key] = run.id
    session.commit()
    return sweep.id, runs


def test_the_base_holds_each_distinct_entry_of_the_unmanaged_runs_once(
    session: Session, tmp_path: Path
) -> None:
    sweep_id, runs = a_sweep(session)

    report = export_sweep(session, sweep_id, tmp_path)

    assert (report.runs, report.unmanaged_runs, report.trades_read) == (3, 2, 4)
    assert (report.events, report.events_disagreeing, report.unreconciled) == (3, 0, [])
    target = tmp_path / f"sweep={sweep_id}"
    events = pq.read_table(target / "events.parquet").to_pylist()
    by_hour = {row["entry_time"].hour: row for row in events}
    assert sorted(by_hour) == [1, 5, 9]  # the managed point's trade at 01:00 is not another one
    shared = by_hour[1]
    assert shared["n_configurations"] == 2
    assert json.loads(shared["configurations"]) == [
        {"setup.params.long_average_period": 100},
        {"setup.params.long_average_period": 200},
    ]
    assert shared["r_multiple"] == 2.0
    assert shared["mfe_r"] == 3.0
    run_rows = pq.read_table(target / "runs.parquet").to_pylist()
    assert {row["run_id"]: row["unmanaged"] for row in run_rows} == {
        str(runs["a"]): True,
        str(runs["b"]): True,
        str(runs["c"]): False,
    }
    manifest = json.loads((target / "manifest.json").read_text())
    assert (manifest["events"], manifest["unreconciled_runs"]) == (3, [])
    assert set(manifest["files"]) == {"events.parquet", "runs.parquet"}


def test_a_run_whose_trades_are_not_its_record_is_named_and_the_command_fails(
    session: Session, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sweep_id, runs = a_sweep(session)
    lost = session.get(BacktestMetrics, runs["a"])
    assert lost is not None
    lost.total_trades = 3  # a trade the run recorded and the database no longer holds
    lost.long_trades = 3
    session.commit()

    code = main(["export", str(sweep_id), "--out", str(tmp_path)])

    assert code == 1
    assert "unreconciled 1" in capsys.readouterr().out
    manifest = json.loads((tmp_path / f"sweep={sweep_id}" / "manifest.json").read_text())
    assert manifest["unreconciled_runs"] == [str(runs["a"])]
