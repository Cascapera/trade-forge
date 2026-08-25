"""The beating thread, without a database.

Fast on purpose. The interval is a constructor argument precisely so that the machinery — one
thread, a prompt stop, a beat that raises — can be proved against a counter in milliseconds
instead of against Postgres in minutes. `test_heartbeat_integration.py` proves the write.

⚠️ Every wait here has a deadline and polls; none of them sleeps for a fixed "long enough".
A test that sleeps 200ms and asserts a count is a test that fails on a loaded CI runner and
gets marked flaky, which is how a real regression ends up muted.
"""

import datetime as dt
import logging
import threading
import time
import uuid
from collections.abc import Callable
from typing import cast

import pytest
from sqlalchemy.orm import Session, sessionmaker

from tradeforge_api.live.heartbeat import Heartbeat, _utcnow, session_heartbeat

# Short enough that a test proving "it keeps beating" finishes instantly, long enough that a
# handful of beats is not just scheduler noise.
TICK = dt.timedelta(milliseconds=10)

# Longer than any test is willing to wait. Used where the point is that `stop()` does *not*
# wait for the interval — with `time.sleep` in the loop instead of `Event.wait`, a stop would
# block until the join timed out.
NEVER = dt.timedelta(seconds=30)


def wait_until(predicate: Callable[[], bool], *, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return predicate()


class Counter:
    """A beat that records, and can be told to fail its first `fail_times` calls."""

    def __init__(self, *, fail_times: int = 0) -> None:
        self.calls = 0
        self._fail_times = fail_times

    def __call__(self) -> None:
        self.calls += 1
        if self.calls <= self._fail_times:
            raise RuntimeError(f"beat {self.calls} refused")


def test_it_beats_while_the_main_thread_is_blocked() -> None:
    """⚠️ **The whole reason this module is a thread**, in miniature.

    A live session's main thread sits inside `CandleStream.candles()`, blocked on a Redis read
    that returns when a bar closes — four hours on H4. Here the main thread blocks on an event
    nobody sets, which is the same shape and finishes in a moment.

    A design that beat from the bar loop scores **zero** here. That is what this separates: not
    "does the thread work", but "does liveness survive a main thread that is legitimately doing
    nothing for a long time".
    """
    counter = Counter()
    blocked_like_a_stream_read = threading.Event()

    with Heartbeat(counter, every=TICK):
        # The main thread is now as idle as an H4 session between bars.
        assert not blocked_like_a_stream_read.wait(0.2), "nobody sets this; the wait must expire"
        beats_while_blocked = counter.calls

    assert beats_while_blocked >= 5, (
        f"only {beats_while_blocked} beats while the main thread was blocked — "
        "liveness is riding on the bar loop"
    )


def test_the_first_beat_happens_at_start_not_one_interval_later() -> None:
    """A session killed five seconds in must not look like one whose thread never ran.

    ⚠️ `NEVER` is the interval, so a loop that waited before its first beat produces nothing
    here for thirty seconds. Using `TICK` instead would make both orderings pass.
    """
    counter = Counter()
    heartbeat = Heartbeat(counter, every=NEVER)

    heartbeat.start()
    try:
        assert wait_until(lambda: counter.calls >= 1), "no beat before the first interval elapsed"
    finally:
        heartbeat.stop()

    assert counter.calls == 1, "a second beat arrived; the interval is not being honoured"


def test_stopping_does_not_wait_for_the_interval() -> None:
    """⚠️ The one that separates `Event.wait` from `time.sleep`.

    Both loops beat correctly and both stop eventually. The difference is only visible here: a
    sleeping loop cannot be interrupted, so `stop()` blocks until the join times out and a
    session process appears to hang on shutdown.
    """
    heartbeat = Heartbeat(Counter(), every=NEVER)
    heartbeat.start()

    started = time.monotonic()
    heartbeat.stop()
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"stop() took {elapsed:.1f}s; the loop is sleeping, not waiting"


def test_stop_waits_for_a_beat_already_in_flight() -> None:
    """⚠️ The join, and it is not politeness.

    A session is stopped by writing `stopped_at`. A beat still in flight when `stop()` returns
    lands *after* that write, leaving a stopped row carrying a heartbeat from after it stopped —
    the one thing the column must never say.
    """
    entered = threading.Event()
    finished = threading.Event()

    def slow_beat() -> None:
        entered.set()
        time.sleep(0.3)
        finished.set()

    heartbeat = Heartbeat(slow_beat, every=NEVER)
    heartbeat.start()
    assert entered.wait(5), "the beat never started"

    heartbeat.stop()

    assert finished.is_set(), "stop() returned while a beat was still running"


def test_no_beat_lands_after_stop_returns() -> None:
    counter = Counter()
    heartbeat = Heartbeat(counter, every=TICK)
    heartbeat.start()
    assert wait_until(lambda: counter.calls >= 3), "the thread never got going"

    heartbeat.stop()
    settled = counter.calls

    # Twenty intervals' worth of opportunity to beat once more.
    time.sleep(TICK.total_seconds() * 20)

    assert counter.calls == settled, f"{counter.calls - settled} beats arrived after stop()"


def test_a_beat_that_raises_does_not_end_the_thread(caplog: pytest.LogCaptureFixture) -> None:
    """⚠️ Otherwise a dropped connection stops liveness *permanently* while the session works on,
    and `reconcile_stale` marks a healthy session `failed` sixty seconds later. The failure this
    module must survive is exactly the one that makes it look like the thing it reports."""
    counter = Counter(fail_times=3)

    with caplog.at_level(logging.CRITICAL), Heartbeat(counter, every=TICK) as heartbeat:
        assert wait_until(lambda: heartbeat.beats >= 2), "the thread died on the first failure"

    assert heartbeat.failures == 3
    assert heartbeat.beats >= 2, "recovered beats were not counted"


def test_the_two_counters_are_not_the_same_number(caplog: pytest.LogCaptureFixture) -> None:
    """A beat that raised is not a beat. Counting them together would let a session whose every
    write is failing report a healthy-looking tally."""
    counter = Counter(fail_times=2)

    with caplog.at_level(logging.CRITICAL), Heartbeat(counter, every=TICK) as heartbeat:
        assert wait_until(lambda: heartbeat.beats >= 1)

    assert heartbeat.failures == 2
    assert heartbeat.beats + heartbeat.failures == counter.calls


def test_starting_twice_is_refused() -> None:
    """Two threads beating is not twice as alive: it is two writers on one column, and a
    `stop()` that only stops one of them."""
    heartbeat = Heartbeat(Counter(), every=NEVER)
    heartbeat.start()
    try:
        with pytest.raises(RuntimeError, match="already started"):
            heartbeat.start()
    finally:
        heartbeat.stop()


def test_it_can_be_started_again_after_a_clean_stop() -> None:
    """⚠️ The other side of the refusal above. A `start()` that stayed refused for ever would
    make the guard indistinguishable from "this object is single-use", and a session that
    restarts its own heartbeat after a reconnect would be blocked by a rule meant for a
    double-start."""
    counter = Counter()
    heartbeat = Heartbeat(counter, every=TICK)

    heartbeat.start()
    assert wait_until(lambda: counter.calls >= 1)
    heartbeat.stop()
    after_first = counter.calls

    heartbeat.start()
    try:
        # ⚠️ **Two**, not one. The loop beats before it checks the stop event, so a restart that
        # never cleared that event still produces exactly one beat and then exits — alive by the
        # count, dead in fact. `> after_first` passed against that mutant; `>= after_first + 2`
        # is what separates "it is beating again" from "it twitched once".
        assert wait_until(lambda: counter.calls >= after_first + 2), (
            "it beat at most once and stopped; the restart did not clear the stop event"
        )
    finally:
        heartbeat.stop()


def test_stopping_before_starting_is_harmless() -> None:
    """`with` must be usable around code that may never have started one."""
    Heartbeat(Counter(), every=TICK).stop()


def test_stopping_twice_is_harmless() -> None:
    heartbeat = Heartbeat(Counter(), every=TICK)
    heartbeat.start()
    heartbeat.stop()
    heartbeat.stop()


@pytest.mark.parametrize("every", [dt.timedelta(0), dt.timedelta(milliseconds=-1)])
def test_a_non_positive_interval_is_refused(every: dt.timedelta) -> None:
    """A zero interval is a spin loop writing to the database as fast as the CPU allows.
    Refused rather than clamped: a caller that computed zero has a bug, and a clamp hides it."""
    with pytest.raises(ValueError, match="must be positive"):
        Heartbeat(Counter(), every=every)


def test_the_context_manager_stops_on_the_way_out() -> None:
    counter = Counter()
    with Heartbeat(counter, every=TICK):
        assert wait_until(lambda: counter.calls >= 2)
    settled = counter.calls

    time.sleep(TICK.total_seconds() * 20)

    assert counter.calls == settled, "the context manager left the thread beating"


def test_the_context_manager_stops_even_when_the_body_raises() -> None:
    """⚠️ The separating half. A session whose engine throws must not leave a thread behind
    still reporting it alive — that is a dead session with a healthy heartbeat, which is the
    precise reading `reconcile_stale` exists to make impossible."""
    counter = Counter()

    def a_session_whose_engine_throws() -> None:
        with Heartbeat(counter, every=TICK):
            assert wait_until(lambda: counter.calls >= 2)
            raise ZeroDivisionError("the engine threw")

    with pytest.raises(ZeroDivisionError):
        a_session_whose_engine_throws()

    settled = counter.calls
    time.sleep(TICK.total_seconds() * 20)

    assert counter.calls == settled, "a raising body left the thread beating"


@pytest.mark.parametrize(
    "lock_timeout",
    [dt.timedelta(0), dt.timedelta(microseconds=500), dt.timedelta(milliseconds=-1)],
)
def test_a_lock_timeout_that_rounds_to_zero_is_refused(lock_timeout: dt.timedelta) -> None:
    """⚠️ **Zero is not "do not wait" to Postgres, it is "wait for ever".** `SET LOCAL
    lock_timeout = 0` *disables* the timeout, so the one value that reads like the safest
    setting available is the one that restores the hang the setting exists to prevent — and it
    would do it in silence, on a healthy-looking session that quietly stops beating.

    Sub-millisecond durations are here for the same reason: they truncate to that same zero.

    No database is touched. The refusal happens while the beat is being built, before any
    connection exists, which is why this lives with the unit tests.
    """
    with pytest.raises(ValueError, match="at least a millisecond"):
        session_heartbeat(
            cast(sessionmaker[Session], None), uuid.uuid4(), lock_timeout=lock_timeout
        )


def test_stop_gives_up_rather_than_hanging_the_caller(caplog: pytest.LogCaptureFixture) -> None:
    """⚠️ The join has a bound, and the bound is deliberate. A beat wedged on something the
    thread cannot be interrupted out of must not make `stop()` block for ever — the caller is
    shutting a session down, and a shutdown that never returns is worse than a heartbeat that
    outlives it by a moment. The thread is a daemon precisely so that giving up here is safe.

    Reported at WARNING rather than raised: an exception would replace an orderly stop with a
    stack trace, and the caller is about to discover the same broken database on its own.
    """
    wedged = threading.Event()

    def beat_that_will_not_finish() -> None:
        wedged.set()
        time.sleep(1.0)

    heartbeat = Heartbeat(beat_that_will_not_finish, every=NEVER)
    heartbeat.start()
    assert wedged.wait(5), "the beat never started"

    with caplog.at_level(logging.WARNING):
        started = time.monotonic()
        heartbeat.stop(timeout=0.05)
        elapsed = time.monotonic() - started

    assert elapsed < 0.9, f"stop() waited {elapsed:.2f}s; it ignored its own timeout"
    assert "did not stop" in caplog.text, "giving up went unreported"


def test_the_default_clock_is_timezone_aware() -> None:
    """⚠️ `heartbeat_at` is `timestamptz`. A naive datetime does not fail on the way in — it is
    interpreted in the server's zone, so a beat written from a machine three hours off lands
    three hours off, and `is_stale` then measures a silence that never happened.
    """
    stamped = _utcnow()

    assert stamped.tzinfo is not None, "a naive beat is a beat in whatever zone Postgres assumes"
    assert stamped.utcoffset() == dt.timedelta(0)
