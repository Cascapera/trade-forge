"""The heartbeat against a real database: the write, and the transaction it happens in.

`test_heartbeat.py` proves the thread. What it cannot prove is the half that only exists once
Postgres is involved — that the beat lands on the right row, that it does not join the caller's
open transaction, and that a session process holding its own `Session` is not sharing it with
the beating thread.
"""

import datetime as dt
import logging
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.heartbeat import session_heartbeat
from tradeforge_db.live_sessions import STALE_AFTER, is_stale, open_session, reconcile_stale
from tradeforge_db.models import Instrument, LiveSession, LiveSessionStatus, Strategy
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)
TICK = dt.timedelta(milliseconds=10)


def a_live_session(db: Session, **overrides: Any) -> LiveSession:
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
    strategy = Strategy(
        definition={
            "schema_version": "1.0",
            "name": "MA Cross",
            "description": "an example",
            "timeframe": "H1",
        },
        version=1,
    )
    db.add_all([instrument, strategy])
    db.flush()

    values: dict[str, Any] = {
        "strategy_id": strategy.id,
        "instrument_id": instrument.id,
        "timeframe": "H1",
        "initial_capital": Decimal("10000"),
        "cost_model": {"type": "bar_spread"},
        "engine_version": "0.1.0",
        "warmup_bars": 62,
        "at": NOON,
    }
    values.update(overrides)
    row = open_session(db, **values)
    db.commit()
    return row


def factory_of(session_factory: Callable[[], Session]) -> sessionmaker[Session]:
    """The conftest annotates its factory as a plain callable; `session_scope` wants the real
    `sessionmaker`, which is what `create_session_factory` actually returned."""
    return cast(sessionmaker[Session], session_factory)


def test_a_beat_lands_on_the_row(session: Session, session_factory: Callable[[], Session]) -> None:
    live = a_live_session(session)
    assert live.heartbeat_at is None, "open_session must not pre-fill the beat"

    heartbeat = session_heartbeat(
        factory_of(session_factory), live.id, every=STALE_AFTER, now=lambda: NOON
    )
    heartbeat.start()
    heartbeat.stop()

    session.expire(live)
    assert live.heartbeat_at == NOON
    assert heartbeat.beats == 1


def test_the_beat_is_its_own_transaction(
    session: Session, session_factory: Callable[[], Session]
) -> None:
    """⚠️ The reason each beat opens its own scope. A session between trades does not commit —
    an H4 one may not commit for hours — so a beat that enlisted in the caller's transaction
    would be invisible to `reconcile_stale` for exactly as long as the session was quiet, which
    is exactly when liveness is the only thing anyone can read.

    Separated by leaving *uncommitted* work on the caller's session and reading the beat back on
    a different connection.

    ⚠️ The uncommitted work is deliberately on a *different* row. Dirtying the session row
    itself does not test transactions, it tests row locks — which is the test below.
    """
    live = a_live_session(session)
    session_id = live.id

    # Dirty the caller's transaction, and deliberately do not commit it.
    session.add(
        Instrument(
            symbol="GBPUSD",
            name="Pound vs US Dollar",
            asset_class=AssetClass.FOREX,
            currency_base="GBP",
            currency_quote="USD",
            tick_size=Decimal("0.00001"),
            tick_value=Decimal("1"),
            contract_size=Decimal("100000"),
            digits=5,
        )
    )
    session.flush()

    heartbeat = session_heartbeat(
        factory_of(session_factory), session_id, every=STALE_AFTER, now=lambda: NOON
    )
    heartbeat.start()
    heartbeat.stop()

    with factory_of(session_factory)() as reader:
        seen = reader.get(LiveSession, session_id)
        assert seen is not None
        assert seen.heartbeat_at == NOON, "the beat did not commit on its own"
        assert reader.query(Instrument).filter_by(symbol="GBPUSD").one_or_none() is None, (
            "the caller's uncommitted work leaked out"
        )


