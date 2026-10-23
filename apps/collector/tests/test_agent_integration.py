"""`collect_range` against a real Postgres, with MetaTrader stood in for.

Run locally with:  docker compose up -d  &&  uv run pytest -m integration
"""

import asyncio
import datetime as dt
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine

from tradeforge_collector import agent, mt5_source
from tradeforge_db.collections import create_collection, finish_collection
from tradeforge_db.config import PostgresSettings
from tradeforge_db.migrate import upgrade
from tradeforge_db.models import BacktestStatus, Collection
from tradeforge_db.session import create_db_engine, create_session_factory
from tradeforge_db.testing import truncate

pytestmark = pytest.mark.integration

YEAR = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


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
def requested(engine: Engine) -> Any:
    with engine.begin() as connection:
        truncate(connection, ("backtest_collections", "collections"))
    factory = create_session_factory(engine)

    def request(*, given_up: bool = False) -> str:
        with factory() as session:
            row = create_collection(
                session,
                symbol="EURUSD",
                timeframe="H1",
                date_from=YEAR,
                date_to=YEAR.replace(month=12, day=31),
                asset_class=None,
                years_total=1,
            )
            session.flush()
            if given_up:
                finish_collection(session, row.id, at=YEAR, error="the collector is not running")
            session.commit()
            return str(row.id)

    return request, factory


class _Closed:
    """MetaTrader with the terminal shut: `initialize()` refuses."""

    def __init__(self, **_: Any) -> None:
        return None

    def connect(self) -> "_Closed":
        raise ConnectionError(
            "MetaTrader 5 refused the connection: (-10003, 'IPC initialize failed')"
        )


class _Forbidden:
    def __init__(self, **_: Any) -> None:
        raise AssertionError("an ended collection must not reach MetaTrader")


def test_a_terminal_that_refuses_fails_the_collection_on_its_row(
    requested: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """⚠️ It used to raise before the journal existed, and the row stayed `queued` for good —
    every run waiting on it sat two hours and then failed (26/09)."""
    request, factory = requested
    collection_id = request()
    monkeypatch.setattr(mt5_source, "MT5Source", _Closed)

    assert asyncio.run(agent.collect_range({}, collection_id)) == 0

    with factory() as session:
        row = session.get(Collection, uuid.UUID(collection_id))
        assert row is not None
        assert row.status is BacktestStatus.FAILED
        assert row.error is not None
        assert "not reachable" in row.error
        assert row.finished_at is not None


def test_a_collection_already_given_up_is_not_downloaded(
    requested: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The worker gave it up while the agent was off, and its runs read the disk. Downloading it
    now would turn that `failed` into `done` under runs that never read it."""
    request, factory = requested
    collection_id = request(given_up=True)
    monkeypatch.setattr(mt5_source, "MT5Source", _Forbidden)

    assert asyncio.run(agent.collect_range({}, collection_id)) == 0

    with factory() as session:
        row = session.get(Collection, uuid.UUID(collection_id))
        assert row is not None
        assert row.status is BacktestStatus.FAILED
