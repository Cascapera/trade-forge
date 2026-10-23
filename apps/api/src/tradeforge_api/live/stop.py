"""Asking one live session to stop, from outside the machine it runs on.

A session ends when its `stopping` predicate says so, and until now the only thing that could
say so was a signal. On Windows that means Ctrl-C in the session's own console: measured 25/08,
`taskkill` without `/F` is refused outright and `kill -INT` never reaches the interpreter. So a
session running on the trading box could not be ended from anywhere else without killing the
process and leaving a `running` row for `reconcile_stale` to clean up as a *failure* — which is
what it would honestly be.

`run_session` was ready for this and says so in as many words: `stopping` is a
`Callable[[], bool]` rather than a `threading.Event` **"so the caller owns what stopping means"**.
This module is one more thing it can mean.

⚠️ **This is deliberately not shared with `tradeforge_api.kill_switch`, and the duplication is
the honest choice.** The two look alike — a key in Redis, presence engages, a timestamp beside
it — and they differ on the only thing that matters:

| | kill switch | stop request |
|---|---|---|
| scope | the whole executor | one session |
| Redis unreachable | **engaged** (fail closed) | **not requested** (fail open) |
| what it protects | the account from new risk | nothing; it is an operator convenience |

A shared helper would have to take the failure policy as a parameter, and a parameter is exactly
how the wrong one gets passed. See `stop_requested` for why the asymmetry is right.
"""

import datetime as dt
import logging
import uuid
from collections.abc import Callable
from typing import Protocol

from redis.exceptions import RedisError
from redis.typing import EncodableT, KeyT, ResponseT

logger = logging.getLogger(__name__)

__all__ = [
    "StopStore",
    "request_stop",
    "requested_at",
    "stop_key",
    "stop_predicate",
    "stop_requested",
]


def stop_key(session_id: uuid.UUID) -> str:
    """The key one session's stop request lives under.

    Per session, unlike the kill switch's single key, because this *is* the aimed weapon the
    kill switch deliberately is not: stopping one strategy is an ordinary operational act, and
    the whole reason to address it by id is that the others keep running.
    """
    return f"live-session:{session_id}:stop"


class StopStore(Protocol):
    """The three Redis calls this module makes, in redis-py's own vocabulary.

    Spelled from the real client's signature rather than from what a double happens to need —
    a protocol is satisfied by a *wider* parameter and a *narrower* return, and this project has
    got that backwards in three separate ways. `process.main` handing a real `Redis` to these
    functions is what makes mypy check the claim.
    """

    def set(self, name: KeyT, value: EncodableT) -> ResponseT: ...

    def exists(self, *names: KeyT) -> ResponseT: ...

    def get(self, name: KeyT) -> ResponseT: ...


def request_stop(store: StopStore, session_id: uuid.UUID, *, now: dt.datetime) -> None:
    """Ask this session to finish the bar it is on and stop. Idempotent.

    ⚠️ **No expiry, and the first draft had one.** An hour looked tidy — long enough to survive
    a hiccup, short enough not to litter. It is wrong in a direction that matters: a machine
    asleep past the expiry wakes up and goes on trading a session somebody ended, silently. The
    request stands until it is honoured.

    Nothing deletes it afterwards either. A key belonging to a session that has already stopped
    is inert — nothing polls it — and *removing* it opens a window where the request is gone and
    the row is still `running`, which is the one state an operator cannot act on.

    ⚠️ The value is a timestamp and **nothing reads it to decide**. `stop_requested` asks
    `EXISTS`, exactly as the executor reads its own switch as presence: a decision that depends
    on parsing a payload is a decision a malformed payload makes for you.
    """
    store.set(stop_key(session_id), now.isoformat())


def stop_requested(store: StopStore, session_id: uuid.UUID) -> bool:
    """Has anybody asked this session to stop? **Answers `False` when Redis cannot be asked.**

    ⚠️ **Fails open, which is the opposite of `RedisFlag` next door, and the asymmetry is the
    whole design.** That switch answers `True` on an unreadable Redis because it decides whether
    to *take on risk*, and refusing costs a missed trade while allowing costs money.

    Here the costs point the other way. Stopping is not the safe direction: a session that stops
    abandons a position it was managing — no trailing stop, no exit condition, nothing but the
    level already sitting at the venue — and it does so unattended, because whoever would have
    watched is the person whose Redis is down. Ending a live strategy over a transient network
    fault is a cure that reproduces the disease.

    And the safe direction is already covered by something else: the kill switch stops the
    account taking on new risk and *it* fails closed. That is the mechanism for "I cannot reach
    anything and I need this to stop". This one is for "I have decided this session is done".
    """
    try:
        return bool(store.exists(stop_key(session_id)))
    except RedisError:
        # Not silence: a session that ignores a stop request has to say why, or the operator is
        # left watching a button that did nothing.
        logger.exception("could not read the stop request for %s; assuming none", session_id)
        return False


def requested_at(store: StopStore, session_id: uuid.UUID) -> dt.datetime | None:
    """When the stop was asked for, or `None` if nobody asked — or if the stamp is unreadable.

    For a screen, never for a decision. On H4 a session can sit for a minute between reads, so
    "requested 40 seconds ago, still running" and "requested an hour ago, still running" are
    very different facts about whether anything is listening.

    ⚠️ A stamp that will not parse answers `None` rather than raising. It is decoration; letting
    it fail the read would let a hand-written key take out the panel.
    """
    raw = store.get(stop_key(session_id))
    text = raw.decode() if isinstance(raw, bytes) else raw
    if not isinstance(text, str):
        return None
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError:
        logger.warning("%s holds %r, which is not a timestamp", stop_key(session_id), text)
        return None


def stop_predicate(
    store: StopStore, session_id: uuid.UUID, *, signalled: Callable[[], bool]
) -> Callable[[], bool]:
    """The predicate `run_session` and the candle stream are both driven by: signal **or** request.

    ⚠️ **A named function rather than a lambda in `process.main`, and that is not style.** A rule
    with no callable address is a rule a test has to write out in its own body — and this project
    has watched exactly that happen to `is_stale`, where the test restated the comparison, pinned
    itself instead of the rule, and a mutant walked through both suites. Everything below is
    reachable from a test with two fakes and no process.

    ⚠️ **Order is cost, not correctness.** The local flag is free and the Redis read is a round
    trip, so a process that has already been signalled never makes it — which matters because
    this is consulted once per blocked read, for the whole life of a session.
    """

    def should_stop() -> bool:
        return signalled() or stop_requested(store, session_id)

    return should_stop
