"""The audit write, against the CHECKs that decide what a row may say.

Every outcome the router can produce has to become a row the database accepts. That is not a
formality: the two failure statuses carry a reason and the two success statuses must not, and
the constraint is an exclusive or in **both** directions — so a status decided in one place and
a reason in another is a row Postgres refuses. The one event most worth recording would be the
one that goes unrecorded.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.live_sessions import BEAT_EVERY, STALE_AFTER, beat, finish_session, open_session
from tradeforge_db.models import Instrument, LiveSession, OrderAudit, OrderAuditStatus, Strategy
from tradeforge_engine.domain import AssetClass, OrderRequest, Side, SignalKind
from tradeforge_executor.gateway import Placement
from tradeforge_executor.ledger import record, session_is_alive
from tradeforge_executor.router import Outcome
from tradeforge_executor.wire import WireOrder

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def an_order(session_id: str = "", **overrides: Any) -> WireOrder:
    values: dict[str, Any] = {
        "symbol": "EURUSD",
        "side": Side.LONG,
        "intent": SignalKind.ENTRY,
        "volume": Decimal("0.10"),
        "decided_at": NOON,
        "stop_loss": Decimal("1.09500"),
    }
    values.update(overrides)
    return WireOrder(
        client_id="zone-42",
        session_id=session_id or str(uuid.uuid4()),
        request=OrderRequest(**values),
    )


def a_placement(*, accepted: bool = True, volume: str = "0.10", retcode: int = 10009) -> Placement:
    return Placement(
        accepted=accepted,
        ticket=99 if accepted else None,
        filled_volume=Decimal(volume),
        price=Decimal("1.10000") if accepted else None,
        retcode=retcode,
        comment="done" if accepted else "no money",
        raw={"retcode": retcode, "comment": "done" if accepted else "no money"},
    )


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
    strategy = Strategy(definition={"schema_version": "1.0", "name": "MA"}, version=1)
    db.add_all([instrument, strategy])
    db.flush()
    values: dict[str, Any] = {
        "strategy_id": strategy.id,
        "instrument_id": instrument.id,
        "timeframe": "H1",
        "initial_capital": Decimal("10000"),
        "cost_model": {"type": "none"},
        "engine_version": "0.1.0",
        "warmup_bars": 10,
        "at": NOON,
    }
    values.update(overrides)
    row = open_session(db, **values)
    db.commit()
    return row


def stored(db: Session) -> OrderAudit:
    return db.execute(select(OrderAudit)).scalars().one()


# --------------------------------------------------------------------------------------------
# Every outcome the router can produce becomes a row the database accepts
# --------------------------------------------------------------------------------------------


def test_a_sent_order_is_recorded_without_a_reason(session: Session) -> None:
    order = an_order()
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=True,
        reason=None,
        placement=a_placement(),
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    row = stored(session)
    assert row.status is OrderAuditStatus.SENT
    assert row.reason is None
    assert row.response == {"retcode": 10009, "comment": "done"}


def test_a_refusal_is_recorded_with_the_rule_that_refused_it(session: Session) -> None:
    order = an_order()
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=False,
        reason="kill switch engaged (file:/etc/tradeforge/KILL)",
        placement=None,
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    row = stored(session)
    assert row.status is OrderAuditStatus.REFUSED
    assert row.reason is not None
    assert "kill switch" in row.reason
    assert row.response is None, "nothing was sent, so the venue said nothing"


def test_an_order_the_venue_rejected_is_an_error_with_a_reason_built_from_it(
    session: Session,
) -> None:
    """⚠️ **The row this file exists for.** An order this machine *allowed* and the venue then
    rejected carries `outcome.reason is None` — `allowed` and `sent` are different questions — so
    a status decided in one place and a reason in another produces `error` with no reason, which
    `a_refusal_says_why` rejects outright.

    The insert would fail, the audit row would be lost, and the single event most worth recording
    would be the one that went unrecorded. Caught here rather than on the first real rejection
    from a broker.
    """
    order = an_order()
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=True,
        reason=None,
        placement=a_placement(accepted=False, retcode=10019),
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    row = stored(session)
    assert row.status is OrderAuditStatus.ERROR
    assert row.reason is not None
    assert "10019" in row.reason, "the reason does not say what the venue answered"
    assert row.response == {"retcode": 10019, "comment": "no money"}


def test_a_short_fill_is_partial_rather_than_filled(session: Session) -> None:
    """⚠️ Its own status, not a `sent` with a smaller number. A partial fill leaves the rest of
    the order somewhere, and reading it as complete is how a position ends up half the size a
    strategy believes it has."""
    order = an_order(volume=Decimal("0.10"))
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=True,
        reason=None,
        placement=a_placement(volume="0.04"),
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    assert stored(session).status is OrderAuditStatus.PARTIAL


def test_a_full_fill_is_not_mistaken_for_a_partial_one(session: Session) -> None:
    """The separating half. Without it, a rule that called everything partial would pass above."""
    order = an_order(volume=Decimal("0.10"))
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=True,
        reason=None,
        placement=a_placement(volume="0.10"),
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    assert stored(session).status is OrderAuditStatus.SENT


def test_the_request_recorded_is_the_one_that_crossed_the_wire(session: Session) -> None:
    """⚠️ The wire encoding, not a second description of the order. An audit row that recorded a
    prettier version of the request would be evidence about a different order — and the whole
    point is being able to compare what was asked for with what the venue was told."""
    order = an_order(limit_price=Decimal("1.09800"), reason="zone-42 armed")
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=True,
        reason=None,
        placement=a_placement(),
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    request = stored(session).request
    assert request["limit_price"] == "1.09800", "the price went through a float"
    assert request["client_id"] == "zone-42"
    assert request["volume"] == "0.10"


def test_an_order_from_an_unknown_session_is_still_recorded(session: Session) -> None:
    """⚠️ The link is dropped, never the row. A foreign key pointing at a session that is not on
    file would fail the insert — and losing the audit row is strictly worse than losing the link:
    the row still says which `client_id` was refused and why."""
    order = an_order(session_id=str(uuid.uuid4()))
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=False,
        reason="the session is gone",
        placement=None,
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    row = stored(session)
    assert row.live_session_id is None
    assert row.client_id == "zone-42"


def test_an_order_from_a_known_session_carries_the_link(session: Session) -> None:
    live = a_live_session(session)
    order = an_order(session_id=str(live.id))
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=True,
        reason=None,
        placement=a_placement(),
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    assert stored(session).live_session_id == live.id


def test_a_session_id_that_is_not_a_uuid_does_not_break_the_write(session: Session) -> None:
    """Malformed input on a queue is a row to record, not a loop to crash."""
    order = an_order(session_id="not-a-uuid")
    outcome = Outcome(
        client_id=order.client_id,
        session_id=order.session_id,
        allowed=False,
        reason="unreadable session id",
        placement=None,
    )

    record(session, order, outcome, now=NOON)
    session.commit()

    assert stored(session).live_session_id is None


# --------------------------------------------------------------------------------------------
# Is the session that placed this order still there?
# --------------------------------------------------------------------------------------------


def test_a_beating_session_is_alive(session: Session) -> None:
    live = a_live_session(session)
    beat(session, live.id, at=NOON)
    session.commit()

    assert session_is_alive(session, str(live.id), now=NOON + BEAT_EVERY) is True


def test_a_session_that_stopped_beating_is_not_alive(session: Session) -> None:
    """⚠️ The scenario that matters: an order sitting in the queue, placed by a session that has
    since died. Sending it would be acting on behalf of something that no longer exists."""
    live = a_live_session(session)
    beat(session, live.id, at=NOON)
    session.commit()

    assert session_is_alive(session, str(live.id), now=NOON + STALE_AFTER) is False


def test_a_session_somebody_stopped_is_not_alive(session: Session) -> None:
    """⚠️ Separates the status check from the heartbeat check. This one is beating-fresh by the
    clock and still must not have orders sent for it — somebody ended it on purpose."""
    live = a_live_session(session)
    beat(session, live.id, at=NOON)
    finish_session(session, live.id, at=NOON)
    session.commit()

    assert session_is_alive(session, str(live.id), now=NOON) is False


def test_a_session_that_is_not_on_file_is_not_alive(session: Session) -> None:
    assert session_is_alive(session, str(uuid.uuid4()), now=NOON) is False


def test_an_unreadable_session_id_is_not_alive(
    session: Session, caplog: pytest.LogCaptureFixture
) -> None:
    """Not a `ValueError`. A malformed id on a work queue must refuse, and be logged, and let the
    loop carry on to the next entry."""
    with caplog.at_level("WARNING"):
        assert session_is_alive(session, "zone-42", now=NOON) is False

    assert "not a uuid" in caplog.text


def test_a_session_that_never_beat_is_judged_from_its_start(session: Session) -> None:
    """⚠️ The state a crash at start-up leaves: `running`, no heartbeat, ever. Treating a missing
    beat as fresh would let orders through for exactly the sessions that failed hardest."""
    live = a_live_session(session)

    assert live.heartbeat_at is None
    assert session_is_alive(session, str(live.id), now=NOON) is True, "it has only just started"
    assert session_is_alive(session, str(live.id), now=NOON + STALE_AFTER) is False
