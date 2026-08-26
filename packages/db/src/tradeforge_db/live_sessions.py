"""Recording a live session and, above all, noticing when one stops existing.

Written from one side only, unlike `collections` next door: the session process creates its own
row and moves it along. There is no queue and no second writer — a row appears because something
is already running (`LiveSession`'s docstring says why there is no `queued`).

**Which is exactly the problem this module is mostly about.** The thing that would mark a session
`stopped` is the thing that died. A killed process, a closed laptop, an OOM — all of them leave a
row saying `running` for ever, and a panel reading `status` reports a session that has not
existed since Tuesday.

⚠️ `last_bar_time` cannot rescue that, and it is worth being precise about why. It advances when
the engine *finishes a bar*, so on H4 a healthy session's stamp is up to four hours old. "Four
hours old" and "dead" are the same reading. This project already owns that mistake once: the
collector agent's arq health check refreshes on a 3600-second default and does not move when a
job completes, so a frozen reading means nothing inside the hour (`specs/backlog.md`), and a
sample taken over twenty-four seconds nearly had a healthy agent declared stuck.

So a session beats on a cadence it chooses, and `reconcile_stale` is what reads those beats and
tells the truth about the rows nobody is writing any more.
"""

import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tradeforge_db.models import LiveSession, LiveSessionStatus, SessionMode

__all__ = [
    "BEAT_EVERY",
    "STALE_AFTER",
    "beat",
    "finish_session",
    "is_stale",
    "open_session",
    "reconcile_stale",
    "running_sessions",
    "silence",
]

# How long a session may go unheard before `reconcile_stale` calls it dead.
#
# ⚠️ This is a *multiple* of the beat, not a duration somebody liked. A session writing every
# `BEAT_EVERY` seconds must survive an ordinary hiccup — a slow commit, a GC pause, a database
# failover — without being declared dead, and four missed beats is the smallest margin that
# feels like a fault rather than a stutter. Raise the beat and this follows it; the two numbers
# are one decision and must not drift apart.
BEAT_EVERY = dt.timedelta(seconds=15)
STALE_AFTER = BEAT_EVERY * 4


def open_session(  # noqa: PLR0913 — keyword-only; each names one column of the row
    session: Session,
    *,
    strategy_id: uuid.UUID,
    instrument_id: uuid.UUID,
    timeframe: str,
    initial_capital: Decimal,
    cost_model: dict[str, Any],
    engine_version: str,
    mode: SessionMode = SessionMode.PAPER,
    warmup_bars: int,
    at: dt.datetime,
) -> LiveSession:
    """Write the row for a session that is **about to start**, and hand it back.

    ⚠️ Called after the warm-up, not before — `warmup_bars` is what the seed actually used, and
    a row written first would have to be updated with it, which is a second write that can fail
    on its own. A session whose warm-up raised never gets a row at all, which is the honest
    outcome: nothing ran.

    `heartbeat_at` is left NULL. The loop sets it on its first beat, so a row with a status of
    `running` and no heartbeat means the process died between opening and its first bar — a
    state `reconcile_stale` recognises rather than one it has to guess at.

    ⚠️ **`mode` defaults to paper, and the default is load-bearing.** Every existing caller means
    paper, and a required argument would have made each of them state it — which sounds tidier
    until you notice that the day somebody adds a caller, the safe value is the one they have to
    remember. A default that fails safe costs nothing; one that fails open costs an account.

    ⚠️ A `live` row is refused by the database itself unless the strategy has completed a bar in
    paper (rev_0016). That refusal is an invariant, not this function's policy: how *many* days
    are required is the application's to decide, and it decides before calling this.
    """
    row = LiveSession(
        strategy_id=strategy_id,
        instrument_id=instrument_id,
        timeframe=timeframe,
        mode=mode,
        status=LiveSessionStatus.RUNNING,
        initial_capital=initial_capital,
        cost_model=cost_model,
        engine_version=engine_version,
        warmup_bars=warmup_bars,
        started_at=at,
    )
    session.add(row)
    session.flush()
    return row


def beat(session: Session, session_id: uuid.UUID, *, at: dt.datetime) -> None:
    """Say the process is alive, now.

    Deliberately separate from `last_bar_time`: one says the engine finished a bar, the other
    says the process exists. Conflating them is how a healthy H4 session reads as dead.
    """
    row = session.get(LiveSession, session_id)
    if row is None:
        return
    row.heartbeat_at = at


