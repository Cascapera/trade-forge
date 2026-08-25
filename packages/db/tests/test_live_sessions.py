"""The staleness decision, without a database.

`reconcile_stale` walks rows and needs Postgres to prove; the *decision* it makes on each one
does not, and that is where the off-by-one lives. Separated so it runs in milliseconds — and
because the coverage gate deselects integration tests (`ci.yml` runs plain `pytest` with a 90%
floor), so a rule reachable only through a database counts as uncovered and stops being measured
at all.

`test_constraints_integration.py` still proves the walk, the writes and the CHECKs.
"""

import datetime as dt

import pytest

from tradeforge_db.live_sessions import BEAT_EVERY, STALE_AFTER, is_stale, silence

NOON = dt.datetime(2026, 8, 24, 12, tzinfo=dt.UTC)


def test_silence_is_measured_from_the_last_beat() -> None:
    """The ordinary case: a session that has been beating and then stopped."""
    heard = NOON - dt.timedelta(minutes=5)

    assert silence(heartbeat_at=heard, started_at=NOON - dt.timedelta(days=1), now=NOON) == (
        dt.timedelta(minutes=5)
    )


def test_silence_is_measured_from_the_start_when_nothing_ever_beat() -> None:
    """⚠️ The state a crash at start-up leaves: `running`, and no heartbeat, ever.

    Treating a missing beat as *fresh* would leave exactly those rows marked running for ever —
    the sessions that failed hardest would be the ones that looked healthiest.
    """
    started = NOON - dt.timedelta(hours=3)

    assert silence(heartbeat_at=None, started_at=started, now=NOON) == dt.timedelta(hours=3)


def test_a_beat_wins_over_the_start_even_when_it_is_older() -> None:
    """⚠️ Not a hypothetical to dismiss: a clock adjustment, or a row whose `started_at` was
    written by the database's `now()` while the beat came from the process, can put them out of
    order. The rule stays "the beat is what the process said", because the start is not evidence
    that anything is still running — it is evidence that something once was.
    """
    beat_time = NOON - dt.timedelta(hours=2)
    started_later = NOON - dt.timedelta(minutes=1)

    assert silence(heartbeat_at=beat_time, started_at=started_later, now=NOON) == (
        dt.timedelta(hours=2)
    )


@pytest.mark.parametrize(
    ("gap", "stale"),
    [
        (STALE_AFTER - dt.timedelta(seconds=1), False),
        (STALE_AFTER, True),
        (STALE_AFTER + dt.timedelta(seconds=1), True),
    ],
)
def test_the_boundary_is_where_it_says_it_is(gap: dt.timedelta, stale: bool) -> None:
    """One second either side of the line, and the line itself.

    ⚠️ The middle case is the one that matters: silent for *exactly* `STALE_AFTER` is stale, and
    a `<=` on the other side of the comparison would keep it alive for one more beat. Neither
    reading is obviously right from the name, which is why it is pinned rather than assumed.

    ⚠️ **This calls `is_stale`; it does not re-derive it.** The version before this one asserted
    `silence(...) >= STALE_AFTER`, which pins the `>=` written *in the test* and says nothing
    about the module — a `<` → `<=` mutant in `reconcile_stale` survived this file and all 76
    integration tests. Writing the rule out in the assertion is how a test comes to agree with
    itself.
    """
    assert is_stale(heartbeat_at=NOON - gap, started_at=NOON - gap, now=NOON) is stale


def test_a_session_heard_from_just_now_is_not_silent_at_all() -> None:
    assert silence(heartbeat_at=NOON, started_at=NOON, now=NOON) == dt.timedelta(0)


def test_the_stale_window_is_a_multiple_of_the_beat() -> None:
    """The two constants are one decision. A `STALE_AFTER` that stopped being a multiple of
    `BEAT_EVERY` would mean sessions declared dead after fewer missed beats than anyone intended,
    and the change would be invisible — both are plain module-level constants.

    Four beats is the margin: enough that a slow commit, a GC pause or a database failover reads
    as a stutter rather than a fault.
    """
    assert STALE_AFTER == BEAT_EVERY * 4
    assert dt.timedelta(0) < BEAT_EVERY, "a beat of zero makes every session instantly stale"


def test_a_caller_can_narrow_the_window_and_the_argument_is_the_one_used() -> None:
    """⚠️ Separating on purpose: thirty seconds is *fresh* under the default `STALE_AFTER` and
    stale under a ten-second window. A probe that used the default value would agree with an
    implementation that ignored the argument entirely — and nothing in the codebase passes
    `stale_after`, so this parameter had no proof at all until here.
    """
    gap = dt.timedelta(seconds=30)
    assert gap < STALE_AFTER, "the fixture stopped separating; pick a gap under the default"

    assert is_stale(heartbeat_at=NOON - gap, started_at=NOON - gap, now=NOON) is False
    assert (
        is_stale(
            heartbeat_at=NOON - gap,
            started_at=NOON - gap,
            now=NOON,
            stale_after=dt.timedelta(seconds=10),
        )
        is True
    )
