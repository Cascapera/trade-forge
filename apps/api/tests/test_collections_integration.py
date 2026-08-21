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
from tradeforge_api.schemas import MAX_COLLECTION_SYMBOLS
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.collections import read_collection, recent_collections

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
    """A batch of one, unless `items` overrides it.

    `symbol` and `asset_class` stay as conveniences because most of these tests are about a
    *decision* the endpoint makes — can this symbol be classified, is this window legal — and
    those read better on one symbol. The batching itself is proved by the tests that pass
    `items` directly.
    """
    symbol = fields.pop("symbol", "EURUSD")
    asset_class = fields.pop("asset_class", None)
    item: dict[str, object] = {"symbol": symbol}
    if asset_class is not None:
        item["asset_class"] = asset_class

    payload: dict[str, object] = {
        "items": [item],
        "rows": [
            {
                "timeframe": "H1",
                "date_from": "2020-06-01T00:00:00Z",
                "date_to": "2022-03-01T00:00:00Z",
            }
        ],
    }
    payload.update(fields)
    return payload


def only(response: object) -> dict[str, Any]:
    """The single row of a batch of one. Named rather than spelled `[0]` everywhere so the
    tests that care about batching stand out from the ones that do not.

    The two asserts are the point: a body that came back as an object, or as two rows, fails
    here with that sentence rather than as a `KeyError` several assertions later.
    """
    assert isinstance(response, list), f"the endpoint answers with a list, got {type(response)}"
    assert len(response) == 1, f"expected a batch of one, got {len(response)}"
    row = response[0]
    assert isinstance(row, dict)
    return row


