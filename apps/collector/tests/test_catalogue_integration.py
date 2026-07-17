"""The catalogue, against a real Postgres.

The Parquet is the product; the `instruments` and `datasets` rows are how the rest of
the system finds out it exists. Both writes are upserts, and the property that matters
is the one a nightly job depends on: run the backfill twice and the database looks the
same as after running it once.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import datetime as dt
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from tradeforge_collector.backfill import backfill
from tradeforge_collector.synthetic import SyntheticSource
from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import upgrade
from tradeforge_db.models import Dataset, Instrument
from tradeforge_db.session import create_db_engine, create_session_factory

pytestmark = pytest.mark.integration

JAN = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
FEB = dt.datetime(2024, 2, 1, tzinfo=dt.UTC)


@pytest.fixture(scope="module")
def engine() -> Iterator[Engine]:
    dsn = PostgresSettings().sqlalchemy_dsn
    upgrade("head", dsn=dsn)

    db_engine = create_db_engine(dsn)
    try:
        yield db_engine
    finally:
        db_engine.dispose()


@pytest.fixture
def session(engine: Engine) -> Iterator[Session]:
    with engine.begin() as connection:
        connection.execute(text("TRUNCATE datasets, instruments RESTART IDENTITY CASCADE"))

    db = create_session_factory(engine)()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


def test_a_backfill_registers_the_instrument_and_the_dataset(
    session: Session, tmp_path: Path
) -> None:
    report = backfill(
        SyntheticSource(),
        root=tmp_path,
        symbol="EURUSD",
        timeframe="H1",
        start=JAN,
        end=FEB,
        session=session,
    )
    session.commit()

    instrument = session.execute(
        select(Instrument).where(Instrument.symbol == "EURUSD")
    ).scalar_one()
    dataset = session.execute(select(Dataset)).scalar_one()

    assert instrument.tick_size == Decimal("0.00001")
    assert instrument.digits == 5
    assert dataset.instrument_id == instrument.id
    assert dataset.timeframe == "H1"
    assert dataset.candle_count == report.candles
    assert dataset.parquet_path == report.parquet_path
    # The catalogue must agree with the file, not with what was asked for: the first
    # candle of January lands after the 1st, because the market was shut.
    assert dataset.date_from == report.date_from
    assert dataset.date_to == report.date_to


def test_running_the_backfill_twice_updates_the_catalogue_instead_of_duplicating_it(
    session: Session, tmp_path: Path
) -> None:
    """The property a nightly job depends on. The unique constraint from PR-101 enforces it."""
    for _ in range(2):
        backfill(
            SyntheticSource(),
            root=tmp_path,
            symbol="EURUSD",
            timeframe="H1",
            start=JAN,
            end=FEB,
            session=session,
        )
        session.commit()

    assert len(session.execute(select(Instrument)).scalars().all()) == 1
    assert len(session.execute(select(Dataset)).scalars().all()) == 1


def test_two_timeframes_of_one_symbol_are_two_datasets_and_one_instrument(
    session: Session, tmp_path: Path
) -> None:
    for timeframe in ("H1", "H4"):
        backfill(
            SyntheticSource(),
            root=tmp_path,
            symbol="EURUSD",
            timeframe=timeframe,
            start=JAN,
            end=FEB,
            session=session,
        )
    session.commit()

    assert len(session.execute(select(Instrument)).scalars().all()) == 1
    assert len(session.execute(select(Dataset)).scalars().all()) == 2


def test_a_stock_and_a_pair_land_side_by_side(session: Session, tmp_path: Path) -> None:
    """Different tick arithmetic, same table — the multi-asset path, end to end."""
    for symbol in ("EURUSD", "AAPL"):
        backfill(
            SyntheticSource(),
            root=tmp_path,
            symbol=symbol,
            timeframe="H1",
            start=JAN,
            end=FEB,
            session=session,
        )
    session.commit()

    aapl = session.execute(select(Instrument).where(Instrument.symbol == "AAPL")).scalar_one()

    assert aapl.currency_base is None
    assert aapl.tick_size == Decimal("0.01")
