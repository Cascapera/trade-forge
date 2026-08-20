"""`/collections` over HTTP, against a real Postgres.

The unit tests next door prove what a request accepts. What only a real database can prove is
the part that makes the feature work at all: that the row exists before the 202 comes back, that
the id in the body is the id on the queue, and that a symbol nobody can classify is turned away
*before* a job is enqueued rather than after one has failed.

⚠️ No MetaTrader anywhere, and there cannot be — this runs on Linux CI. The API's whole part in
collecting is a write to Postgres and a push to Redis; the terminal belongs to the host agent.
"""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_api.queue import COLLECT_QUEUE, COLLECT_RANGE
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.collections import read_collection

pytestmark = pytest.mark.integration

SYNCED_AT = dt.datetime(2026, 8, 20, 12, 0, tzinfo=dt.UTC)


class _CapturingQueue:
    """Records enqueues instead of reaching Redis. The host agent is not running in CI."""

    def __init__(self) -> None:
        self.jobs: list[tuple[str, dict[str, object]]] = []
        self.args: list[tuple[object, ...]] = []

    async def enqueue_job(self, function: str, *args: object, **options: object) -> object:
        self.jobs.append((function, dict(options)))
        self.args.append(args)
        return None


@pytest.fixture
def queue() -> _CapturingQueue:
    return _CapturingQueue()


@pytest.fixture
def client(session_factory: Callable[[], Session], queue: _CapturingQueue) -> Iterator[TestClient]:
    app: Any = create_app(settings=Settings(), session_factory=session_factory, arq_pool=queue)
    with TestClient(app) as opened:
        yield opened


def snapshot(session_factory: Callable[[], Session], **symbols: str) -> None:
    """Photograph a broker catalogue, symbol name to tree path."""
    with session_factory() as session:
        replace_snapshot(
            session,
            [BrokerSymbolEntry(symbol=symbol, path=path) for symbol, path in symbols.items()],
            server="Tradeview-Demo",
            synced_at=SYNCED_AT,
        )
        session.commit()


def a_request(**fields: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "symbol": "EURUSD",
        "timeframe": "H1",
        "date_from": "2020-06-01T00:00:00Z",
        "date_to": "2022-03-01T00:00:00Z",
    }
    payload.update(fields)
    return payload


def test_a_collection_is_accepted_recorded_and_queued(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    response = client.post("/collections", json=a_request())

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    # ⚠️ Three years, counted at request time. The screen renders "0 of 3 years" the moment the
    # button is pressed; leaving it to the agent would show "0 of 0", which reads as finished.
    assert body["years_total"] == 3
    assert body["years_done"] == 0
    assert body["candles"] is None

    assert queue.jobs == [(COLLECT_RANGE, {"_queue_name": COLLECT_QUEUE})]
    assert queue.args == [(body["id"],)], "the queue carries the row's id and nothing else"


def test_the_row_exists_before_the_response_does(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ The ordering the agent depends on.

    The job is queued after the row is committed, so an agent that picks it up instantly finds
    something there. Queue first and the job can race ahead of its own row, log "collection no
    longer exists", and vanish with nothing on screen ever explaining why.
    """
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    body = client.post("/collections", json=a_request()).json()

    with session_factory() as session:
        stored = read_collection(session, uuid.UUID(body["id"]))
    assert stored is not None
    assert stored.symbol == "EURUSD"
    assert stored.date_to.year == 2022


def test_a_symbol_the_path_cannot_classify_is_refused_before_anything_is_queued(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """⚠️ 24 of this broker's 84 symbols, and the refusal is what makes them collectable.

    `instruments.asset_class` is NOT NULL with five legal values and `CFDs` names none of them.
    Accepting the request would queue a job that fails on the host three minutes later, by which
    time the person who could answer the question has gone. 409 while they are looking at the
    form is a field to fill in.
    """
    snapshot(session_factory, XAUUSD="CFDs\\Metals\\XAUUSD")

    response = client.post("/collections", json=a_request(symbol="XAUUSD"))

    assert response.status_code == 409
    assert "asset class" in response.json()["detail"]
    assert queue.jobs == [], "nothing may be queued for a request that was refused"


def test_the_same_symbol_is_accepted_once_the_class_is_supplied(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """The other half: the refusal above must be answerable, or it is just a wall."""
    snapshot(session_factory, XAUUSD="CFDs\\Metals\\XAUUSD")

    response = client.post("/collections", json=a_request(symbol="XAUUSD", asset_class="future"))

    assert response.status_code == 202
    assert response.json()["asset_class"] == "future", "who decided it is worth recording"
    assert len(queue.jobs) == 1


def test_a_path_that_names_the_class_needs_no_answer_and_records_none(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ NULL means *the path decided*, not *nobody knows*. Storing the derived value here
    would make the row unable to say who chose it — and a re-derivation later, after the broker
    refiles a symbol, would have no way to tell an inherited guess from a human answer."""
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    body = client.post("/collections", json=a_request()).json()

    assert body["asset_class"] is None


def test_crypto_currency_is_a_path_that_names_its_class(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """Measured on this broker: its seven crypto symbols file under `Crypto Currency`, and
    before this PR that root was unclassifiable — so the collect button would have refused
    BTCUSD while the word "crypto" was sitting in the path."""
    snapshot(session_factory, BTCUSD="Crypto Currency\\BTCUSD")

    response = client.post("/collections", json=a_request(symbol="BTCUSD"))

    assert response.status_code == 202


def test_a_symbol_that_was_never_synced_is_refused_rather_than_guessed(
    client: TestClient, queue: _CapturingQueue
) -> None:
    """No snapshot at all, so no path, so no class. The same 409 — and correctly so: the
    endpoint has no evidence, and inventing `forex` would file anything as a currency pair."""
    response = client.post("/collections", json=a_request())

    assert response.status_code == 409
    assert queue.jobs == []


def test_a_backwards_window_is_refused_by_the_schema(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    response = client.post(
        "/collections",
        json=a_request(date_from="2022-01-01T00:00:00Z", date_to="2020-01-01T00:00:00Z"),
    )

    assert response.status_code == 422
    assert queue.jobs == []


def test_a_collection_can_be_read_back_by_id(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")
    created = client.post("/collections", json=a_request()).json()

    fetched = client.get(f"/collections/{created['id']}")

    assert fetched.status_code == 200
    assert fetched.json()["id"] == created["id"]


def test_an_unknown_id_is_a_404_and_not_an_empty_row(client: TestClient) -> None:
    response = client.get(f"/collections/{uuid.uuid4()}")

    assert response.status_code == 404


def test_the_listing_is_newest_first(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ Ordered explicitly. The primary key is a UUID and carries no order of its own, so
    without an ORDER BY the list would reshuffle between polls while nothing changed."""
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD", GBPUSD="Forex\\Majors\\GBPUSD")
    client.post("/collections", json=a_request(symbol="EURUSD"))
    client.post("/collections", json=a_request(symbol="GBPUSD"))

    listed = client.get("/collections").json()

    assert [row["symbol"] for row in listed] == ["GBPUSD", "EURUSD"]
