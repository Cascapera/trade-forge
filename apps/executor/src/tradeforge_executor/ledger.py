"""Writing down what happened to an order, and asking whether its session still exists.

Two small jobs that both need the database, kept together and away from the deciding.

**The audit write happens on every path.** A refusal that reached no venue and left no record is
indistinguishable from an order nobody ever placed — and "why did my strategy stop trading at
11am" is the question `order_audit` exists to answer. The refusals are the *interesting* rows.

⚠️ **Liveness is asked about the session that placed the order, not about "the core".** That is
sharper than a generic heartbeat and needs no new mechanism: the order carries its `session_id`,
and `live_sessions.heartbeat_at` already says whether that session is still beating (`is_stale`,
PR-144). The scenario it catches is the real one — an order sitting in the queue, placed by a
session that has since died, must not be sent on behalf of something that no longer exists.
"""

import datetime as dt
import logging
import uuid

from sqlalchemy.orm import Session

from tradeforge_db.live_sessions import is_stale
from tradeforge_db.models import LiveSession, LiveSessionStatus, OrderAudit, OrderAuditStatus
from tradeforge_executor.router import Outcome
from tradeforge_executor.wire import (
    Instruction,
    WireCancel,
    WireOrder,
    cancel_fields,
    modify_stop_fields,
    order_fields,
)

logger = logging.getLogger(__name__)

__all__ = ["record", "request_fields", "session_is_alive"]


def session_is_alive(db: Session, session_id: str, *, now: dt.datetime) -> bool:
    """Is the session that placed this order still running and still beating?

    ⚠️ **Three ways to answer no, and they are all "no".** The row is gone, the row is no longer
    `running`, or the row has stopped beating. An executor that only checked `status` would send
    orders on behalf of a session that was killed at the wall socket, because the thing that
    would have changed `status` is the thing that died — which is the whole reason `is_stale`
    exists.

    ⚠️ A `session_id` that is not a uuid answers **no**, not `ValueError`. Malformed input on a
    queue is a refusal to record, not a loop to crash.
    """
    try:
        identifier = uuid.UUID(session_id)
    except ValueError:
        logger.warning("order carries a session id that is not a uuid: %r", session_id)
        return False

    row = db.get(LiveSession, identifier)
    if row is None or row.status is not LiveSessionStatus.RUNNING:
        return False
    return not is_stale(heartbeat_at=row.heartbeat_at, started_at=row.started_at, now=now)


def request_fields(instruction: Instruction) -> dict[str, str]:
    """The instruction as the trail stores it, whichever of the three it is.

    ⚠️ **Re-encoded from what was parsed, not copied from what arrived.** The two differ exactly
    when the entry carried a field this format does not read — and the trail should say what this
    machine acted on, because that is the thing an incident is asking about. What the venue said
    lands in `response`, verbatim, on the same row.
    """
    if isinstance(instruction, WireOrder):
        return order_fields(
            instruction.request,
            session_id=instruction.session_id,
            client_id=instruction.client_id,
        )
    if isinstance(instruction, WireCancel):
        return cancel_fields(session_id=instruction.session_id, client_id=instruction.client_id)
    return modify_stop_fields(
        session_id=instruction.session_id,
        client_id=instruction.client_id,
        symbol=instruction.symbol,
        stop_loss=instruction.stop_loss,
        decided_at=instruction.decided_at,
    )


def record(db: Session, order: Instruction, outcome: Outcome, *, now: dt.datetime) -> OrderAudit:
    """One row for one outcome. Added to the caller's session; never committed here.

    ⚠️ **The row is written once, with the outcome already known**, which is why there is no
    `requested` status. A row saying "I picked this up" would have to be updated when the answer
    arrived, and `order_audit` refuses updates — so `requested_at` and the outcome land together.

    ⚠️ `live_session_id` is left NULL when the session is not on file. A foreign key pointing at
    a session that does not exist would fail the insert, and losing the audit row is strictly
    worse than losing the link: the row still says which `client_id` was refused and why.
    """
    status, reason = _verdict(outcome, order)
    row = OrderAudit(
        live_session_id=_known_session(db, order.session_id),
        client_id=order.client_id,
        status=status,
        request=request_fields(order),
        response=outcome.placement.raw if outcome.placement is not None else None,
        reason=reason,
        requested_at=now,
        resolved_at=now,
    )
    db.add(row)
    return row


def _verdict(outcome: Outcome, order: Instruction) -> tuple[OrderAuditStatus, str | None]:
    """The status and the reason, decided **together**, because the database ties them.

    ⚠️ `a_refusal_says_why` is `(reason IS NULL) <> (status IN ('refused', 'error'))` — an
    exclusive or, in both directions. Deciding the two separately is how a row ends up with an
    `error` status and no reason, which the CHECK rejects: the insert fails, the audit row is
    lost, and the one event worth recording is the one that goes unrecorded.

    That is not hypothetical here. An order this machine **allowed** and the venue then rejected
    carries `outcome.reason is None` — `allowed` and `sent` are different questions — so the
    reason for an `error` has to be built from what the venue said, not copied from a field that
    is empty by construction.

    `REFUSED` and `ERROR` stay apart because their remedies are opposite: one is this machine's
    safeguards working (look at the rule), the other is the venue failing (look at MT5).
    """
    if not outcome.allowed:
        return OrderAuditStatus.REFUSED, outcome.reason or "refused without a stated rule"

    placement = outcome.placement
    if placement is None:
        return OrderAuditStatus.ERROR, "the order was admitted but never reached the venue"
    if not placement.accepted:
        return (
            OrderAuditStatus.ERROR,
            f"the venue rejected it: retcode {placement.retcode} ({placement.comment})",
        )
    # ⚠️ Only an order can be *partially* done. A cancel either withdrew something or did not,
    # and asking `is_short_of` about an instruction with no volume would be asking a question the
    # instruction has no answer to.
    if isinstance(order, WireOrder) and placement.is_short_of(order.request.volume):
        # Its own value, not a `filled` with a smaller number: the rest of the order is still
        # somewhere, and reading it as filled is how a position ends up half the size a strategy
        # believes it has.
        return OrderAuditStatus.PARTIAL, None
    return OrderAuditStatus.SENT, None


def _known_session(db: Session, session_id: str) -> uuid.UUID | None:
    try:
        identifier = uuid.UUID(session_id)
    except ValueError:
        return None
    return identifier if db.get(LiveSession, identifier) is not None else None
