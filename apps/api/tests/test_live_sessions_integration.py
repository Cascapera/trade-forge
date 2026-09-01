"""`/live-sessions` against a real Postgres and a real Redis.

These endpoints are almost entirely queries, so a unit test of them would be a test of SQLAlchemy
fakes. What is worth proving needs the database: that an open position is the trade with no exit,
that "today" is a UTC boundary and not the machine's, that the event log carries a refusal's
reason, and that a stop request written by the route is the one a session would read.

⚠️ **Redis on database 15**, for the reason `test_kill_switch_integration` gives: the key names
are the real ones because the name is the contract, but nothing in this system is configured to
use `/15`, so a run cannot reach a live session's real stop key.

Run with:  docker compose up -d  &&  POSTGRES_DB=tradeforge_test uv run pytest -m integration
"""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
import redis
from fastapi.testclient import TestClient
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.live.stop import stop_key, stop_requested
from tradeforge_api.main import create_app
from tradeforge_db.live_sessions import STALE_AFTER, open_session
from tradeforge_db.models import (
    ExitReason,
    Instrument,
    LiveSession,
    LiveSessionStatus,
    OrderAudit,
    OrderAuditStatus,
    SessionMode,
    Strategy,
    Trade,
)
from tradeforge_engine.domain import AssetClass, Side

from .test_live_session_acceptance import ma_cross_strategy

pytestmark = pytest.mark.integration

SANDBOX_DB = 15
SYMBOL = "EURUSD"


class _FakeQueue:
    async def enqueue_job(self, *args: Any, **options: Any) -> None:
        return None


@pytest.fixture
def store(settings: Settings) -> Iterator[redis.Redis]:
    client: redis.Redis = redis.Redis(
        host=settings.redis_host, port=settings.redis_port, db=SANDBOX_DB, decode_responses=True
    )
    client.flushdb()
    try:
        yield client
    finally:
        client.flushdb()
        client.close()


@pytest.fixture
def rows(session: Session) -> tuple[Strategy, Instrument]:
    instrument = Instrument(
        symbol=SYMBOL,
        name="Euro vs US Dollar",
        asset_class=AssetClass.FOREX,
        currency_base="EUR",
        currency_quote="USD",
        tick_size=Decimal("0.00001"),
        tick_value=Decimal("1"),
        contract_size=Decimal("100000"),
        digits=5,
    )
    strategy = Strategy(definition=ma_cross_strategy(), version=1)
    session.add_all([instrument, strategy])
    session.commit()
    return strategy, instrument


@pytest.fixture
def client(
    settings: Settings, session_factory: Callable[[], Session], store: redis.Redis
) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        arq_pool=_FakeQueue(),
        stop_store=store,
    )
    with TestClient(app) as test_client:
        yield test_client


def _open(
    session: Session,
    rows: tuple[Strategy, Instrument],
    *,
    at: dt.datetime,
    heartbeat_at: dt.datetime | None = None,
) -> LiveSession:
    strategy, instrument = rows
    row = open_session(
        session,
        strategy_id=strategy.id,
        instrument_id=instrument.id,
        timeframe="H1",
        initial_capital=Decimal("10000"),
        cost_model={"type": "none"},
        engine_version="0.1.0",
        mode=SessionMode.PAPER,
        warmup_bars=120,
        at=at,
    )
    row.heartbeat_at = heartbeat_at
    session.commit()
    return row


# --------------------------------------------------------------------------- #
# Listing                                                                      #
# --------------------------------------------------------------------------- #


