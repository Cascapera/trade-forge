"""Engines and sessions.

`create_engine` does not connect — it builds a lazy pool that dials out on first
use. That is what lets the unit tests assert how a URL was assembled without a
database anywhere in sight, and it is why a misconfigured DSN surfaces at the first
query rather than at import time.
"""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_db.config import PostgresSettings


def create_db_engine(dsn: str | None = None, *, echo: bool = False) -> Engine:
    """Build the engine. Reads the environment when no DSN is passed."""
    url = dsn or PostgresSettings().sqlalchemy_dsn
    return create_engine(
        url,
        echo=echo,
        # A pooled connection that the database closed underneath us (a restart, an
        # idle timeout on a proxy) is handed out dead. `pre_ping` spends one trivial
        # round-trip to find that out before the caller's query does.
        pool_pre_ping=True,
        future=True,
    )


def create_session_factory(engine: Engine) -> sessionmaker[Session]:
    """A session factory bound to an engine.

    `expire_on_commit=False` because our objects outlive their transaction: a worker
    commits a backtest and keeps reading it to publish progress. The default would
    turn every attribute access after the commit into a fresh SELECT — or an error,
    once the session is closed.
    """
    return sessionmaker(bind=engine, expire_on_commit=False, class_=Session)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """A transaction that commits on success and rolls back on any exception.

    The rollback is the point. A half-written backtest — trades in, metrics missing —
    is worse than no backtest at all, because it looks like a result.
    """
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
