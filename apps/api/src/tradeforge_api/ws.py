"""The two live feeds: one backtest's progress, and one live session's events.

They are the same idea over two different Redis primitives, and the difference is not a matter
of taste:

* A backtest's progress is **pub/sub**. It is a percentage that nobody wants the history of, and
  a subscriber who was not listening has missed nothing worth replaying.
* A live session's events are a **stream** (`venue.outcomes`), because they are things that
  happened to real money. The executor already writes them there for the session to read, and
  `VENUE_OUTCOMES`'s own docstring says who else was expected: *"an outcome must be seen by the
  session that placed it, and by anything else watching (a panel, later)"*. This is that panel.
"""

import asyncio
import contextlib
import datetime as dt
import json
import logging
import uuid
from collections.abc import Callable
from typing import Any, cast

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_api.queue import progress_channel
from tradeforge_api.schemas import LiveSessionOut, session_fields
from tradeforge_db.models import Instrument, LiveSession, LiveSessionStatus
from tradeforge_executor.wire import VENUE_OUTCOMES, WireFill, outcome_from_fields

logger = logging.getLogger(__name__)

router = APIRouter()

_TERMINAL = {"done", "failed"}

LIVE_BLOCK_MS = 15_000
"""How long the session feed waits on the stream before looking at the row again.

⚠️ **`BEAT_EVERY`, deliberately.** Two different things wake this loop: an outcome arriving, and
the *absence* of one. A session dying is not an event anybody publishes — the process that would
say so is the process that died — so the only way to notice is to look at `heartbeat_at`, and
looking more often than it is written buys nothing while looking less often means a panel shows a
corpse as healthy for the difference. See `live_sessions.is_stale`, which owns the threshold.
"""


@router.websocket("/ws/backtests/{backtest_id}")
async def backtest_progress(websocket: WebSocket, backtest_id: uuid.UUID) -> None:
    await websocket.accept()
    settings = websocket.app.state.settings
    redis: Redis = Redis(host=settings.redis_host, port=settings.redis_port)
    pubsub = redis.pubsub()
    await pubsub.subscribe(progress_channel(backtest_id))
    # Announce that the subscription is live before any progress can arrive. A client (and a
    # test) then knows that from this point on, nothing published to the channel will be missed.
    await websocket.send_text(json.dumps({"status": "subscribed"}))
    try:
        async for message in pubsub.listen():
            if message["type"] != "message":
                continue  # subscription confirmations and the like are not progress
            data = message["data"]
            text = data.decode() if isinstance(data, bytes) else str(data)
            await websocket.send_text(text)
            if _is_terminal(text):
                break
    except WebSocketDisconnect:
        pass  # the client hung up; unwinding below is all that is left to do
    finally:
        await pubsub.unsubscribe(progress_channel(backtest_id))
        await pubsub.aclose()  # type: ignore[no-untyped-call]  # redis ships no stub for this
        await redis.aclose()
        await _close_quietly(websocket)


def _is_terminal(text: str) -> bool:
    try:
        return json.loads(text).get("status") in _TERMINAL
    except (ValueError, AttributeError):
        return False


async def _close_quietly(websocket: WebSocket) -> None:
    # Already closed by the disconnect is fine; anything else is not ours to swallow.
    with contextlib.suppress(RuntimeError):
        await websocket.close()


# --------------------------------------------------------------------------- #
# One live session's events                                                    #
# --------------------------------------------------------------------------- #


def _snapshot(
    factory: Callable[[], Session], session_id: uuid.UUID, *, now: dt.datetime
) -> LiveSessionOut | None:
    """The session as `/live-sessions` would report it, or `None` if there is no such row.

    ⚠️ **Synchronous, and called through `asyncio.to_thread` on purpose.** SQLAlchemy's session is
    blocking; awaiting nothing while it runs would stall the event loop and every *other* socket
    with it. The query is small and runs once per tick, so a thread hop is the whole cost.

    ⚠️ It projects through `session_fields`, the same function the HTTP routes use. A panel that
    got `stale` from one shape over HTTP and another over the socket would flicker between two
    truths every time an event arrived.
    """
    db = factory()
    try:
        row = db.get(LiveSession, session_id)
        if row is None:
            return None
        symbol = db.scalar(select(Instrument.symbol).where(Instrument.id == row.instrument_id))
        return LiveSessionOut(**session_fields(row, symbol or "", now=now))
    finally:
        db.close()


def _is_running(state: LiveSessionOut) -> bool:
    return state.status == LiveSessionStatus.RUNNING.value


def _watched(state: LiveSessionOut) -> tuple[object, ...]:
    """The part of a session's state whose change is worth a frame.

    ⚠️ **`silent_for_seconds` and `heartbeat_at` are deliberately not in here, and leaving them in
    was the first version's bug.** Both change *by construction* on every tick — one is a clock,
    the other is written every `BEAT_EVERY` by a healthy session — so a projection that included
    them would differ from the last one every single time, and "send a state when it changes"
    would quietly become "send a state every fifteen seconds, for ever". The rule would still be
    spelled as a comparison and would have stopped meaning anything.

    What is left is what an operator would call news: it stopped, it went stale, it finished a
    bar, it failed. `stale` is the freshness question already *answered*, which is the answer a
    panel needs — the raw seconds live on `GET /live-sessions/{id}` for whoever wants them.
    """
    return (state.status, state.stale, state.stopped_at, state.last_bar_time, state.error)


