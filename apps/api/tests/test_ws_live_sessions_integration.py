"""`WS /ws/live-sessions/{id}` against a real Postgres and a real Redis.

The socket reads a stream and a row, and neither can be faked without faking the thing under
test. What this file plays is the **executor's** part: it publishes to `venue.outcomes` with a
plain client, the way the executor does, and asserts what reaches the panel.

⚠️ **This writes to the real `venue.outcomes` on database 0**, because the endpoint builds its
own client and that is where live sessions actually talk. Every entry it adds is `XDEL`ed in
teardown — `test_broker_integration` does the same thing without cleaning up, which is how this
project ended up with 503 stale entries and a broken gate on an unrelated PR.

Run with:  docker compose up -d  &&  POSTGRES_DB=tradeforge_test uv run pytest -m integration
"""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any, cast

import pytest
import redis
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api import ws
from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.live_sessions import STALE_AFTER, open_session
from tradeforge_db.models import Instrument, LiveSession, LiveSessionStatus, SessionMode, Strategy
from tradeforge_engine.domain import AssetClass
from tradeforge_executor.wire import (
    VENUE_OUTCOMES,
    WireFill,
    WireRefusal,
    fill_fields,
    refusal_fields,
)

from .test_live_session_acceptance import ma_cross_strategy

pytestmark = pytest.mark.integration

SYMBOL = "EURUSD"


class _FakeQueue:
    async def enqueue_job(self, *args: Any, **options: Any) -> None:
        return None


class Publisher:
    """The executor's half of the conversation, and a record of what to clean up."""

    def __init__(self, client: redis.Redis) -> None:
        self._client = client
        self.ids: list[str] = []

    def fill(self, session_id: uuid.UUID, *, client_id: str = "zone-1") -> None:
        self._add(
            fill_fields(
                WireFill(
                    client_id=client_id,
                    session_id=str(session_id),
                    symbol=SYMBOL,
                    at=dt.datetime.now(dt.UTC),
                    price=Decimal("1.10500"),
                    volume=Decimal("0.10"),
                    spread=Decimal("0.00002"),
                    ticket=987654,
                )
            )
        )

    def refusal(self, session_id: uuid.UUID, *, reason: str, by_venue: bool = False) -> None:
        self._add(
            refusal_fields(
                WireRefusal(
                    client_id="zone-2",
                    session_id=str(session_id),
                    at=dt.datetime.now(dt.UTC),
                    reason=reason,
                    by_venue=by_venue,
                )
            )
        )

    def rubbish(self) -> None:
        """An entry no reader can parse. It has to be survivable, not merely unlikely."""
        self._add({"kind": "something-nobody-wrote-a-parser-for", "session_id": "?"})

    def _add(self, fields: dict[str, str]) -> None:
        entry = self._client.xadd(VENUE_OUTCOMES, cast("dict[Any, Any]", fields))
        self.ids.append(str(entry))


@pytest.fixture
def publisher(settings: Settings) -> Iterator[Publisher]:
    client: redis.Redis = redis.Redis(
        host=settings.redis_host, port=settings.redis_port, decode_responses=True
    )
    made = Publisher(client)
    try:
        yield made
    finally:
        if made.ids:
            client.xdel(VENUE_OUTCOMES, *made.ids)
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
def client(settings: Settings, session_factory: Callable[[], Session]) -> Iterator[TestClient]:
    app = create_app(settings=settings, session_factory=session_factory, arq_pool=_FakeQueue())
    with TestClient(app) as test_client:
        yield test_client


def _open(session: Session, rows: tuple[Strategy, Instrument], **extra: Any) -> LiveSession:
    strategy, instrument = rows
    now = dt.datetime.now(dt.UTC)
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
        at=now - dt.timedelta(hours=1),
    )
    row.heartbeat_at = now
    for name, value in extra.items():
        setattr(row, name, value)
    session.commit()
    return row


# --------------------------------------------------------------------------- #
# What the socket says first                                                   #
# --------------------------------------------------------------------------- #


