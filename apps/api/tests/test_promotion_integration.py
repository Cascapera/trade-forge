"""The promotion gate, against a real database — because both halves of it *are* the database.

The counting is a `SELECT` with a `DISTINCT` over a date cast; the floor underneath it is a
trigger. Neither can be tested against a double, and a double would only be a description of what
this author believes Postgres does with `date(last_bar_time)` across midnight — which is exactly
the kind of belief worth checking.

Run locally with:

    POSTGRES_DB=tradeforge_test uv run pytest -m integration \
        apps/api/tests/test_promotion_integration.py
"""

import datetime as dt
import uuid
from decimal import Decimal

import pytest
from sqlalchemy.exc import IntegrityError, InternalError
from sqlalchemy.orm import Session

from tradeforge_api.live.promotion import paper_days, promotion_for
from tradeforge_db.live_sessions import open_session
from tradeforge_db.models import (
    Instrument,
    LiveSession,
    LiveSessionStatus,
    SessionMode,
    Strategy,
)
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

NOON = dt.datetime(2026, 8, 26, 12, tzinfo=dt.UTC)

# The trigger raises through plpgsql, which SQLAlchemy surfaces as one of these depending on the
# error class the driver maps it to. Both are the same refusal.
REFUSED = (IntegrityError, InternalError)


@pytest.fixture
def plan(session: Session) -> tuple[uuid.UUID, uuid.UUID]:
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
    strategy = Strategy(definition={"schema_version": "1.0", "name": "S"}, version=1)
    session.add_all([instrument, strategy])
    session.flush()
    return strategy.id, instrument.id


def a_session(
    db: Session,
    plan: tuple[uuid.UUID, uuid.UUID],
    *,
    mode: SessionMode = SessionMode.PAPER,
    last_bar: dt.datetime | None = NOON,
) -> LiveSession:
    row = open_session(
        db,
        strategy_id=plan[0],
        instrument_id=plan[1],
        timeframe="M15",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        engine_version="0.1.0",
        mode=mode,
        warmup_bars=0,
        at=NOON,
    )
    row.last_bar_time = last_bar
    row.status = LiveSessionStatus.STOPPED
    row.stopped_at = NOON
    db.flush()
    return row


# --------------------------------------------------------------- the database's floor


