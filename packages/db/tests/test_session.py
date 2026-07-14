"""Engines and sessions, built without ever dialling out.

`create_engine` is lazy: it resolves the URL and builds a pool, and connects on first
use. That is what lets these run in the unit job, with no Postgres in sight.
"""

from pathlib import Path

import pytest
from sqlalchemy import Engine

from tradeforge_db.session import create_db_engine, create_session_factory

DSN = "postgresql+psycopg://trader:s3cret@db.internal:6543/forge"


def test_engine_is_built_from_an_explicit_dsn() -> None:
    engine = create_db_engine(DSN)

    assert isinstance(engine, Engine)
    assert engine.url.host == "db.internal"
    assert engine.url.database == "forge"
    assert engine.url.drivername == "postgresql+psycopg"


def test_engine_falls_back_to_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("POSTGRES_HOST", "from-env")
    monkeypatch.setenv("POSTGRES_PASSWORD", "s3cret")

    assert create_db_engine().url.host == "from-env"


def test_sessions_survive_their_commit() -> None:
    """`expire_on_commit=False`: a worker keeps reading a backtest after saving it.

    With the default, every attribute touched after the commit fires a fresh SELECT —
    and once the session is closed, raises instead.
    """
    factory = create_session_factory(create_db_engine(DSN))

    assert factory.kw["expire_on_commit"] is False
