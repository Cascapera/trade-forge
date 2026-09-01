"""`/live-sessions` — what is trading forward right now, and the handle to end one.

Until this router existed a live session was observable only through `psql`. Everything here is
a projection of rows the session process already writes; nothing is invented and nothing is
asked of MetaTrader.

**Where each answer comes from, and why not from somewhere else:**

* **state, capital, warm-up** — the `live_sessions` row.
* **open position** — `trades` with no exit. *Not* the venue: MT5 holds the whole **account**,
  which can carry a position this session did not take.
* **realised today** — `trades` closed since midnight UTC.
* **event log** — `order_audit` by session. Not the logs: a refusal has to be queryable rather
  than greppable, which is the whole reason that table exists.
* **was it asked to stop** — Redis. Not the row: the request has to reach a process that spends
  its life blocked on a stream read.

⚠️ **The list is deliberately Postgres-only.** It is the screen somebody opens when they suspect
something is wrong, so it must not be capable of failing for a reason unrelated to the sessions
themselves. Only the detail reaches for Redis, and only for `stop_requested_at`.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from redis.exceptions import RedisError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_api.deps import SessionDep, StopStoreDep
from tradeforge_api.live.stop import request_stop, requested_at
from tradeforge_api.schemas import (
    LiveSessionDetail,
    LiveSessionOut,
    LiveSessionsPage,
    OpenPositionOut,
    SessionEventOut,
    SessionEventsPage,
)
from tradeforge_db.live_sessions import is_stale, silence
from tradeforge_db.models import Instrument, LiveSession, LiveSessionStatus, OrderAudit, Trade

router = APIRouter(tags=["live-sessions"])

_MAX_OFFSET = 9_223_372_036_854_775_807  # 2**63 - 1, Postgres bigint

_Responses = dict[int | str, dict[str, Any]]

_NOT_FOUND: _Responses = {status.HTTP_404_NOT_FOUND: {"description": "no session with that id"}}
_CONFLICT: _Responses = {status.HTTP_409_CONFLICT: {"description": "the session has already ended"}}

_UNREACHABLE = (
    "could not read the stop request from Redis, so this session's stop state is unknown. "
    "The session itself is unaffected and keeps running."
)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _start_of_day(now: dt.datetime) -> dt.datetime:
    """Midnight UTC of `now`'s day — the same boundary the daily loss cap counts from.

    ⚠️ Not the operator's midnight and not the broker's. Three clocks are live in this system
    (he is on UTC-3, the database on UTC, the broker on UTC+3), and a panel totalling a different
    day from the cap that halts trading would be unable to explain either number.
    """
    return now.astimezone(dt.UTC).replace(hour=0, minute=0, second=0, microsecond=0)


def _load(session: Session, session_id: uuid.UUID) -> tuple[LiveSession, str]:
    """The row and its instrument's symbol, or a 404.

    The symbol is joined rather than left to the client: a panel that printed an instrument uuid
    where a human expects `EURUSD` is a panel nobody can read at a glance, which is the only
    speed that matters on this screen.
    """
    row = session.get(LiveSession, session_id)
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="live session not found")
    symbol = session.scalar(select(Instrument.symbol).where(Instrument.id == row.instrument_id))
    return row, symbol or ""


def _project(row: LiveSession, symbol: str, *, now: dt.datetime) -> dict[str, object]:
    """The fields every view of a session shares, including the two the row cannot state."""
    return {
        "id": row.id,
        "strategy_id": row.strategy_id,
        "instrument_id": row.instrument_id,
        "symbol": symbol,
        "timeframe": row.timeframe,
        "mode": row.mode.value,
        "status": row.status.value,
        "initial_capital": row.initial_capital,
        "engine_version": row.engine_version,
        "warmup_bars": row.warmup_bars,
        "started_at": row.started_at,
        "stopped_at": row.stopped_at,
        "heartbeat_at": row.heartbeat_at,
        "last_bar_time": row.last_bar_time,
        "error": row.error,
        # ⚠️ Called, never re-derived. `is_stale` owns the boundary — silent for *exactly*
        # `STALE_AFTER` counts — and the last test that wrote that comparison out in its own body
        # pinned itself instead of the rule, letting a mutant through both suites.
        "stale": is_stale(heartbeat_at=row.heartbeat_at, started_at=row.started_at, now=now),
        "silent_for_seconds": int(
            silence(
                heartbeat_at=row.heartbeat_at, started_at=row.started_at, now=now
            ).total_seconds()
        ),
    }


@router.get("/live-sessions", response_model=LiveSessionsPage)
def list_sessions(
    session: SessionDep,
    *,
    session_status: Annotated[
        LiveSessionStatus | None,
        Query(alias="status", description="only sessions in this state"),
    ] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
) -> LiveSessionsPage:
    """Every session, newest first, with enough to tell a live one from a corpse.

    ⚠️ **`status=running` is a filter on what the rows claim**, not on what is running. A process
    that died leaves its row saying `running` because the thing that would update it is the thing
    that died — so `stale` and `silent_for_seconds` travel with every row, and a screen that
    showed only `status` would report the deadest sessions as the healthiest.

    ⚠️ `offset` is capped, and the ceiling is not decoration: this project has already had a
    query parameter handed the 500 of a bigint by a fuzzer, on a route that had been green for
    weeks because nobody had drawn that value yet.
    """
    now = _utcnow()
    where = [] if session_status is None else [LiveSession.status == session_status]

    total = session.scalar(select(func.count()).select_from(LiveSession).where(*where)) or 0
    rows = session.scalars(
        select(LiveSession)
        .where(*where)
        .order_by(LiveSession.started_at.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    # One query for every instrument on the page, not one per row. A panel polls, and N+1 on a
    # poll is N+1 for ever.
    # ⚠️ `.tuples()` and not a bare `execute`. SQLAlchemy types a plain result as `Row`, which
    # `dict()` refuses and a comprehension only launders — this is the typed spelling of the same
    # query. One query for every instrument on the page, not one per row: a panel polls, and an
    # N+1 on a poll is an N+1 for ever.
    symbols: dict[uuid.UUID, str] = dict(
        session.execute(
            select(Instrument.id, Instrument.symbol).where(
                Instrument.id.in_({row.instrument_id for row in rows})
            )
        )
        .tuples()
        .all()
    )
    return LiveSessionsPage(
        total=total,
        sessions=[
            LiveSessionOut(**_project(row, symbols.get(row.instrument_id, ""), now=now))
            for row in rows
        ],
    )


@router.get("/live-sessions/{session_id}", response_model=LiveSessionDetail, responses=_NOT_FOUND)
def get_session(
    session_id: uuid.UUID, session: SessionDep, store: StopStoreDep
) -> LiveSessionDetail:
    """One session: its state, what it is holding, what it realised today, and its stop request.

    ⚠️ **503 rather than `stop_requested_at: null` when Redis cannot be read.** `null` here means
    "nobody asked", and this is the screen an operator is looking at when they are deciding
    whether to press stop — telling them nobody asked when the truth is unknown is the one error
    that changes what they do next. The list endpoint stays up regardless, because it never asks.
    """
    row, symbol = _load(session, session_id)
    now = _utcnow()

    open_rows = session.scalars(
        select(Trade)
        .where(Trade.live_session_id == session_id, Trade.exit_time.is_(None))
        .order_by(Trade.entry_time)
    ).all()

    since = _start_of_day(now)
    closed_today = session.execute(
        select(func.count(), func.coalesce(func.sum(Trade.net_pnl), Decimal(0))).where(
            Trade.live_session_id == session_id, Trade.exit_time >= since
        )
    ).one()

    try:
        asked_at = requested_at(store, session_id)
    except RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"{_UNREACHABLE} ({error})"
        ) from error

    return LiveSessionDetail(
        **_project(row, symbol, now=now),
        stop_requested_at=asked_at,
        open_positions=[
            OpenPositionOut(
                id=trade.id,
                direction=trade.direction.value,
                entry_time=trade.entry_time,
                entry_price=trade.entry_price,
                volume=trade.volume,
                stop_loss=trade.stop_loss,
                take_profit=trade.take_profit,
            )
            for trade in open_rows
        ],
        trades_closed_today=closed_today[0],
        realised_today=closed_today[1],
    )


@router.get(
    "/live-sessions/{session_id}/events", response_model=SessionEventsPage, responses=_NOT_FOUND
)
def list_events(
    session_id: uuid.UUID,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0, le=_MAX_OFFSET)] = 0,
) -> SessionEventsPage:
    """This session's `order_audit`, newest first, paginated.

    A sub-resource rather than a field on the detail, and the reason is the shape of the data: a
    session that has run for a week has hundreds of rows, and a panel that only wants to know
    whether the thing is alive would carry all of them on every poll.

    ⚠️ **Newest first, unlike `/backtests/{id}/trades`**, which is oldest first. A backtest's
    trades are a sequence somebody reads from the beginning; this is a log somebody opens because
    something just happened.
    """
    _load(session, session_id)

    total = (
        session.scalar(
            select(func.count())
            .select_from(OrderAudit)
            .where(OrderAudit.live_session_id == session_id)
        )
        or 0
    )
    rows = session.scalars(
        select(OrderAudit)
        .where(OrderAudit.live_session_id == session_id)
        .order_by(OrderAudit.requested_at.desc(), OrderAudit.id.desc())
        .limit(limit)
        .offset(offset)
    ).all()

    return SessionEventsPage(
        total=total,
        events=[
            SessionEventOut(
                id=event.id,
                client_id=event.client_id,
                status=event.status.value,
                reason=event.reason,
                requested_at=event.requested_at,
                resolved_at=event.resolved_at,
                request=event.request,
                response=event.response,
            )
            for event in rows
        ],
    )


@router.post(
    "/live-sessions/{session_id}/stop",
    response_model=LiveSessionDetail,
    responses={**_NOT_FOUND, **_CONFLICT},
)
def stop_session(
    session_id: uuid.UUID, session: SessionDep, store: StopStoreDep
) -> LiveSessionDetail:
    """Ask this session to finish the bar it is on and stop. Idempotent.

    **This is not the kill switch and the difference is not cosmetic.** The kill switch stops the
    *executor* taking on new risk, for every session at once, and it fails closed. This ends one
    session, on purpose, and it fails open — a session that stopped because Redis blinked would
    be a strategy abandoned mid-position over a network fault. See `live/stop.py`.

    ⚠️ **It does not close the position.** The session stops managing it — no trailing stop, no
    exit condition — and it stays at the venue with the level it already has. The panel has to
    say so beside the button, because "stop" reads as "get me out" and this is not that.

    ⚠️ **It is not instantaneous, and the body says how far off it is.** A session spends nearly
    all its time blocked on a stream read, so it notices within one block interval rather than at
    the next bar — but `status` will still say `running` when this returns. `stop_requested_at`
    is what tells the screen the request landed.

    `409` for a session that has already ended: it is not an error the caller can fix, but
    answering `200` would let a screen report a stop it did not cause. `202` was the other
    candidate and says the wrong thing — the *request* is durably recorded when this returns; it
    is the effect that is pending, and 202 would suggest neither is settled.
    """
    # The symbol is not needed here — `get_session` below reloads and projects it. What this
    # call is for is the 404 and the status, both of which have to happen before anything writes.
    row, _ = _load(session, session_id)
    if row.status is not LiveSessionStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"session {session_id} is {row.status.value}, not running",
        )

    now = _utcnow()
    try:
        request_stop(store, session_id, now=now)
    except RedisError as error:
        # ⚠️ Never a cheerful 200. The request was not recorded, so nothing will act on it, and a
        # screen that showed "stopping…" would have an operator waiting on a message nobody sent.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not record the stop request ({error}); the session is still running",
        ) from error

    return get_session(session_id, session, store)