def test_a_session_that_stopped_beating_is_listed_running_and_flagged_stale(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ **The single most important row on this screen, and `status` cannot say it.**

    A process that dies leaves its row saying `running`, because the thing that would update the
    status is the thing that died. A panel showing `status` alone reports the deadest sessions as
    the healthiest — so `stale` travels with every row, and it is `is_stale` called, not a
    comparison rewritten here.
    """
    now = dt.datetime.now(dt.UTC)
    _open(session, rows, at=now - dt.timedelta(hours=2), heartbeat_at=now - STALE_AFTER * 3)

    body = client.get("/live-sessions").json()

    assert body["total"] == 1
    (found,) = body["sessions"]
    assert found["status"] == "running", "nothing has corrected the row, and that is the point"
    assert found["stale"] is True
    assert found["silent_for_seconds"] >= STALE_AFTER.total_seconds() * 3


def test_a_beating_session_is_not_stale(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    now = dt.datetime.now(dt.UTC)
    _open(session, rows, at=now - dt.timedelta(hours=2), heartbeat_at=now)

    (found,) = client.get("/live-sessions").json()["sessions"]

    assert found["stale"] is False
    assert found["symbol"] == SYMBOL, "a panel showing an instrument uuid is a panel nobody reads"


def test_a_session_that_never_beat_is_silent_from_its_start(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ A NULL heartbeat is not freshness. It means the process died between opening the row and
    its first bar — the sessions that failed hardest — and treating it as fresh would leave
    exactly those marked healthy for ever.
    """
    now = dt.datetime.now(dt.UTC)
    _open(session, rows, at=now - STALE_AFTER * 5, heartbeat_at=None)

    (found,) = client.get("/live-sessions").json()["sessions"]

    assert found["heartbeat_at"] is None
    assert found["stale"] is True


def test_the_status_filter_selects(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    now = dt.datetime.now(dt.UTC)
    running = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)
    stopped = _open(session, rows, at=now - dt.timedelta(hours=3), heartbeat_at=now)
    stopped.status = LiveSessionStatus.STOPPED
    stopped.stopped_at = now
    session.commit()

    body = client.get("/live-sessions", params={"status": "running"}).json()

    assert [found["id"] for found in body["sessions"]] == [str(running.id)]
    assert body["total"] == 1, "the total counts the filter, not the table"


def test_sessions_come_back_newest_first(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    now = dt.datetime.now(dt.UTC)
    older = _open(session, rows, at=now - dt.timedelta(hours=5))
    newer = _open(session, rows, at=now - dt.timedelta(hours=1))

    body = client.get("/live-sessions").json()

    assert [found["id"] for found in body["sessions"]] == [str(newer.id), str(older.id)]


# --------------------------------------------------------------------------- #
# The detail                                                                   #
# --------------------------------------------------------------------------- #


def test_an_open_position_is_the_trade_with_no_exit(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """The table's own definition — *"An open position has no exit yet"* — read back as a body.

    ⚠️ Both trades belong to this session and only one is open, which is what separates this from
    an implementation that simply lists the session's trades.
    """
    _strategy, instrument = rows
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=4), heartbeat_at=now)
    session.add_all(
        [
            Trade(
                live_session_id=row.id,
                instrument_id=instrument.id,
                direction=Side.LONG,
                entry_time=now - dt.timedelta(hours=3),
                entry_price=Decimal("1.10000"),
                volume=Decimal("0.10"),
                stop_loss=Decimal("1.09000"),
                exit_time=now - dt.timedelta(hours=2),
                exit_price=Decimal("1.10500"),
                exit_reason=ExitReason.TAKE_PROFIT,
                gross_pnl=Decimal("50"),
                costs=Decimal("0"),
                net_pnl=Decimal("50"),
            ),
            Trade(
                live_session_id=row.id,
                instrument_id=instrument.id,
                direction=Side.SHORT,
                entry_time=now - dt.timedelta(hours=1),
                entry_price=Decimal("1.11000"),
                volume=Decimal("0.20"),
                stop_loss=Decimal("1.12000"),
            ),
        ]
    )
    session.commit()

    body = client.get(f"/live-sessions/{row.id}").json()

    (position,) = body["open_positions"]
    assert position["direction"] == "short"
    # ⚠️ `1.1100000000`, not the `1.11000` that was written. Money crosses this wire as **text**,
    # and the text is the column's own quantisation — `PRICE` carries ten decimal places. That is
    # the convention working, not a bug: the exact Decimal that survived Postgres survives to the
    # client, and a test asserting the literal it typed would be asserting something the database
    # never promised.
    assert position["entry_price"] == "1.1100000000"
    assert position["volume"] == "0.20000000"


def test_realised_today_counts_what_closed_since_midnight_utc(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ **UTC, the same midnight the executor's daily loss cap counts from.**

    Three clocks are live here — he is on UTC-3, the database on UTC, the broker on UTC+3 — and a
    panel totalling a different day from the cap that halts trading could explain neither number.
    The trade below closed yesterday in UTC and *today* in UTC-3, which is what makes the two
    readings separable at all.
    """
    _strategy, instrument = rows
    now = dt.datetime.now(dt.UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = _open(session, rows, at=midnight - dt.timedelta(days=2), heartbeat_at=now)
    session.add_all(
        [
            Trade(
                live_session_id=row.id,
                instrument_id=instrument.id,
                direction=Side.LONG,
                entry_time=midnight - dt.timedelta(hours=4),
                entry_price=Decimal("1.10000"),
                volume=Decimal("0.10"),
                exit_time=midnight - dt.timedelta(hours=1),
                exit_price=Decimal("1.09000"),
                exit_reason=ExitReason.STOP_LOSS,
                gross_pnl=Decimal("-100"),
                costs=Decimal("0"),
                net_pnl=Decimal("-100"),
            ),
            Trade(
                live_session_id=row.id,
                instrument_id=instrument.id,
                direction=Side.LONG,
                entry_time=midnight + dt.timedelta(minutes=5),
                entry_price=Decimal("1.10000"),
                volume=Decimal("0.10"),
                exit_time=midnight + dt.timedelta(minutes=30),
                exit_price=Decimal("1.10300"),
                exit_reason=ExitReason.TAKE_PROFIT,
                gross_pnl=Decimal("30"),
                costs=Decimal("0"),
                net_pnl=Decimal("30"),
            ),
        ]
    )
    session.commit()

    body = client.get(f"/live-sessions/{row.id}").json()

    assert body["realised_today"] == "30.00000000", "yesterday's loss is not today's"
    assert body["trades_closed_today"] == 1


def test_a_trade_closed_exactly_at_midnight_belongs_to_the_new_day(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """The boundary, and it was found by a mutant rather than by thinking.

    Every other case in this file sits comfortably on one side of midnight, so `>= since` and
    `> since` are the same query for all of them: the mutant survived. A trade whose `exit_time`
    is *exactly* midnight is the only fixture that separates them, and the reading that is right
    is the inclusive one — 00:00:00 is the first instant of today, not the last of yesterday.

    Silent if wrong, and wrong in the direction that matters at the worst moment: the trade that
    opens the day is the one missing from the day's total.
    """
    _strategy, instrument = rows
    now = dt.datetime.now(dt.UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    row = _open(session, rows, at=midnight - dt.timedelta(days=1), heartbeat_at=now)
    session.add(
        Trade(
            live_session_id=row.id,
            instrument_id=instrument.id,
            direction=Side.LONG,
            entry_time=midnight - dt.timedelta(hours=2),
            entry_price=Decimal("1.10000"),
            volume=Decimal("0.10"),
            exit_time=midnight,
            exit_price=Decimal("1.10700"),
            exit_reason=ExitReason.TAKE_PROFIT,
            gross_pnl=Decimal("70"),
            costs=Decimal("0"),
            net_pnl=Decimal("70"),
        )
    )
    session.commit()

    body = client.get(f"/live-sessions/{row.id}").json()

    assert body["trades_closed_today"] == 1
    assert body["realised_today"] == "70.00000000"


def test_a_day_with_no_closed_trades_reports_zero_and_says_so(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ Zero here is a real statement, not an absence — nothing was closed, so nothing was
    realised. `trades_closed_today` is beside it because zero from no trades and zero from two
    trades that cancelled out are different days, and a screen showing one number cannot tell
    them apart.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)

    body = client.get(f"/live-sessions/{row.id}").json()

    # ⚠️ `"0"` here and `"30.00000000"` above, and the difference is honest: that one is a
    # `MONEY` column read back at its stored scale, this one is a total of nothing that never
    # touched a column. A client formats money anyway; a body that faked the scale would be
    # claiming a precision no row supports.
    assert body["realised_today"] == "0"
    assert body["trades_closed_today"] == 0
    assert body["open_positions"] == []


def test_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.get(f"/live-sessions/{uuid.uuid4()}").status_code == 404


# --------------------------------------------------------------------------- #
# The event log                                                                #
# --------------------------------------------------------------------------- #


def test_the_event_log_carries_the_rule_that_refused(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """*"Why did my strategy stop trading at 11am"* is the question this table exists to answer,
    and the answer is `reason`. The database itself refuses a refusal with no reason.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=2), heartbeat_at=now)
    session.add_all(
        [
            OrderAudit(
                live_session_id=row.id,
                client_id="zone-1",
                status=OrderAuditStatus.REFUSED,
                request={"symbol": SYMBOL, "volume": "0.20"},
                reason="kill switch engaged (redis:executor:kill-switch)",
                requested_at=now - dt.timedelta(minutes=10),
                resolved_at=now - dt.timedelta(minutes=10),
            ),
            OrderAudit(
                live_session_id=row.id,
                client_id="zone-2",
                status=OrderAuditStatus.FILLED,
                request={"symbol": SYMBOL, "volume": "0.10"},
                response={"retcode": 10009},
                requested_at=now - dt.timedelta(minutes=2),
                resolved_at=now - dt.timedelta(minutes=2),
            ),
        ]
    )
    session.commit()

    body = client.get(f"/live-sessions/{row.id}/events").json()

    assert body["total"] == 2
    newest, older = body["events"]
    assert newest["client_id"] == "zone-2", "a log is opened because something just happened"
    assert newest["reason"] is None, "no refusal happened, which is not the same as an empty one"
    assert older["reason"].startswith("kill switch engaged")
    assert older["response"] is None, "a refusal never reached MT5, so it has nothing it said"


