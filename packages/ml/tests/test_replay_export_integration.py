"""Choosing the runs to replay from real Postgres (12/10)."""

import datetime as dt
import uuid
from collections.abc import Iterator
from decimal import Decimal

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import upgrade
from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy, Sweep, SweepPoint
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_db.testing import truncate
from tradeforge_engine.domain import AssetClass
from tradeforge_ml.replay_export import jobs_of

pytestmark = pytest.mark.integration

TABLES = ("backtest_metrics", "sweep_points", "backtests", "sweeps", "strategies", "instruments")
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


def managed_document(rr: int) -> dict[str, object]:
    return {
        "schema_version": "1.0",
        "name": f"turn rr {rr}",
        "timeframe": "H1",
        "setup": {"type": "mme9_turn", "params": {"side": "both", "breakeven_at_r": None}},
        "exit": {"take_profit": {"type": "risk_multiple", "params": {"rr": rr}}},
        "risk": {"sizing": {"type": "percent_risk", "params": {"percent": 1.0}}},
    }


def test_the_run_standing_for_an_entry_does_not_depend_on_the_query_plan(
    session: Session,
) -> None:
    """⚠️ A sweep writes all its runs at once, so `created_at` ties across the whole of it: the
    run standing for an entry with no unmanaged point was whichever row Postgres returned first.
    Three managed runs of one entry, inserted from the largest id down: the smallest id stands."""
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
    written_at = dt.datetime(2026, 10, 11, 11, 51, tzinfo=dt.UTC)
    ids = [uuid.UUID(int=n) for n in (3, 2, 1)]
    for position, (run_id, rr) in enumerate(zip(ids, (3, 5, 7), strict=True)):
        strategy = Strategy(definition=managed_document(rr), version=1)
        session.add(strategy)
        session.flush()
        session.add(
            SweepPoint(
                sweep_id=sweep.id,
                position=position,
                strategy_id=strategy.id,
                entry_id="e1",
                label=f"H1 · rr {rr}",
                coordinates={"timeframe": "H1", "exit.take_profit.params.rr": rr},
            )
        )
        session.add(
            Backtest(
                id=run_id,
                strategy_id=strategy.id,
                instrument_id=instrument.id,
                sweep_id=sweep.id,
                timeframe="H1",
                date_from=sweep.date_from,
                date_to=sweep.date_to,
                initial_capital=Decimal("10000"),
                cost_model={"type": "none"},
                status=BacktestStatus.DONE,
                engine_version="0.6.0",
                created_at=written_at,
            )
        )
        session.flush()
    session.commit()

    (job,) = jobs_of(session, sweep.id)

    assert job.run_id == str(uuid.UUID(int=1))
    assert job.definition["exit"] == {"take_profit": None}