def test_a_live_session_is_refused_by_the_database_with_no_paper_at_all(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ **The invariant, and it is not this application's to relax.** A trigger, applying to
    everyone — a second writer, a migration script, a `psql` session at three in the morning."""
    with pytest.raises(REFUSED, match="never completed a bar in paper"):
        a_session(session, plan, mode=SessionMode.LIVE)


def test_paper_that_never_saw_a_bar_does_not_open_the_gate(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ The separating half, and the one that makes the floor worth having.

    Without `last_bar_time IS NOT NULL` the gate is defeated by starting a session and killing
    it — a row that never saw a candle is a process that started, not a day of paper trading.
    """
    a_session(session, plan, last_bar=None)

    with pytest.raises(REFUSED, match="never completed a bar in paper"):
        a_session(session, plan, mode=SessionMode.LIVE)


def test_one_paper_day_with_a_bar_lifts_the_database_floor(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """The floor is *ever*, not *enough*. How many days is the application's question."""
    a_session(session, plan)

    live = a_session(session, plan, mode=SessionMode.LIVE)

    assert live.mode is SessionMode.LIVE


def test_a_different_strategy_does_not_inherit_the_paper_days(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """Per strategy is per strategy. A second strategy in the same database starts from nothing,
    however much paper the first one has."""
    a_session(session, plan)
    other = Strategy(definition={"schema_version": "1.0", "name": "T"}, version=1)
    session.add(other)
    session.flush()

    with pytest.raises(REFUSED, match="never completed a bar in paper"):
        a_session(session, (other.id, plan[1]), mode=SessionMode.LIVE)


# --------------------------------------------------------------- the application's policy


def test_days_are_distinct_dates_not_sessions(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ Six restarts on one Tuesday are one day of evidence. Counting rows would make the gate
    a measure of how often somebody pressed start."""
    for _ in range(6):
        a_session(session, plan, last_bar=NOON)

    assert paper_days(session, plan[0]) == 1


def test_three_days_count_as_three(session: Session, plan: tuple[uuid.UUID, uuid.UUID]) -> None:
    """The separating half of the one above: without it, "distinct" and "always one" agree."""
    for day in range(3):
        a_session(session, plan, last_bar=NOON + dt.timedelta(days=day))

    assert paper_days(session, plan[0]) == 3


def test_the_day_belongs_to_the_bar_not_to_the_session(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ They differ for a session running across midnight, and the bar's instant is the honest
    one: it says which day's market the strategy was actually watched trading.

    Both sessions here opened at noon on the 26th and one processed its last bar after midnight.
    The count is 2 only if the date comes from `last_bar_time`.
    """
    a_session(session, plan, last_bar=NOON)
    a_session(session, plan, last_bar=NOON + dt.timedelta(hours=13))

    assert paper_days(session, plan[0]) == 2


def test_a_live_session_does_not_count_towards_its_own_requirement(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ Otherwise the gate is a ratchet: one live session, however it got there, becomes the
    evidence for the next. Only paper counts as paper."""
    a_session(session, plan)
    a_session(session, plan, mode=SessionMode.LIVE, last_bar=NOON + dt.timedelta(days=1))

    assert paper_days(session, plan[0]) == 1


def test_a_session_that_never_saw_a_bar_is_not_a_day(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ The counting half of the rule the trigger enforces as a floor — and it needed its own
    test. Mutation said so: dropping `last_bar_time IS NOT NULL` from `paper_days` survived every
    other test in this file, because every other scenario gives its sessions a bar.

    Two rows here, one of them a process that started and died. The answer is 1.
    """
    a_session(session, plan, last_bar=NOON)
    a_session(session, plan, last_bar=None)

    assert paper_days(session, plan[0]) == 1


def test_the_count_is_this_strategy_s_alone(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ Also its own test, and for the same reason. The floor test above proves the *trigger*
    filters by strategy; nothing proved the *count* did, and dropping that filter survived.

    Three days of paper on a second strategy must leave this one where it was.
    """
    a_session(session, plan, last_bar=NOON)
    other = Strategy(definition={"schema_version": "1.0", "name": "T"}, version=1)
    session.add(other)
    session.flush()
    for day in range(3):
        a_session(session, (other.id, plan[1]), last_bar=NOON + dt.timedelta(days=day))

    assert paper_days(session, plan[0]) == 1
    assert paper_days(session, other.id) == 3


def test_the_boundary_is_exactly_n_not_one_short(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ **The off-by-one, pinned on both sides of the line.**

    Four days against a requirement of five must refuse and five must allow. Neither assertion
    alone catches `days >= required_days - 1`: with three days and five required, one-short still
    refuses, so a test that stops well below the boundary cannot see it. Mutation found this by
    surviving.
    """
    for day in range(4):
        a_session(session, plan, last_bar=NOON + dt.timedelta(days=day))

    assert not promotion_for(session, plan[0], mode=SessionMode.LIVE, required_days=5)

    a_session(session, plan, last_bar=NOON + dt.timedelta(days=4))

    assert promotion_for(session, plan[0], mode=SessionMode.LIVE, required_days=5)


def test_the_policy_refuses_until_enough_days_are_on_record(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    for day in range(3):
        a_session(session, plan, last_bar=NOON + dt.timedelta(days=day))

    refused = promotion_for(session, plan[0], mode=SessionMode.LIVE, required_days=5)

    assert not refused
    assert refused.days == 3
    assert "2 more trading day(s)" in refused.reason, "the refusal does not say how much is left"


def test_the_policy_allows_once_the_days_are_there(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    for day in range(5):
        a_session(session, plan, last_bar=NOON + dt.timedelta(days=day))

    assert promotion_for(session, plan[0], mode=SessionMode.LIVE, required_days=5)


def test_paper_never_needs_permission(session: Session, plan: tuple[uuid.UUID, uuid.UUID]) -> None:
    """A gate that made paper trading conditional would stand in front of the very thing it
    exists to require."""
    assert promotion_for(session, plan[0], mode=SessionMode.PAPER, required_days=5)


def test_a_requirement_of_zero_is_refused_rather_than_obeyed(
    session: Session, plan: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """⚠️ Not a looser policy — the absence of one, arriving through the field that was supposed
    to express it. The database's floor would still hold, so no account is exposed; what would be
    lost is the operator's ability to see they had switched the gate off."""
    for day in range(5):
        a_session(session, plan, last_bar=NOON + dt.timedelta(days=day))

    refused = promotion_for(session, plan[0], mode=SessionMode.LIVE, required_days=0)

    assert not refused
    assert "disabled one" in refused.reason