def _event_for(fields: dict[str, str], session_id: uuid.UUID) -> dict[str, Any] | None:
    """One stream entry as this session's event, or `None` if it is not this session's business.

    ⚠️ **A malformed entry is skipped, never fatal.** `venue.outcomes` is fan-out: everything any
    executor publishes for any session passes under this reader's nose. Letting one unreadable
    entry raise would close a panel that is watching a perfectly healthy session, for a reason
    that has nothing to do with it.
    """
    try:
        outcome = outcome_from_fields(fields)
    except (ValueError, KeyError, ArithmeticError):
        logger.warning("skipping an unreadable entry on %s: %r", VENUE_OUTCOMES, fields)
        return None

    if outcome.session_id != str(session_id):
        return None

    if isinstance(outcome, WireFill):
        return {
            "type": "fill",
            "client_id": outcome.client_id,
            "at": outcome.at.isoformat(),
            "symbol": outcome.symbol,
            # Money as text, like everywhere else on this wire: a JSON number is an IEEE double
            # and the exact decimal discipline would end at the socket.
            "price": str(outcome.price),
            "volume": str(outcome.volume),
            "spread": str(outcome.spread),
        }
    return {
        "type": "refusal",
        "client_id": outcome.client_id,
        "at": outcome.at.isoformat(),
        "reason": outcome.reason,
        # ⚠️ Carried, because the two refusals behave oppositely: ours describe conditions that
        # change on their own, the venue's usually do not. A panel that showed one word for both
        # would be telling somebody to wait when they should be fixing something.
        "by_venue": outcome.by_venue,
        # Absent and zero are different facts, and only one of them is a verdict.
        "retcode": outcome.retcode,
    }


async def _tail(redis: Redis) -> str:
    """The id of the newest entry on the stream, or `0-0` if it has none.

    ⚠️ **Read before the first state snapshot, not after**, so nothing can happen in the gap
    between "what is the state" and "start listening". The other order leaves a window in which a
    fill lands, is missed by the listener, and is also absent from the snapshot the panel drew.

    ⚠️ It also means the never-trimmed backlog on this stream is irrelevant here: this feed starts
    at the tail, so 500 stale entries cost nothing. What a client missed while disconnected is
    `GET /live-sessions/{id}/events`, which is why that endpoint exists.
    """
    entries = cast(
        "list[tuple[str, dict[str, str]]]", await redis.xrevrange(VENUE_OUTCOMES, count=1)
    )
    return entries[0][0] if entries else "0-0"


@router.websocket("/ws/live-sessions/{session_id}")
async def live_session_events(websocket: WebSocket, session_id: uuid.UUID) -> None:
    """Everything that happens to one live session, as it happens.

    The first frame is always the session's current state, so a client never has to guess what it
    is looking at before the first event arrives — the same promise `subscribed` makes on the
    backtest feed, carrying something useful instead.

    After that: `fill` and `refusal` as the executor publishes them, plus a fresh `state` whenever
    the row changes in a way worth reporting — see `_watched`, which is narrower than "the row
    changed" and has to be. **A `state` is sent on change, not on a timer**, so a healthy quiet
    session costs nothing on the wire.

    ⚠️ **The feed closes when the session reaches a terminal status**, including when it was
    already terminal before anybody connected — a socket that stayed open on a finished session
    would have a panel showing "live" over a row that stopped hours ago.

    ⚠️ **A session that died without saying so still closes this feed**, and that is the point of
    looking at the row rather than only at the stream: nothing publishes "I have crashed". The
    state carries `stale`, `reconcile_stale` eventually marks it `failed`, and the feed ends.
    """
    await websocket.accept()
    settings = websocket.app.state.settings
    factory: Callable[[], Session] = websocket.app.state.session_factory
    redis: Redis = Redis(host=settings.redis_host, port=settings.redis_port, decode_responses=True)

    try:
        cursor = await _tail(redis)
        state = await asyncio.to_thread(_snapshot, factory, session_id, now=_utcnow())
        if state is None:
            await websocket.send_text(json.dumps({"type": "error", "detail": "no such session"}))
            return

        await websocket.send_text(
            json.dumps({"type": "state", "session": state.model_dump(mode="json")})
        )
        while _is_running(state):
            answer = cast(
                "list[tuple[str, list[tuple[str, dict[str, str]]]]] | None",
                await redis.xread({VENUE_OUTCOMES: cursor}, block=LIVE_BLOCK_MS),
            )
            for _stream, entries in answer or []:
                for entry_id, fields in entries:
                    cursor = entry_id
                    event = _event_for(fields, session_id)
                    if event is not None:
                        await websocket.send_text(json.dumps(event))

            fresh = await asyncio.to_thread(_snapshot, factory, session_id, now=_utcnow())
            if fresh is None or _watched(fresh) == _watched(state):
                continue
            state = fresh
            await websocket.send_text(
                json.dumps({"type": "state", "session": state.model_dump(mode="json")})
            )
    except WebSocketDisconnect:
        pass  # the client hung up; unwinding below is all that is left to do
    finally:
        await redis.aclose()
        await _close_quietly(websocket)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