def test_a_collection_is_accepted_recorded_and_queued(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    response = client.post("/collections", json=a_request())

    assert response.status_code == 202
    body = only(response.json())
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

    body = only(client.post("/collections", json=a_request()).json())

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
    assert only(response.json())["asset_class"] == "future", "who decided it is worth recording"
    assert len(queue.jobs) == 1


def test_a_path_that_names_the_class_needs_no_answer_and_records_none(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ NULL means *the path decided*, not *nobody knows*. Storing the derived value here
    would make the row unable to say who chose it — and a re-derivation later, after the broker
    refiles a symbol, would have no way to tell an inherited guess from a human answer."""
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    body = only(client.post("/collections", json=a_request()).json())

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
        json=a_request(
            rows=[
                {
                    "timeframe": "H1",
                    "date_from": "2022-01-01T00:00:00Z",
                    "date_to": "2020-01-01T00:00:00Z",
                }
            ]
        ),
    )

    assert response.status_code == 422
    assert queue.jobs == []


def test_a_collection_can_be_read_back_by_id(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")
    created = only(client.post("/collections", json=a_request()).json())

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


# --------------------------------------------------------------------------- #
# A batch: N symbols, one window                                                #
# --------------------------------------------------------------------------- #


def count_collections(session_factory: Callable[[], Session]) -> int:
    with session_factory() as session:
        return len(recent_collections(session))


def test_a_batch_becomes_one_row_and_one_job_per_symbol(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """The whole point: three symbols, one window, three independent requests.

    No parent row and no aggregate state — a batch is exactly what three separate calls would
    have produced, which is why `baskets` needed a table and this does not.
    """
    snapshot(
        session_factory,
        EURUSD="Forex\\Majors\\EURUSD",
        GBPUSD="Forex\\Majors\\GBPUSD",
        USDJPY="Forex\\Majors\\USDJPY",
    )

    response = client.post(
        "/collections",
        json=a_request(items=[{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}, {"symbol": "USDJPY"}]),
    )

    assert response.status_code == 202
    body = response.json()
    # ⚠️ The order of the response is the order of the request, so a caller can zip its own
    # list against it. Sorting here would look tidier and break that.
    assert [row["symbol"] for row in body] == ["EURUSD", "GBPUSD", "USDJPY"]
    assert {row["status"] for row in body} == {"queued"}
    # Every item shares the window, so every row counts the same three years.
    assert [row["years_total"] for row in body] == [3, 3, 3]

    assert len(queue.jobs) == 3
    assert queue.args == [(row["id"],) for row in body], "one job per row, carrying its id"


def test_the_rows_of_a_batch_are_unrelated_to_each_other(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """Three distinct ids and three distinct rows. Nothing links them, on purpose — DD-01."""
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD", GBPUSD="Forex\\Majors\\GBPUSD")

    body = client.post(
        "/collections", json=a_request(items=[{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}])
    ).json()

    assert len({row["id"] for row in body}) == 2
    with session_factory() as session:
        stored = [read_collection(session, uuid.UUID(row["id"])) for row in body]
    assert [row.symbol for row in stored if row is not None] == ["EURUSD", "GBPUSD"]


def test_each_symbol_of_a_batch_keeps_its_own_asset_class(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ The failure a batch-wide `asset_class` field would have caused.

    XAUUSD is filed under `CFDs\\Metals` and BTCUSD under `Crypto Currency`; the first needs an
    answer and the second does not. One field for the batch would have to file one of them as
    the other, and the row would look perfectly well-formed while being wrong.
    """
    snapshot(session_factory, XAUUSD="CFDs\\Metals\\XAUUSD", BTCUSD="Crypto Currency\\BTCUSD")

    body = client.post(
        "/collections",
        json=a_request(items=[{"symbol": "XAUUSD", "asset_class": "future"}, {"symbol": "BTCUSD"}]),
    ).json()

    assert [(row["symbol"], row["asset_class"]) for row in body] == [
        ("XAUUSD", "future"),
        # NULL because the path decided, not because nobody knows — the same distinction the
        # single-symbol case makes.
        ("BTCUSD", None),
    ]


def test_one_unclassifiable_symbol_refuses_the_whole_batch_and_writes_nothing(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """⚠️ All or nothing on acceptance, proved by **counting rows before and after**.

    Asserting only that the response is 409 would pass against an endpoint that had already
    written the two good rows before reaching the third — the failure mode this refuses is
    invisible from the response alone. A partially accepted batch leaves the operator
    reconciling which of the twenty went through against a list the form no longer shows.
    """
    snapshot(
        session_factory,
        EURUSD="Forex\\Majors\\EURUSD",
        GBPUSD="Forex\\Majors\\GBPUSD",
        XAUUSD="CFDs\\Metals\\XAUUSD",
    )
    before = count_collections(session_factory)

    response = client.post(
        "/collections",
        json=a_request(items=[{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}, {"symbol": "XAUUSD"}]),
    )

    assert response.status_code == 409
    assert count_collections(session_factory) == before, "a refused batch writes no rows at all"
    assert queue.jobs == [], "and queues no jobs at all"


def test_the_refusal_names_every_unclassifiable_symbol_not_just_the_first(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """A caller fixing a twenty-symbol list one rejection at a time is a caller being fought."""
    snapshot(session_factory, XAUUSD="CFDs\\Metals\\XAUUSD", XAGUSD="CFDs\\Metals\\XAGUSD")

    response = client.post(
        "/collections", json=a_request(items=[{"symbol": "XAUUSD"}, {"symbol": "XAGUSD"}])
    )

    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "XAUUSD" in detail
    assert "XAGUSD" in detail


def test_a_repeated_symbol_refuses_the_batch_and_writes_nothing(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")
    before = count_collections(session_factory)

    response = client.post(
        "/collections", json=a_request(items=[{"symbol": "EURUSD"}, {"symbol": "EURUSD"}])
    )

    assert response.status_code == 422
    assert count_collections(session_factory) == before
    assert queue.jobs == []


def test_a_batch_past_the_ceiling_refuses_and_writes_nothing(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """Without a ceiling a single POST enqueues as many jobs as the caller likes — and each one
    of these is minutes of a terminal downloading, not milliseconds of CPU."""
    before = count_collections(session_factory)

    response = client.post(
        "/collections",
        json=a_request(
            items=[{"symbol": f"SYM{n:02d}"} for n in range(MAX_COLLECTION_SYMBOLS + 1)]
        ),
    )

    assert response.status_code == 422
    assert count_collections(session_factory) == before
    assert queue.jobs == []


# --------------------------------------------------------------------------- #
# The product: symbols multiplied by timeframe rows                             #
# --------------------------------------------------------------------------- #


def a_row(timeframe: str, date_from: str, date_to: str) -> dict[str, object]:
    return {"timeframe": timeframe, "date_from": date_from, "date_to": date_to}


def test_two_symbols_across_two_timeframes_are_four_collections(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """The whole point of a row: one form, one submit, four independent collections.

    ⚠️ **Each row carries its own window, and the rows here differ on purpose.** A year of M1
    and seventeen of H1 are the same budget of bars over very different spans — a single window
    across both would be wrong for one of them by construction, which is why the window sits on
    the row rather than on the request.
    """
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD", GBPUSD="Forex\\Majors\\GBPUSD")

    response = client.post(
        "/collections",
        json=a_request(
            items=[{"symbol": "EURUSD"}, {"symbol": "GBPUSD"}],
            rows=[
                a_row("M1", "2025-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
                a_row("H1", "2020-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
            ],
        ),
    )

    assert response.status_code == 202
    body = response.json()
    # ⚠️ Symbol-major, so the answer reads the way the form does: everything asked for EURUSD,
    # then everything asked for GBPUSD. Row-major would interleave them and quietly break a
    # caller zipping its own list against the response.
    assert [(r["symbol"], r["timeframe"]) for r in body] == [
        ("EURUSD", "M1"),
        ("EURUSD", "H1"),
        ("GBPUSD", "M1"),
        ("GBPUSD", "H1"),
    ]
    assert len(queue.jobs) == 4


def test_each_row_records_its_own_years_not_the_batch_s(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ `years_total` is the progress denominator the screen renders, and it is per row.

    One year of M1 is one slice; six years of H1 are six. A batch-wide count would make the M1
    row show "0 of 6 years" and sit at 1/6 forever after finishing — progress that reads as a
    stall on a run that is already done.
    """
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD")

    body = client.post(
        "/collections",
        json=a_request(
            rows=[
                a_row("M1", "2025-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
                a_row("H1", "2020-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
            ]
        ),
    ).json()

    assert [(r["timeframe"], r["years_total"]) for r in body] == [("M1", 1), ("H1", 6)]


def test_one_unclassifiable_symbol_refuses_every_row_of_the_batch(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """All-or-nothing now spans the product, not just the symbol list — counted, as ever, on
    the rows in the table rather than on the status code alone."""
    snapshot(session_factory, EURUSD="Forex\\Majors\\EURUSD", XAUUSD="CFDs\\Metals\\XAUUSD")
    before = count_collections(session_factory)

    response = client.post(
        "/collections",
        json=a_request(
            items=[{"symbol": "EURUSD"}, {"symbol": "XAUUSD"}],
            rows=[
                a_row("M1", "2025-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
                a_row("H1", "2020-01-01T00:00:00Z", "2025-12-31T00:00:00Z"),
            ],
        ),
    )

    assert response.status_code == 409
    assert count_collections(session_factory) == before, "not one of the four rows may be written"
    assert queue.jobs == []


def test_a_batch_past_the_work_ceiling_writes_nothing(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    """⚠️ The ceiling the symbol limit cannot express: eleven symbols is legal, four timeframes
    is legal, and forty-four collections is not."""
    before = count_collections(session_factory)

    response = client.post(
        "/collections",
        json=a_request(
            items=[{"symbol": f"SYM{n:02d}"} for n in range(11)],
            rows=[
                a_row(tf, "2024-01-01T00:00:00Z", "2024-12-31T00:00:00Z")
                for tf in ("M1", "M5", "M15", "H1")
            ],
        ),
    )

    assert response.status_code == 422
    assert count_collections(session_factory) == before
    assert queue.jobs == []
