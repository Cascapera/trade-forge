"""Fixtures for the API tests that need a real Postgres.

Only the integration tests ask for these; the unit tests never do, so `uv run pytest` still
runs with no Docker anywhere. Mirrors the `packages/db` conftest — one migrated database per
session, truncated to a known state before each test.
"""

import datetime as dt
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_collector import write_candles
from tradeforge_collector.storage import coverage, dataset_path
from tradeforge_db.instruments import upsert_dataset
from tradeforge_db.migrate import upgrade
from tradeforge_db.models import Instrument
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_db.testing import truncate
from tradeforge_engine.domain import Candle

TABLES_CHILD_FIRST = (
    # ⚠️ First of all: it points at `backtests` and at `collections`, and both are emptied below.
    "backtest_collections",
    # ⚠️ Append-only, and the only table here whose trigger has to be lifted to empty it
    # at all — see `tradeforge_db.testing.truncate`. Listed because a row left behind is
    # a parent the next test could attach to, exactly like `live_sessions`.
    "order_audit",
    "trades",
    # ⚠️ Before `strategies` and `instruments`, which it points at, and after `trades`, which
    # points at it. Missing here until the first API test wrote one — `packages/db`'s copy has
    # had it since the table existed, and the two lists drifted. A session row left behind is
    # not inert: it is a parent the next test's trades could attach to.
    "live_sessions",
    "backtest_metrics",
    "backtests",
    "baskets",
    # Named rather than left to the CASCADE that would reach it from `strategies` anyway. A
    # tuple whose name says "child first" and which in practice relies on a cascade is one that
    # breaks the day the cascade changes — and it breaks as a test leaving rows behind for the
    # next one, which is the hardest kind of failure to attribute.
    "studies",
    # The third grouping, beside `baskets` and `studies`, and here for the reason they are:
    # after `backtests`, which points at it. A sweep row left behind is not inert — it is a
    # parent the next test's runs could attach to. ⚠️ Unlike `catalog_entries` below it, this
    # table holds **no** foreign key to `strategies`: the strategy ids in `points` are text
    # inside JSONB, which is the trade `rev_0018` makes so a finished sweep stays readable.
    "sweeps",
    # ⚠️ Before `strategies`, which it points at with a RESTRICT foreign key — so a row
    # left behind does not merely linger, it makes the next test unable to empty
    # `strategies` at all. Added with the table; the two lists above it drifted once
    # before for exactly this reason (`live_sessions`).
    "catalog_entries",
    "strategies",
    "datasets",
    "instruments",
    # ⚠️ Not a child of anything, and listed anyway. `broker_symbols` deliberately has no
    # foreign key — that is what lets a sync replace it wholesale — so no CASCADE reaches it
    # and a test that syncs would leak its rows into the next one.
    "broker_symbols",
    # The same argument, and the same absence of a foreign key. `symbol_history` is keyed on
    # (symbol, timeframe), so a row left behind is not inert — it is the answer the next test
    # reads. It was missed when the table was added, which is what this comment is for.
    "symbol_history",
    "collections",
)


@pytest.fixture(scope="session")
def settings() -> Settings:
    return Settings()


@pytest.fixture(scope="session")
def migrated_engine(settings: Settings) -> Iterator[Engine]:
    """A database at head, migrated once for the whole session."""
    upgrade("head", dsn=settings.sqlalchemy_dsn)
    engine = create_db_engine(settings.sqlalchemy_dsn)
    try:
        yield engine
    finally:
        engine.dispose()


@pytest.fixture
def session_factory(migrated_engine: Engine) -> Callable[[], Session]:
    """A session factory over an emptied database. Truncating before the test means a failure
    leaves its rows behind to inspect, and the next test still starts from nothing."""
    with migrated_engine.begin() as connection:
        truncate(connection, TABLES_CHILD_FIRST)
    return create_session_factory(migrated_engine)


@pytest.fixture
def session(session_factory: Callable[[], Session]) -> Iterator[Session]:
    db = session_factory()
    try:
        yield db
    finally:
        db.rollback()
        db.close()


Collect = Callable[[Path, str, str, list[Candle]], None]
Index = Callable[..., None]


def _index(  # noqa: PLR0913 — keyword-only; these are the columns of the row
    session_factory: Callable[[], Session],
    *,
    symbol: str,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
    candle_count: int,
    parquet_path: str,
) -> None:
    with session_factory() as session:
        instrument_id = session.scalars(
            select(Instrument.id).where(Instrument.symbol == symbol)
        ).one()
        upsert_dataset(
            session,
            instrument_id=instrument_id,
            timeframe=timeframe,
            date_from=date_from,
            date_to=date_to,
            candle_count=candle_count,
            parquet_path=parquet_path,
        )
        session.commit()


@pytest.fixture
def collected(session_factory: Callable[[], Session]) -> Collect:
    """Write bars **and index them**, the two steps a real collection always takes together.

    ⚠️ Since PR-262 a launch asks the `datasets` index whether the window holds any candle, so
    bars written to Parquet alone are bars no run can be launched over — which is also true in
    production, where the collector catalogues from the disk right after writing. The extent is
    read back from the files for the same reason it is there (`storage.coverage`). The symbol's
    instrument must already exist.
    """

    def collect(root: Path, symbol: str, timeframe: str, candles: list[Candle]) -> None:
        write_candles(root, symbol, timeframe, candles)
        on_disk = coverage(root, symbol, timeframe)
        assert on_disk is not None
        _index(
            session_factory,
            symbol=symbol,
            timeframe=timeframe,
            date_from=on_disk.date_from,
            date_to=on_disk.date_to,
            candle_count=on_disk.candle_count,
            parquet_path=dataset_path(root, symbol, timeframe),
        )

    return collect


@pytest.fixture
def indexed(session_factory: Callable[[], Session]) -> Index:
    """Index a pair as collected over a wide window, with **no bars behind it**.

    For tests that launch a run and never let a worker read it — the launch only asks the index.
    ⚠️ A test whose worker reads candles wants `collected`: this index points at nothing.
    """

    def index(symbol: str, timeframe: str = "H1") -> None:
        _index(
            session_factory,
            symbol=symbol,
            timeframe=timeframe,
            date_from=dt.datetime(2000, 1, 1, tzinfo=dt.UTC),
            date_to=dt.datetime(2099, 12, 31, tzinfo=dt.UTC),
            candle_count=1,
            parquet_path=f"{symbol}/{timeframe}",
        )

    return index