def finish_session(
    session: Session,
    session_id: uuid.UUID,
    *,
    at: dt.datetime,
    error: str | None = None,
) -> None:
    """Stop a session, cleanly or otherwise. `error` decides which.

    The `error_iff_failed` CHECK makes the two inseparable at the database, so this cannot
    record a failure with no reason or a clean stop that carries one.
    """
    row = session.get(LiveSession, session_id)
    if row is None:
        return
    row.status = LiveSessionStatus.FAILED if error else LiveSessionStatus.STOPPED
    row.stopped_at = at
    row.error = error


def running_sessions(session: Session) -> list[LiveSession]:
    """Every row that still claims to be running, oldest first.

    ⚠️ *Claims*. A row saying `running` is a row nobody has updated, which is not the same as a
    process that is up — see the module docstring. `reconcile_stale` is what turns this into an
    answer.
    """
    return list(
        session.execute(
            select(LiveSession)
            .where(LiveSession.status == LiveSessionStatus.RUNNING)
            .order_by(LiveSession.started_at)
        )
        .scalars()
        .all()
    )


def silence(
    *, heartbeat_at: dt.datetime | None, started_at: dt.datetime, now: dt.datetime
) -> dt.timedelta:
    """How long a session has gone unheard.

    **Measured from the last beat, or from the start when there has never been one.** Both are
    real states and they mean different things — "it was running and stopped" versus "it never
    got as far as its first bar" — but they are silence either way, and treating a missing
    heartbeat as *fresh* would leave exactly the sessions that died at start-up marked `running`
    for ever: the ones that failed hardest would look the healthiest.

    A duration, and nothing more — the *decision* about that duration is `is_stale`. Keeping
    them apart means the number can be reported ("silent for 3m20s") by callers that are not
    asking whether to kill anything.
    """
    return now - (heartbeat_at or started_at)


def is_stale(
    *,
    heartbeat_at: dt.datetime | None,
    started_at: dt.datetime,
    now: dt.datetime,
    stale_after: dt.timedelta = STALE_AFTER,
) -> bool:
    """Whether a session has been silent long enough to be called dead. **The boundary is here.**

    Silent for *exactly* `stale_after` is stale: the comparison is `>=`. Neither reading is
    obviously right from the name, which is why it is one function with one test rather than a
    `<` inlined in a loop.

    ⚠️ Pure, and separate from `reconcile_stale`, which is the whole point. This is where the
    off-by-one lives, and `reconcile_stale` needs Postgres to run — so a boundary left inline
    there can only be tested through a database, and the coverage gate deselects integration
    tests (`ci.yml` runs plain `pytest` with a 90% floor). That is not hypothetical: the test
    that was meant to pin this boundary had nothing to call, so it wrote the comparison out in
    its own body and pinned itself instead. A `<` → `<=` mutant survived both suites.
    """
    return silence(heartbeat_at=heartbeat_at, started_at=started_at, now=now) >= stale_after


def reconcile_stale(
    session: Session, *, now: dt.datetime, stale_after: dt.timedelta = STALE_AFTER
) -> list[LiveSession]:
    """Mark every `running` session that has stopped beating as `failed`. Returns what it marked.

    **Silence is measured from the last beat, or from the start when there has never been one.**
    Both are real states and they mean different things — "it was running and stopped" versus "it
    never got as far as its first bar" — but the reconciliation is the same, and treating a
    missing heartbeat as *fresh* would leave exactly the sessions that died at start-up marked
    `running` for ever.

    ⚠️ **`failed`, not `stopped`.** A session that stopped beating did not stop; it was stopped
    *for* it, by something nobody chose. Recording that as a clean stop would put it beside the
    sessions somebody ended on purpose, and the difference is the one an operator is looking
    for. The message says what was actually observed — the last time it was heard from — rather
    than guessing a cause it has no way to know.
    """
    marked: list[LiveSession] = []
    for row in running_sessions(session):
        heard = row.heartbeat_at or row.started_at
        if not is_stale(
            heartbeat_at=row.heartbeat_at,
            started_at=row.started_at,
            now=now,
            stale_after=stale_after,
        ):
            continue
        row.status = LiveSessionStatus.FAILED
        row.stopped_at = now
        row.error = (
            f"no heartbeat since {heard.isoformat()} "
            f"({(now - heard).total_seconds():.0f}s, limit {stale_after.total_seconds():.0f}s); "
            "the process is gone and did not get to say so"
        )
        marked.append(row)
    return marked