def test_the_first_frame_is_the_session_itself(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ Not `subscribed`, which is what the backtest feed sends and would be a wasted frame here.

    A panel opening this socket has to know what it is looking at *before* the first event, and
    on a quiet session the first event may be hours away. The frame carries the same shape
    `GET /live-sessions` returns, from the same `session_fields`, so the screen has one vocabulary.
    """
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        frame = socket.receive_json()

    assert frame["type"] == "state"
    assert frame["session"]["id"] == str(row.id)
    assert frame["session"]["symbol"] == SYMBOL
    assert frame["session"]["status"] == "running"
    assert frame["session"]["stale"] is False


def test_an_unknown_session_is_told_so_rather_than_left_waiting(client: TestClient) -> None:
    """⚠️ A socket that accepted and then said nothing is indistinguishable from a quiet session.

    A panel would sit on a spinner for ever, on a session that does not exist.
    """
    with client.websocket_connect(f"/ws/live-sessions/{uuid.uuid4()}") as socket:
        frame = socket.receive_json()

    assert frame == {"type": "error", "detail": "no such session"}


def test_a_session_that_already_ended_gets_its_state_and_the_feed_closes(
    client: TestClient, session: Session, rows: tuple[Strategy, Instrument]
) -> None:
    """⚠️ The state still goes out first, and then the socket closes.

    Closing with nothing would leave a panel unable to say *why* it has no feed — "the session
    finished" and "the server refused me" look the same from the client's side.
    """
    row = _open(
        session,
        rows,
        status=LiveSessionStatus.STOPPED,
        stopped_at=dt.datetime.now(dt.UTC),
    )

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        frame = socket.receive_json()
        assert frame["session"]["status"] == "stopped"

        with pytest.raises(Exception):  # noqa: B017, PT011 — a disconnect of some flavour
            socket.receive_json()


# --------------------------------------------------------------------------- #
# The events                                                                   #
# --------------------------------------------------------------------------- #


def test_a_fill_published_by_the_executor_reaches_the_panel(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
) -> None:
    """The acceptance criterion of PR-304 in one test: *fill -> painel atualiza via WS*.

    Published after the first frame is in hand, so there is no race: the socket read the stream's
    tail before it read the row, which means anything added from here on is delivered.
    """
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        publisher.fill(row.id)
        event = socket.receive_json()

    assert event["type"] == "fill"
    assert event["client_id"] == "zone-1"
    assert event["price"] == "1.10500"


def test_another_sessions_fill_never_reaches_this_panel(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
) -> None:
    """⚠️ **`venue.outcomes` is fan-out**, so this socket is offered every session's outcomes.

    The other session's fill is published *first*. If the filter were missing, the next frame
    would be that one — and it would look completely ordinary, because a fill is a fill. The only
    way to see the bug is to make the wrong answer arrive earlier than the right one.
    """
    row = _open(session, rows)
    someone_else = uuid.uuid4()

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        publisher.fill(someone_else, client_id="not-mine")
        publisher.fill(row.id, client_id="mine")
        event = socket.receive_json()

    assert event["client_id"] == "mine"


def test_a_refusal_reaches_the_panel_with_the_rule_that_refused(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
) -> None:
    """*"Why did my strategy stop trading at 11am"* has to be answerable on the screen, live."""
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        publisher.refusal(row.id, reason="kill switch engaged (redis:executor:kill-switch)")
        event = socket.receive_json()

    assert event["type"] == "refusal"
    assert event["reason"].startswith("kill switch engaged")
    assert event["by_venue"] is False


def test_an_unreadable_entry_does_not_take_the_panel_down(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
) -> None:
    """⚠️ The rubbish is published **between** two good entries, which is the only ordering that
    proves the socket survived it rather than merely tolerated it at the end.
    """
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        publisher.fill(row.id, client_id="before")
        publisher.rubbish()
        publisher.fill(row.id, client_id="after")

        assert socket.receive_json()["client_id"] == "before"
        assert socket.receive_json()["client_id"] == "after"


# --------------------------------------------------------------------------- #
# Noticing that a session ended                                                #
# --------------------------------------------------------------------------- #


def test_a_session_that_stops_pushes_a_state_and_closes(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️ **Nothing publishes "I have stopped"**, so this is the tick, not an event.

    The row is the only place a stop is recorded, so the loop looks at it every `LIVE_BLOCK_MS`.
    Shortened here rather than waiting fifteen real seconds — it is the same code path, and the
    interval is a constant precisely so a test can say so.
    """
    monkeypatch.setattr(ws, "LIVE_BLOCK_MS", 200)
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        row.status = LiveSessionStatus.STOPPED
        row.stopped_at = dt.datetime.now(dt.UTC)
        session.commit()

        frame = socket.receive_json()
        assert frame["type"] == "state"
        assert frame["session"]["status"] == "stopped"

        with pytest.raises(Exception):  # noqa: B017, PT011 — a disconnect of some flavour
            socket.receive_json()


def test_a_session_that_stops_beating_is_reported_stale_without_anybody_publishing_it(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **The failure this feed exists to make visible.**

    A session whose process died publishes nothing — the thing that would say so is the thing
    that died — and its row goes on saying `running` for ever. Without the tick, a panel would
    show a corpse as healthy until somebody reloaded the page.

    The heartbeat is pushed into the past rather than time being waited out; `is_stale` owns the
    threshold and this test does not restate it.
    """
    monkeypatch.setattr(ws, "LIVE_BLOCK_MS", 200)
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["session"]["stale"] is False

        row.heartbeat_at = dt.datetime.now(dt.UTC) - STALE_AFTER * 3
        session.commit()

        frame = socket.receive_json()

    assert frame["type"] == "state"
    assert frame["session"]["stale"] is True
    assert frame["session"]["status"] == "running", "nothing has corrected the row yet"


def test_a_healthy_quiet_session_says_nothing_on_the_tick(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """⚠️⚠️ **The test that stops "on change" from meaning "on a timer" — and the first version of
    it did not, which a mutant proved.**

    The bug being pinned is mine: comparing whole states. A state carries clocks —
    `silent_for_seconds` ticks by construction, and `heartbeat_at` is *rewritten every
    `BEAT_EVERY` by a session that is perfectly fine*. Either one in the comparison turns this
    feed into a frame every fifteen seconds for ever, while the code still reads as "only when it
    changes".

    ⚠️ **The scenario has to force a tick with news that is not news, and it cannot do that by
    sleeping.** Two things happen here that look incidental and are not:

    * the heartbeat is **rewritten**, which is the one field a healthy session changes constantly;
    * another session's entry is published, which wakes the blocked read *immediately* and forces
      the state re-check — no timing, no sleep, no flake.

    Under the whole-state comparison the next frame is a `state`. Under `_watched` it is the fill.
    Without either half, the mutant survives: measured.
    """
    monkeypatch.setattr(ws, "LIVE_BLOCK_MS", 200)
    row = _open(session, rows)

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        row.heartbeat_at = dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)
        session.commit()
        publisher.fill(uuid.uuid4(), client_id="somebody-else")
        publisher.fill(row.id, client_id="after-the-quiet")

        frame = socket.receive_json()

    assert frame["type"] == "fill", "a tick spoke when it had nothing to say"
    assert frame["client_id"] == "after-the-quiet"


def test_the_feed_starts_at_the_tail_and_does_not_replay_history(
    client: TestClient,
    session: Session,
    rows: tuple[Strategy, Instrument],
    publisher: Publisher,
) -> None:
    """⚠️ **A live feed opens at "now", and this stream is never trimmed.**

    `venue.outcomes` has no `MAXLEN` (`specs/backlog.md`), so a socket that started at `0-0` would
    open by replaying every outcome the account ever had — measured at 503 entries on this
    machine before it was cleared. The panel would fill with fills from last week, in order,
    looking exactly like a very busy morning.

    The entry below is published **before anybody connects**, which is the only way to tell the
    two starting points apart: with the tail, the first event is the one that came after.
    """
    row = _open(session, rows)
    publisher.fill(row.id, client_id="before-anybody-was-listening")

    with client.websocket_connect(f"/ws/live-sessions/{row.id}") as socket:
        assert socket.receive_json()["type"] == "state"

        publisher.fill(row.id, client_id="after")
        frame = socket.receive_json()

    assert frame["client_id"] == "after", "the feed replayed history a panel had already seen"