def test_events_for_an_unknown_session_are_a_404_not_an_empty_page(client: TestClient) -> None:
    """⚠️ An empty page would say "this session did nothing"; the truth is "there is no session".

    The same distinction `/symbols/{symbol}/history` already makes, and for the same reason: one
    of those invites somebody to keep waiting and the other does not.
    """
    assert client.get(f"/live-sessions/{uuid.uuid4()}/events").status_code == 404


# --------------------------------------------------------------------------- #
# Stopping                                                                     #
# --------------------------------------------------------------------------- #


def test_stopping_writes_the_request_a_session_would_read(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument], store: redis.Redis
) -> None:
    """The acceptance criterion, with nothing faked between the route and the reader.

    `stop_requested` is the function the session process consults through `stop_predicate`, run
    here over the same real client and the same real key.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)
    assert stop_requested(store, row.id) is False

    body = client.post(f"/live-sessions/{row.id}/stop").json()

    assert stop_requested(store, row.id) is True
    assert body["stop_requested_at"] is not None
    assert store.get(stop_key(row.id)) is not None


def test_a_stopped_session_still_reports_running_until_it_notices(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ **The route does not change the row, and the body says so honestly.**

    Only the session writes `stopped_at`, when it has actually finished. A route that flipped the
    status would be reporting an outcome it cannot observe — and a session whose process is
    already dead would then show `stopped` while its position sat unmanaged at the venue.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)

    body = client.post(f"/live-sessions/{row.id}/stop").json()

    assert body["status"] == "running"
    assert body["stopped_at"] is None
    assert body["stop_requested_at"] is not None


def test_stopping_twice_keeps_the_session_stopping(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument], store: redis.Redis
) -> None:
    """A screen that lost the response and retried must not turn a stop into an error."""
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)

    first = client.post(f"/live-sessions/{row.id}/stop")
    second = client.post(f"/live-sessions/{row.id}/stop")

    assert (first.status_code, second.status_code) == (200, 200)
    assert stop_requested(store, row.id) is True


def test_stopping_one_session_leaves_the_others_alone(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument], store: redis.Redis
) -> None:
    """⚠️ The whole reason the key is per session, checked end to end rather than in a unit.

    An implementation on one global key would pass every other test in this file and wind down
    every strategy on the box, each reporting a clean deliberate stop.
    """
    now = dt.datetime.now(dt.UTC)
    one = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)
    two = _open(session, rows, at=now - dt.timedelta(hours=2), heartbeat_at=now)

    client.post(f"/live-sessions/{one.id}/stop")

    assert stop_requested(store, one.id) is True
    assert stop_requested(store, two.id) is False
    assert client.get(f"/live-sessions/{two.id}").json()["stop_requested_at"] is None


def test_stopping_a_session_that_already_ended_is_a_409(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument], store: redis.Redis
) -> None:
    """⚠️ Not a cheerful 200. Answering success would let a screen report a stop it did not cause.

    And nothing is written: a request recorded against a finished session is a key nobody will
    ever read, left where the next reader has to work out that it means nothing.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=3), heartbeat_at=now)
    row.status = LiveSessionStatus.STOPPED
    row.stopped_at = now
    session.commit()

    response = client.post(f"/live-sessions/{row.id}/stop")

    assert response.status_code == 409
    assert "not running" in response.json()["detail"]
    assert stop_requested(store, row.id) is False


