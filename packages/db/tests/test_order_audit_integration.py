"""The audit trail, against the database that enforces it.

Every claim in this file is about what **Postgres** refuses, not about what the application
declines to do. That is the entire point of the table: a rule that lives in the code that writes
the record is a rule the code that writes the record can change its mind about.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from tradeforge_db.live_sessions import open_session
from tradeforge_db.models import Instrument, LiveSession, OrderAudit, OrderAuditStatus, Strategy
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def an_entry(session: Session, **overrides: Any) -> OrderAudit:
    values: dict[str, Any] = {
        "client_id": "zone-42",
        "status": OrderAuditStatus.SENT,
        "request": {"symbol": "EURUSD", "volume": "0.10", "side": "long"},
        "requested_at": NOON,
    }
    values.update(overrides)
    row = OrderAudit(**values)
    session.add(row)
    session.commit()
    return row


def a_session_row(session: Session) -> LiveSession:
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
    strategy = Strategy(definition={"schema_version": "1.0", "name": "MA"}, version=1)
    session.add_all([instrument, strategy])
    session.flush()
    row = open_session(
        session,
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        engine_version="0.1.0",
        warmup_bars=10,
        at=NOON,
    )
    session.commit()
    return row


# --------------------------------------------------------------------------------------------
# Append-only, enforced by the database
# --------------------------------------------------------------------------------------------


def test_an_entry_can_be_written(session: Session) -> None:
    """The separating half. Without it, a table that refused *everything* would pass every
    refusal below."""
    row = an_entry(session)

    stored = session.execute(select(OrderAudit)).scalars().one()
    assert stored.id == row.id
    assert stored.status is OrderAuditStatus.SENT
    assert stored.request["volume"] == "0.10"


def test_an_entry_cannot_be_updated(session: Session) -> None:
    """⚠️ **The reason this table exists.** A record that can be corrected after a bad day is a
    record whose author can correct it after a bad day — and the author is the person with the
    most reason to want to."""
    an_entry(session)

    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("UPDATE order_audit SET status = 'filled'"))
    session.rollback()

    assert session.execute(select(OrderAudit)).scalars().one().status is OrderAuditStatus.SENT


def test_an_entry_cannot_be_deleted(session: Session) -> None:
    an_entry(session)

    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("DELETE FROM order_audit"))
    session.rollback()

    assert len(session.execute(select(OrderAudit)).scalars().all()) == 1


def test_the_table_cannot_be_truncated(session: Session) -> None:
    """⚠️ Guarded **separately**, and it has to be. `TRUNCATE` is not a `DELETE` as far as
    Postgres is concerned — a `FOR EACH ROW` trigger never sees it — so a table protected only
    against `DELETE` can still be emptied in a single statement.

    This project has already met that from the other side: the integration fixtures truncate
    eleven tables, and this is the one that must survive a run pointed at the wrong database.
    """
    an_entry(session)

    with pytest.raises(DBAPIError, match="append-only"):
        session.execute(text("TRUNCATE order_audit"))
    session.rollback()

    assert len(session.execute(select(OrderAudit)).scalars().all()) == 1


def test_the_refusal_raises_rather_than_silently_dropping_the_statement(
    session: Session,
) -> None:
    """⚠️ Separates `RAISE EXCEPTION` from `RETURN NULL`.

    A `BEFORE` trigger that returns NULL **silently drops** the statement: the UPDATE reports
    success, changes nothing, and the caller believes it worked. An audit trail that lies about
    being immutable is worse than one that is not immutable at all — the second at least tells
    the truth about itself.
    """
    an_entry(session)

    with pytest.raises(DBAPIError) as caught:
        session.execute(text("UPDATE order_audit SET reason = 'tidied'"))
    session.rollback()

    assert "rev_0015" in str(caught.value), "the refusal does not say where the rule lives"


def test_deleting_a_session_cannot_take_its_audit_trail_with_it(session: Session) -> None:
    """⚠️ `RESTRICT`, not `CASCADE`. That is the one deletion an audit trail exists to survive:
    the row that explains an incident must not disappear because somebody tidied up the session
    the incident happened in."""
    live = a_session_row(session)
    an_entry(session, live_session_id=live.id)

    with pytest.raises(IntegrityError):
        session.execute(text("DELETE FROM live_sessions WHERE id = :id"), {"id": str(live.id)})
    session.rollback()

    assert len(session.execute(select(OrderAudit)).scalars().all()) == 1


# --------------------------------------------------------------------------------------------
# What a row is allowed to say
# --------------------------------------------------------------------------------------------


def test_an_order_refused_before_any_session_existed_is_still_recorded(session: Session) -> None:
    """⚠️ `live_session_id` is nullable on purpose. A kill switch engaged at start-up refuses an
    order before a session exists, and a foreign key that demanded a parent would make the
    executor choose between inventing a false one and writing nothing."""
    an_entry(
        session,
        live_session_id=None,
        status=OrderAuditStatus.REFUSED,
        reason="kill switch engaged (file:/etc/tradeforge/KILL)",
    )

    stored = session.execute(select(OrderAudit)).scalars().one()
    assert stored.live_session_id is None
    assert "kill switch" in (stored.reason or "")


@pytest.mark.parametrize("status", [OrderAuditStatus.REFUSED, OrderAuditStatus.ERROR])
def test_a_refusal_without_a_reason_is_refused(session: Session, status: OrderAuditStatus) -> None:
    """ "Refused" with no rule named leaves an operator unable to tell a kill switch from a lot
    one step too large — two situations with opposite responses."""
    with pytest.raises(IntegrityError, match="a_refusal_says_why"):
        an_entry(session, status=status, reason=None)
    session.rollback()


@pytest.mark.parametrize(
    "status", [OrderAuditStatus.SENT, OrderAuditStatus.FILLED, OrderAuditStatus.PARTIAL]
)
def test_an_outcome_that_is_not_a_refusal_may_not_carry_a_reason(
    session: Session, status: OrderAuditStatus
) -> None:
    """⚠️ The separating half of the CHECK, and it is not symmetry for its own sake. A `filled`
    row carrying a refusal reason would read, to anybody scanning, exactly like an order that
    was stopped — and the two are opposite facts."""
    with pytest.raises(IntegrityError, match="a_refusal_says_why"):
        an_entry(session, status=status, reason="something happened")
    session.rollback()


def test_a_row_cannot_resolve_before_it_was_requested(session: Session) -> None:
    """Time running backwards on an audit row is either a clock problem or a fabricated record,
    and both are worth refusing at the boundary rather than explaining later."""
    with pytest.raises(IntegrityError, match="audit_resolves_after_it_is_requested"):
        an_entry(session, resolved_at=NOON - dt.timedelta(seconds=1))
    session.rollback()


def test_an_unknown_status_is_refused(session: Session) -> None:
    """The enum is a CHECK in the database, not only a Python type. A row written by anything
    that is not this ORM — a migration, a script, psql — has to meet the same rule."""
    with pytest.raises(IntegrityError, match="order_audit_status"):
        session.execute(
            text(
                "INSERT INTO order_audit (client_id, status, request, requested_at) "
                "VALUES ('x', 'tidied-up', '{}', now())"
            )
        )
    session.rollback()


def test_the_request_is_stored_whole_rather_than_projected(session: Session) -> None:
    """⚠️ A projection records the fields somebody thought of. The question an incident asks is
    almost always about a field nobody thought of — so the document goes in verbatim."""
    payload = {
        "symbol": "EURUSD",
        "volume": "0.10",
        "magic": 770077,
        "comment": "zone-42",
        "an_unforeseen_field": {"nested": [1, 2, 3]},
    }

    an_entry(session, request=payload)

    assert session.execute(select(OrderAudit)).scalars().one().request == payload


def test_two_outcomes_for_one_order_are_two_rows(session: Session) -> None:
    """⚠️ There is no `requested` status, and this is why. A row saying "I picked this up" would
    have to be *updated* when the outcome arrived — which the trigger forbids. So an order that
    is sent and then filled leaves two rows sharing a `client_id`, and the correlation is the
    id the strategy gave it (ADR-0014), not a row somebody mutates twice."""
    an_entry(session, client_id="zone-42", status=OrderAuditStatus.SENT)
    an_entry(
        session,
        client_id="zone-42",
        status=OrderAuditStatus.FILLED,
        resolved_at=NOON + dt.timedelta(seconds=2),
    )

    rows = session.execute(select(OrderAudit).order_by(OrderAudit.requested_at)).scalars().all()
    assert [row.status for row in rows] == [OrderAuditStatus.SENT, OrderAuditStatus.FILLED]
    assert {row.client_id for row in rows} == {"zone-42"}
    assert rows[0].id != rows[1].id


def test_an_audit_row_needs_no_uuid_from_the_caller(session: Session) -> None:
    """`gen_random_uuid()` server-side. The executor writing an audit row must not have to be
    holding a working uuid library to record that something went wrong."""
    session.execute(
        text(
            "INSERT INTO order_audit (client_id, status, request, requested_at) "
            "VALUES ('x', 'sent', '{}', now())"
        )
    )
    session.commit()

    assert isinstance(session.execute(select(OrderAudit)).scalars().one().id, uuid.UUID)


def test_the_foreign_key_protects_the_trail_even_with_the_trigger_lifted(
    session: Session,
) -> None:
    """⚠️ Two guards, and they have to be **independent** — which the obvious test cannot see.

    With `ondelete="CASCADE"` the session delete cascades into `order_audit`, the append-only
    trigger refuses *that*, and the statement fails anyway. So a test that only asserts "the
    delete failed and the row survived" passes against both spellings, and the `RESTRICT` looks
    redundant. It is not: the two protect against different things, and the moment they come
    apart is the moment somebody lifts the trigger — which this project's own test fixtures do,
    every single test, to be able to empty the schema at all (`tradeforge_db.testing.truncate`).

    So the trigger is lifted here on purpose, and the foreign key has to hold on its own.
    """
    live = a_session_row(session)
    an_entry(session, live_session_id=live.id)

    session.execute(text("ALTER TABLE order_audit DISABLE TRIGGER USER"))
    try:
        with pytest.raises(IntegrityError, match="fk_order_audit_live_session_id"):
            session.execute(text("DELETE FROM live_sessions WHERE id = :id"), {"id": str(live.id)})
        session.rollback()
    finally:
        session.execute(text("ALTER TABLE order_audit ENABLE TRIGGER USER"))
        session.commit()

    assert len(session.execute(select(OrderAudit)).scalars().all()) == 1
