"""Saying "the process exists" on a cadence the work does not have.

**Why this is a thread and not a line in the loop.** The obvious place to beat is next to the
bar: finish a bar, write the heartbeat. It does not work, and the reason is structural rather
than a matter of taste. `CandleStream.candles()` blocks inside its read and hands control back
only at a `yield`, and a `yield` is a closed bar — so on H4 the loop body runs **once every four
hours**. With `BEAT_EVERY` at 15 seconds and `STALE_AFTER` at 60, a perfectly healthy H4 session
would be reconciled to `failed` a minute after it started. The stream's own "nothing closed"
path cannot rescue it either: that branch `continue`s, so the consumer never sees it.

Making the loop beat therefore requires the bar cadence and the liveness cadence to be the same
number, and they are not the same *kind* of number. One is chosen by the market, the other by
how long an operator is willing to wait before believing a session is gone.

⚠️ **What this proves, and what it does not.** A beat says the heartbeat thread is running and
can reach the database. It does **not** say the engine is making progress — a main thread wedged
on something that is not the stream would keep beating for ever. That is exactly why
`last_bar_time` is a separate column and not a synonym: "the process exists" and "the work
advanced" are different questions, and a single answer to both is the failure this project
already documented once, when the collector agent's hourly arq health check nearly had a
healthy agent declared stuck.

**On the first beat.** It happens at `start()`, not one interval later, and it happens **before
`start()` returns**. `open_session` leaves `heartbeat_at` NULL and reads that as "died between
opening and its first bar"; the beat's documented meaning is that the process exists, and it does
exist at start. Waiting an interval would make a session killed five seconds in indistinguishable
from one whose thread never ran.

⚠️ **"At `start()`" used to mean "on the thread `start()` spawned", and those are not the same
promise.** A caller that begins a heartbeat because it is about to do something that requires
being alive got a beat that had not landed yet — measured, 200 starts out of 200 at a realistic
beat cost. `session.py` is that caller, and the demo account produced the failure in full: a
session warmed over 39 204 bars, opened its row, placed the warm-up's resting order, and the
executor refused it as silent for 700 s. See `start()`.
"""

import datetime as dt
import logging
import threading
import uuid
from collections.abc import Callable

from sqlalchemy import text
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_db.live_sessions import BEAT_EVERY, beat
from tradeforge_db.session import session_scope

logger = logging.getLogger(__name__)

__all__ = ["BEAT_LOCK_TIMEOUT", "Heartbeat", "session_heartbeat"]

# How long a beat will wait for the row before giving up on this tick.
#
# ⚠️ **A beat must never be able to hang, and this is not hypothetical.** The session process
# writes `last_bar_time` on the same row every bar, so its main thread and this one contend for
# it by design. Without a timeout, a beat that arrives while the main thread holds an open
# transaction on that row blocks until that transaction ends — and it blocks *inside* the beat,
# so `stop()` cannot interrupt it either. Measured while writing this module: an uncommitted
# UPDATE on the row left the thread stuck and `stop()` gave up after five seconds.
#
# Well under `BEAT_EVERY`, so a beat that loses the race fails, is counted, and is simply tried
# again on the next tick. Losing four ticks in a row is what `STALE_AFTER` is for.
BEAT_LOCK_TIMEOUT = dt.timedelta(seconds=2)


def _utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