def test_stopping_an_unknown_session_is_a_404(client: TestClient) -> None:
    assert client.post(f"/live-sessions/{uuid.uuid4()}/stop").status_code == 404


# --------------------------------------------------------------------------- #
# When Redis is not there                                                      #
# --------------------------------------------------------------------------- #


class _BrokenRedis:
    """A store that cannot be reached, raising what redis-py raises."""

    def set(self, name: Any, value: Any) -> bool:
        raise RedisConnectionError("Error 111 connecting to localhost:6379.")

    def exists(self, *names: Any) -> int:
        raise RedisConnectionError("Error 111 connecting to localhost:6379.")

    def get(self, name: Any) -> str | None:
        raise RedisConnectionError("Error 111 connecting to localhost:6379.")


@pytest.fixture
def blind_client(
    settings: Settings, session_factory: Callable[[], Session]
) -> Iterator[TestClient]:
    app = create_app(
        settings=settings,
        session_factory=session_factory,
        arq_pool=_FakeQueue(),
        stop_store=_BrokenRedis(),
    )
    with TestClient(app) as test_client:
        yield test_client


def test_the_list_survives_a_redis_that_is_gone(
    blind_client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ **The property the whole endpoint split exists for.**

    The list is the screen somebody opens when they suspect something is wrong, so it must not be
    capable of failing for a reason that has nothing to do with the sessions. It never asks Redis
    anything, and this proves it by taking Redis away entirely.
    """
    now = dt.datetime.now(dt.UTC)
    _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)

    response = blind_client.get("/live-sessions")

    assert response.status_code == 200
    assert response.json()["total"] == 1


def test_the_detail_refuses_to_guess_at_the_stop_state(
    blind_client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ **503, never `stop_requested_at: null`.**

    `null` on that field means "nobody asked". This is the screen an operator is on when they are
    deciding whether to press stop, and telling them nobody asked when the truth is unknown is
    the one error that changes what they do next — they press again, or they walk away believing
    a request was lost.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)

    response = blind_client.get(f"/live-sessions/{row.id}")

    assert response.status_code == 503
    assert "stop state is unknown" in response.json()["detail"]


def test_a_stop_that_could_not_be_recorded_is_never_a_cheerful_200(
    blind_client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """The request was not written, so nothing will act on it.

    ⚠️ And the detail says the session is **still running**, because it is. A 503 that only said
    "failed" would leave an operator unsure whether the stop half-landed.
    """
    now = dt.datetime.now(dt.UTC)
    row = _open(session, rows, at=now - dt.timedelta(hours=1), heartbeat_at=now)

    response = blind_client.post(f"/live-sessions/{row.id}/stop")

    assert response.status_code == 503
    assert "still running" in response.json()["detail"]


def test_a_404_is_answered_before_redis_is_ever_asked(blind_client: TestClient) -> None:
    """An unknown session is 404 even with Redis gone: the row is checked first.

    ⚠️ Not an ordering nicety. Reaching for Redis first would turn every mistyped id into a 503
    while Redis was down — an availability problem wearing the clothes of a lookup failure.
    """
    assert blind_client.get(f"/live-sessions/{uuid.uuid4()}").status_code == 404
    assert blind_client.post(f"/live-sessions/{uuid.uuid4()}/stop").status_code == 404
