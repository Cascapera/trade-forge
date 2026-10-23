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
from tradeforge_executor.ledger import (
    deal_was_reported,
    record,
    session_for,
    session_is_alive,
)
from tradeforge_executor.router import Outcome
from tradeforge_executor.wire import WireOrder

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 25, 12, tzinfo=dt.UTC)


def an_order(session_id: str = "", *, client_id: str = "zone-42", **overrides: Any) -> WireOrder:
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
        client_id=client_id,
        session_id=session_id or str(uuid.uuid4()),
        request=OrderRequest(**values),
    )


def a_placement(
    *,
    accepted: bool = True,
    volume: str = "0.10",
    retcode: int = 10009,
    deal: int | None = 555,
    spread: str = "0.00002",
) -> Placement:
    """``deal=None`` is the resting shape: accepted by the venue, nothing executed."""
    executed = accepted and deal is not None
    return Placement(
        accepted=accepted,
        ticket=99 if accepted else None,
        filled_volume=Decimal(volume) if executed else Decimal(0),
        price=Decimal("1.10000") if executed else None,
        retcode=retcode,
        comment="done" if accepted else "no money",
        # ⚠️ **`deal` belongs in here, and it was missing.** `raw` is `OrderSendResult._asdict()`
        # verbatim, which always carries a `deal` — `0` when nothing executed. `deal_was_reported`
        # reads exactly that key, so a fixture without it is a fixture that cannot fail the way
        # production would.
        raw={
            "retcode": retcode,
            "comment": "done" if accepted else "no money",
            "deal": deal if executed else 0,
        },
        deal=deal if executed else None,
        spread=Decimal(spread) if executed else None,
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
    assert row.response == {"retcode": 10009, "comment": "done", "deal": 555}


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
    assert row.response == {"retcode": 10019, "comment": "no money", "deal": 0}


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


# --------------------------------------------------------------------------- #
# Reading the trail back: whose deal is this, and did we already say so         #
# --------------------------------------------------------------------------- #


def an_outcome(*, client_id: str, session_id: str, deal: int | None) -> Outcome:
    """An allowed order and what the venue answered. `deal=None` is the resting shape."""
    return Outcome(
        client_id=client_id,
        session_id=session_id,
        allowed=True,
        reason=None,
        placement=a_placement(deal=deal),
    )


def test_a_name_is_traced_back_to_the_session_that_sent_it(session: Session) -> None:
    """What a deal read out of the venue's history has to be joined to.

    The deal carries the `client_id` in its comment and nothing else — no session, no strategy —
    so `order_audit` is the only record that the two ever belonged together.
    """
    live = a_live_session(session)
    record(
        session,
        an_order(session_id=str(live.id), client_id="demand-1"),
        an_outcome(client_id="demand-1", session_id=str(live.id), deal=None),
        now=dt.datetime.now(dt.UTC),
    )
    session.commit()

    assert session_for(session, "demand-1") == str(live.id)


def test_a_name_nobody_sent_has_no_session(session: Session) -> None:
    """⚠️ `None` is a refusal, not an absence to work around. `WireFill.session_id` routes a fill
    to the strategy that armed it, so a guess here hands a real position to a session that never
    asked for one."""
    assert session_for(session, "a-name-nobody-sent") is None


def test_a_row_with_no_session_is_not_an_answer(session: Session) -> None:
    """⚠️ **The branch that separates "we sent it" from "we can say whose it was".** `record`
    leaves `live_session_id` NULL when the session is not on file — losing the link beats losing
    the audit row — and such a row still names the `client_id`. Reading it as an attribution
    would return NULL where the code expects a session id."""
    record(
        session,
        an_order(session_id=str(uuid.uuid4()), client_id="orphan-1"),
        an_outcome(client_id="orphan-1", session_id=str(uuid.uuid4()), deal=None),
        now=dt.datetime.now(dt.UTC),
    )
    session.commit()

    assert session.query(OrderAudit).filter_by(client_id="orphan-1").one().live_session_id is None
    assert session_for(session, "orphan-1") is None


def test_a_deal_the_order_loop_recorded_is_known_to_have_been_reported(session: Session) -> None:
    """The de-duplication, against a row shaped like the one production writes.

    ⚠️ The ticket is read out of `response`, which is `OrderSendResult._asdict()` verbatim — not
    out of a column somebody remembered to add. That is what makes this answer true for rows
    written before this function existed.
    """
    live = a_live_session(session)
    record(
        session,
        an_order(session_id=str(live.id), client_id="market-1"),
        an_outcome(client_id="market-1", session_id=str(live.id), deal=777),
        now=dt.datetime.now(dt.UTC),
    )
    session.commit()

    assert deal_was_reported(session, 777)
    assert not deal_was_reported(session, 778), "any ticket would do, which is not a check"


def test_a_resting_order_reports_no_deal(session: Session) -> None:
    """⚠️ **The case that makes this whole PR necessary, from the trail's side.** A limit comes
    back accepted with `deal=0`: nothing executed, so the order loop published nothing, so the
    scan **must** publish it when it finally trades. A check that treated the echoed `0` as a
    deal would suppress exactly the fills this exists to deliver."""
    live = a_live_session(session)
    record(
        session,
        an_order(session_id=str(live.id), client_id="limit-1"),
        an_outcome(client_id="limit-1", session_id=str(live.id), deal=None),
        now=dt.datetime.now(dt.UTC),
    )
    session.commit()

    row = session.query(OrderAudit).filter_by(client_id="limit-1").one()
    assert row.response is not None, "the trail lost the venue's answer"
    assert row.response["deal"] == 0, "a resting order did not echo a zero deal"
    assert not deal_was_reported(session, 0), (
        "the echoed zero of a resting order was read as an execution"
    )


def test_the_most_recent_sending_of_a_name_is_the_one_that_owns_a_deal(
    session: Session,
) -> None:
    """⚠️ **A `client_id` really does repeat across sessions, and this is the shape it takes.**

    `setups.py` mints the name from `f"{kind}-{time:%Y%m%dT%H%M}-{armed_count}"`. The kind and the
    zone's instant are facts of the market — identical between two sessions warmed over the same
    history — and `_armed_count` restarts at zero with every strategy instance, which is every
    session. So a session that dies and is restarted arms the *same still-valid zone* under the
    *same name*: two rows, two sessions, one name. Not a collision worth one line in a million —
    the ordinary shape of a restart.

    Read oldest-first, a deal arriving now is stamped with the dead session's id, `MT5Broker._read`
    discards everything that is not its own, and the fill is lost while the position is real.
    """
    dead = a_live_session(session)
    # ⚠️ The same strategy and the same instrument, because that is what a restart is. A second
    # `a_live_session` would mint a second EURUSD and fail the unique index — which is the schema
    # saying, correctly, that this scenario is one instrument seen twice.
    alive = open_session(
        session,
        strategy_id=dead.strategy_id,
        instrument_id=dead.instrument_id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        engine_version="0.1.0",
        warmup_bars=10,
        at=NOON + dt.timedelta(hours=1),
    )
    session.commit()
    name = "demand-20260730T1500-1"

    record(
        session,
        an_order(session_id=str(dead.id), client_id=name),
        an_outcome(client_id=name, session_id=str(dead.id), deal=None),
        now=NOON,
    )
    record(
        session,
        an_order(session_id=str(alive.id), client_id=name),
        an_outcome(client_id=name, session_id=str(alive.id), deal=None),
        now=NOON + dt.timedelta(hours=1),
    )
    session.commit()

    assert session.query(OrderAudit).filter_by(client_id=name).count() == 2, (
        "the scenario needs two sendings of one name, or it proves nothing about ordering"
    )
    assert session_for(session, name) == str(alive.id), (
        "the deal was attributed to the session that died, not the one holding the position"
    )