def test_a_beat_blocked_on_the_row_fails_fast_instead_of_hanging(
    session: Session, session_factory: Callable[[], Session], caplog: pytest.LogCaptureFixture
) -> None:
    """⚠️ **The failure mode this module could have shipped with**, found by a test that was
    trying to prove something else.

    The session process updates `last_bar_time` on this same row every bar, so its main thread
    and the beating thread contend for it *by design*. Without `lock_timeout`, a beat arriving
    while the main thread holds an open transaction on the row waits for it — and it waits
    inside the beat, where `stop()` cannot reach. Measured before the fix: the thread stuck, and
    `stop()` gave up after five seconds. A heartbeat that hangs is a healthy session reconciled
    to `failed`, which is the exact reading this module exists to prevent.

    So a blocked beat is a *failed* beat: counted, logged, and tried again on the next tick.
    """
    live = a_live_session(session)
    session_id = live.id

    # The main thread's transaction, holding the row and not committing — a session mid-bar.
    live.last_bar_time = NOON - dt.timedelta(hours=4)
    session.flush()

    heartbeat = session_heartbeat(
        factory_of(session_factory),
        session_id,
        every=STALE_AFTER,
        now=lambda: NOON,
        lock_timeout=dt.timedelta(milliseconds=100),
    )
    with caplog.at_level(logging.CRITICAL):
        heartbeat.start()
        started = time.monotonic()
        heartbeat.stop()
        elapsed = time.monotonic() - started

    assert heartbeat.failures == 1, "the beat should have given up on the lock, not taken it"
    assert heartbeat.beats == 0
    assert elapsed < 2.0, f"stop() took {elapsed:.1f}s; the beat hung on the lock"

    session.rollback()


def test_beating_keeps_a_session_out_of_the_reconciliation(
    session: Session, session_factory: Callable[[], Session]
) -> None:
    """End to end, and the point of the whole module: a session whose main thread is doing
    nothing is still not stale, because something else is beating for it.

    ⚠️ `now` is a clock the test controls, so this asserts a rule rather than a race. The beat
    is stamped at `NOON` and the reconciliation is run a full `STALE_AFTER` later minus a
    second — the same second the unit tests use, on the safe side of the boundary `is_stale`
    pins.
    """
    live = a_live_session(session)
    session_id = live.id

    heartbeat = session_heartbeat(
        factory_of(session_factory), session_id, every=TICK, now=lambda: NOON
    )
    with heartbeat:
        # The main thread is blocked, as it would be inside a stream read. Waited on rather than
        # assumed: `beats >= 0` is true of a thread that never ran, which is the whole thing
        # this file is here to rule out.
        deadline = time.monotonic() + 5.0
        while heartbeat.beats < 1 and time.monotonic() < deadline:
            time.sleep(0.002)
        assert heartbeat.beats >= 1, "nothing beat while the main thread was blocked"

    just_inside = NOON + STALE_AFTER - dt.timedelta(seconds=1)
    assert not is_stale(heartbeat_at=NOON, started_at=NOON, now=just_inside)

    with factory_of(session_factory)() as reconciler:
        marked = reconcile_stale(reconciler, now=just_inside)
        reconciler.commit()

    assert [row.id for row in marked] == []

    session.expire_all()
    refreshed = session.get(LiveSession, session_id)
    assert refreshed is not None
    assert refreshed.status is LiveSessionStatus.RUNNING


def test_a_session_that_stopped_beating_is_reconciled(
    session: Session, session_factory: Callable[[], Session]
) -> None:
    """⚠️ The separating half. Without it the test above passes against a reconciliation that
    never marks anything — and a heartbeat proving nothing would look identical."""
    live = a_live_session(session)
    session_id = live.id

    heartbeat = session_heartbeat(
        factory_of(session_factory), session_id, every=STALE_AFTER, now=lambda: NOON
    )
    heartbeat.start()
    heartbeat.stop()

    with factory_of(session_factory)() as reconciler:
        marked = reconcile_stale(reconciler, now=NOON + STALE_AFTER)
        reconciler.commit()

    assert [row.id for row in marked] == [session_id]


def test_a_beat_for_a_session_that_does_not_exist_is_not_an_error(
    session_factory: Callable[[], Session],
) -> None:
    """`beat` returns quietly when the row is gone, and the thread must not treat that as a
    failure to retry for ever. A session deleted while its process winds down is untidy, not
    broken."""
    heartbeat = session_heartbeat(
        factory_of(session_factory), uuid.uuid4(), every=STALE_AFTER, now=lambda: NOON
    )
    heartbeat.start()
    heartbeat.stop()

    assert heartbeat.failures == 0
    assert heartbeat.beats == 1