class Heartbeat:
    """A callable run on a fixed cadence by a background thread, until stopped.

    Deliberately knows nothing about sessions or databases. What it owns is the part that is
    easy to get subtly wrong — starting once, stopping promptly, and surviving a beat that
    raises — and that part is worth testing in milliseconds against a counter rather than in
    minutes against Postgres. `session_heartbeat` below is the database half.
    """

    def __init__(
        self,
        beat_once: Callable[[], None],
        *,
        every: dt.timedelta = BEAT_EVERY,
        name: str = "heartbeat",
    ) -> None:
        if every <= dt.timedelta(0):
            # A non-positive interval is a spin loop that writes to the database as fast as the
            # CPU allows. Refused here rather than clamped, because a caller that computed zero
            # has a bug and a clamp would hide it.
            raise ValueError(f"every must be positive, got {every}")

        self._beat_once = beat_once
        self._interval = every.total_seconds()
        self._name = name
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self.beats = 0
        """Beats that reached the database. Written only by the beating thread."""

        self.failures = 0
        """Beats that raised. See `_loop` for why they do not end the thread."""

    def start(self) -> None:
        """Beat now — **on the caller's thread** — then every interval, on a daemon thread.

        Daemon so that a crashed main thread cannot leave a process alive purely because
        something is still writing heartbeats — a session that keeps claiming to be alive after
        its engine died is the exact reading this module exists to prevent.

        ⚠️ **The first beat is synchronous, and that is not a detail of scheduling.** This used
        to happen on the new thread, and "beat now" then meant "beat soon": `start()` returned
        with nothing written, and the caller carried on. Measured over 200 starts with a beat
        costing a millisecond — the floor for a Postgres round trip — the caller was ahead of
        the first beat **200 times out of 200**. So a caller that starts a heartbeat and then
        does something that depends on being alive is, in practice, always doing it too early.
        `session.py` is exactly that caller: it places the warm-up's resting orders immediately
        afterwards, and the executor refuses orders from a session that has not been heard from.

        The cost is that `start()` now blocks for one beat. Bounded by `BEAT_LOCK_TIMEOUT`, and
        paid once.
        """
        if self._thread is not None:
            # Two threads beating is not twice as alive; it is two writers on one column and a
            # `stop()` that only stops one of them.
            raise RuntimeError(f"{self._name} is already started")

        # ⚠️ Cleared here, not in `stop()`. A stopped heartbeat must stay stopped even if the
        # thread has not noticed yet, and clearing on the way out would give a beat still in
        # flight a fresh licence to loop.
        self._stopping.clear()
        # ⚠️ Before the thread exists, so `beats` is already truthful when `start()` returns and
        # a caller can *ask*. Swallowed like any other beat rather than raised: whether a
        # heartbeat that could not reach the database is fatal is the caller's question, not
        # this class's, and `session.py` refuses on it.
        self._beat_safely()
        self._thread = threading.Thread(target=self._loop, name=self._name, daemon=True)
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """Stop beating and wait for the thread to finish. Safe to call before `start`.

        **The join is not politeness.** A session is stopped by writing `stopped_at`, and a beat
        still in flight would land after that write — leaving a stopped row carrying a heartbeat
        from after it stopped, which is the one thing the column must never say.
        """
        self._stopping.set()
        thread, self._thread = self._thread, None
        if thread is None:
            return

        thread.join(timeout)
        if thread.is_alive():
            # Reported, not raised: the caller is shutting a session down and an exception here
            # would replace an orderly stop with a stack trace. A beat that cannot finish in
            # five seconds means the database is gone, which the caller is about to discover
            # on its own.
            logger.warning(
                "%s did not stop within %.1fs; leaving it to the daemon", self._name, timeout
            )

    def _loop(self) -> None:
        # Waits *first*: the beat for this instant was already made by `start()`, on the
        # caller's thread. Beating again here would double the write and, worse, make the first
        # interval half an interval.
        #
        # `Event.wait` rather than `sleep`, so `stop()` returns immediately instead of after up
        # to a full interval. On a 15-second beat that is the difference between a session
        # process exiting at once and appearing to hang.
        while not self._stopping.wait(self._interval):
            self._beat_safely()

    def _beat_safely(self) -> None:
        """One beat, and never an exception.

        ⚠️ **A raise must not end the thread.** A transient failure — a dropped connection, a
        failover — would otherwise stop the heartbeat permanently while the session carried on
        working, and `reconcile_stale` would mark a healthy session `failed`. The loop keeps its
        cadence, so a database that comes back is beaten to again without anything intervening.
        """
        try:
            self._beat_once()
        except Exception:
            self.failures += 1
            logger.exception(
                "%s failed (%d failed, %d succeeded)", self._name, self.failures, self.beats
            )
        else:
            self.beats += 1

    def __enter__(self) -> "Heartbeat":
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()


def session_heartbeat(
    factory: sessionmaker[Session],
    session_id: uuid.UUID,
    *,
    every: dt.timedelta = BEAT_EVERY,
    now: Callable[[], dt.datetime] = _utcnow,
    lock_timeout: dt.timedelta = BEAT_LOCK_TIMEOUT,
) -> Heartbeat:
    """A `Heartbeat` that writes `live_sessions.heartbeat_at` for one session.

    ⚠️ **A factory, not a `Session`.** SQLAlchemy sessions are not safe to share between
    threads, and the session process is using its own to write trades. Handing this one the
    same object would make two threads interleave on one connection, which does not fail
    loudly — it corrupts a transaction that belonged to somebody else.

    Each beat is its own short transaction, for the same reason: joining the caller's
    transaction would mean a heartbeat that is only visible when the session next commits, and
    a session between trades does not commit.

    ⚠️ **`lock_timeout` is what keeps a beat from hanging** — see `BEAT_LOCK_TIMEOUT`. `SET
    LOCAL` rather than a session-wide setting, so the bound dies with the transaction and cannot
    leak onto a pooled connection that some other part of the process picks up next.
    """
    # Rendered here, once, rather than interpolated per beat: `SET LOCAL` takes no bind
    # parameters, so this string is concatenated into SQL and must not come from a beat's
    # arguments. Milliseconds because Postgres reads a bare number that way.
    milliseconds = int(lock_timeout.total_seconds() * 1000)
    if milliseconds <= 0:
        # ⚠️ **Zero is not "no wait" to Postgres, it is "wait for ever"** — `lock_timeout = 0`
        # disables the timeout. So the one value that reads like the safest possible setting is
        # the one that restores the hang this whole mechanism exists to prevent, silently. The
        # refusal covers sub-millisecond durations too, which truncate to the same zero.
        raise ValueError(f"lock_timeout must be at least a millisecond, got {lock_timeout}")
    set_lock_timeout = text(f"SET LOCAL lock_timeout = {milliseconds}")

    def write() -> None:
        with session_scope(factory) as db:
            db.execute(set_lock_timeout)
            beat(db, session_id, at=now())

    return Heartbeat(write, every=every, name=f"heartbeat-{session_id}")
