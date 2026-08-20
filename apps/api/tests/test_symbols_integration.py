"""`/symbols` over HTTP, against a real Postgres.

The unit tests next door prove the shape of the answer. What only a real database can prove is
the query underneath it — a prefix match under a real collation, and the fact that the endpoint
serves the snapshot the host agent wrote rather than anything it fetched itself.

⚠️ There is no MetaTrader anywhere in this file, and there cannot be: these tests run on Linux
CI. That is the ADR-02 boundary showing up as a property of the test suite — the API's entire
symbol feature is exercisable without a terminal, because the API's entire symbol feature is a
read of Postgres.
"""

import datetime as dt
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.symbol_history import HistoryProbe, upsert_history

pytestmark = pytest.mark.integration

SYNCED_AT = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


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


def _catalogue(session_factory: Callable[[], Session], *symbols: str) -> None:
    with session_factory() as session:
        replace_snapshot(
            session,
            [BrokerSymbolEntry(symbol=symbol) for symbol in symbols],
            server="MetaQuotes-Demo",
            synced_at=SYNCED_AT,
        )
        session.commit()


def test_typing_a_prefix_returns_the_broker_symbols_that_start_with_it(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    _catalogue(session_factory, "EURUSD", "EURGBP", "GBPUSD")

    body = client.get("/symbols/search", params={"q": "eur"}).json()

    assert [found["symbol"] for found in body["symbols"]] == ["EURGBP", "EURUSD"]
    assert body["snapshot"]["server"] == "MetaQuotes-Demo"


def test_one_letter_is_enough(client: TestClient, session_factory: Callable[[], Session]) -> None:
    """The combobox queries from the first keystroke, so the endpoint has to answer one."""
    _catalogue(session_factory, "EURUSD", "GBPUSD")

    body = client.get("/symbols/search", params={"q": "g"}).json()

    assert [found["symbol"] for found in body["symbols"]] == ["GBPUSD"]


def test_a_search_with_no_catalogue_says_so_instead_of_saying_no_results(
    client: TestClient,
) -> None:
    """⚠️ The distinction the screen needs, end to end.

    Nothing has ever been synced here. The list is empty — and so is the list for a prefix that
    simply matches nothing — but `snapshot` is null only in this case, which is what lets the
    screen say "sync your broker" rather than "no symbol called that".
    """
    body = client.get("/symbols/search", params={"q": "eur"}).json()

    assert body["symbols"] == []
    assert body["snapshot"] is None


def test_a_prefix_matching_nothing_still_reports_the_catalogue_it_searched(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    _catalogue(session_factory, "EURUSD")

    body = client.get("/symbols/search", params={"q": "zzz"}).json()

    assert body["symbols"] == []
    assert body["snapshot"] is not None


def test_the_page_size_is_bounded(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    _catalogue(session_factory, "EURAUD", "EURCAD", "EURCHF", "EURGBP")

    body = client.get("/symbols/search", params={"q": "eur", "limit": 2}).json()

    assert len(body["symbols"]) == 2


def test_a_limit_beyond_the_ceiling_is_refused_rather_than_clamped(client: TestClient) -> None:
    """⚠️ Every numeric query parameter needs a **maximum**, not just a minimum.

    This project has already been shown that `offset` without a ceiling accepts the 500 of a
    bigint. Refusing is better than silently clamping: a client asking for 10 000 rows has a
    bug, and a response of 100 that looks like success hides it.
    """
    assert client.get("/symbols/search", params={"limit": 10_000}).status_code == 422


def test_a_nul_byte_is_refused_rather_than_reaching_postgres(client: TestClient) -> None:
    """⚠️ **Regression. This endpoint was green on the previous commit by luck.**

    `q` goes straight into an `ILIKE` against a text column, and Postgres cannot hold a NUL
    byte: `?q=%00` raises `DataError` from inside the driver, so a value the client fully
    controls becomes a 500. Schemathesis found it on the run after the one that passed, because
    a fuzzer has to *draw* the value — a green fuzz run is a lottery ticket, not a proof.

    The project already had `StorableText` for exactly this, created the last time it happened,
    and its docstring predicted this: a rule attached to one field is a rule the next field does
    not inherit. `?q=` was the next field.
    """
    # `chr(0)` and not the escape, for the reason `_storable` gives about itself: writing
    # this file put a real NUL in the source, and Python then refuses to parse it.
    response = client.get("/symbols/search", params={"q": chr(0)})

    assert response.status_code == 422


def test_syncing_hands_the_job_to_the_queue_only_the_host_agent_drains(
    client: TestClient, queue: _CapturingQueue
) -> None:
    """⚠️ The routing that keeps the job off a worker that could never run it.

    Enqueued on the default queue, `sync_symbols` would be claimed by a Linux container that
    cannot import MetaTrader — or would sit `queued` for ever with nothing raised. The queue
    name is what makes "who is able to execute this" a fact instead of a hope.
    """
    response = client.post("/symbols/sync")

    assert response.status_code == 202
    assert response.json() == {"job": "sync_symbols"}
    name, options = queue.jobs[0]
    assert name == "sync_symbols"
    assert options["_queue_name"] == "collect"


def test_pressing_sync_again_actually_enqueues_again(
    client: TestClient, queue: _CapturingQueue
) -> None:
    """⚠️ **The bug this replaced, found by pressing the button twice for real.**

    The first version pinned `_job_id` to a constant, which reads as free idempotence and is a
    one-hour mute button: arq refuses an id that still has a result in Redis, and results live
    for `keep_result` seconds — 3600 by default. Measured: after one sync the result key had
    2187 s left, and every further press returned `202` and did nothing. The person most likely
    to press it is the one who just switched brokers, and they would have been told it worked.

    The job replaces the whole snapshot, so running it twice is a no-op by construction. Not
    letting somebody hammer the button belongs to the screen, which disables it while the
    request is in flight.
    """
    client.post("/symbols/sync")
    client.post("/symbols/sync")

    assert len(queue.jobs) == 2
    assert not any("_job_id" in options for _, options in queue.jobs)


def _probe(session_factory: Callable[[], Session], **patch: object) -> None:
    fields: dict[str, object] = {
        "symbol": "EURUSD",
        "timeframe": "D1",
        "oldest": dt.datetime(1971, 1, 3, tzinfo=dt.UTC),
        "bar_count": 14_343,
        "terminal_maxbars": 100_000_000,
        "bar_count_is_a_ceiling": False,
        "last_fabricated": 1972,
        "first_measured_cost": 2009,
    }
    fields.update(patch)
    with session_factory() as session:
        upsert_history(session, HistoryProbe(**fields), probed_at=SYNCED_AT)  # type: ignore[arg-type]
        session.commit()


def test_a_probed_series_reports_its_bounds_and_the_window_they_imply(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    _probe(session_factory)

    body = client.get("/symbols/EURUSD/history", params={"timeframe": "D1"}).json()

    assert body["bar_count"] == 14_343
    assert body["last_fabricated"] == 1972
    assert body["first_measured_cost"] == 2009
    assert body["usable_from"].startswith("2009-01-01")


def test_a_series_nobody_has_probed_is_a_404_and_not_an_empty_row(client: TestClient) -> None:
    """⚠️ "Nobody asked yet" and "this symbol has no bars" are opposite invitations.

    One is fixed by pressing a button; the other is not. A screen shown an empty row for both
    would tell somebody their broker has no history for EURUSD because nobody had measured it.
    """
    response = client.get("/symbols/EURUSD/history", params={"timeframe": "H4"})

    assert response.status_code == 404
    assert "not been probed" in response.json()["detail"]


def test_a_timeframe_the_dsl_does_not_define_is_refused_before_the_lookup(
    client: TestClient,
) -> None:
    # 422, not the 404 above: the two say different things and only one means "press probe".
    assert client.get("/symbols/EURUSD/history", params={"timeframe": "M2"}).status_code == 422


def test_re_probing_replaces_the_answer_rather_than_adding_one(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """⚠️ The answer changes over time — history downloads, `maxbars` gets raised — and two
    accounts of the same series would leave the screen picking between them by date.
    """
    _probe(session_factory, bar_count=100_000, terminal_maxbars=100_000)
    _probe(session_factory, bar_count=14_343, terminal_maxbars=100_000_000)

    body = client.get("/symbols/EURUSD/history", params={"timeframe": "D1"}).json()

    assert body["bar_count"] == 14_343
    assert body["capped_by_terminal"] is False


def test_asking_for_a_probe_hands_the_job_to_the_host_queue(
    client: TestClient, queue: _CapturingQueue
) -> None:
    """⚠️ 202 is the measurement talking: a cold H4 took 207 seconds on this broker."""
    response = client.post("/symbols/XAUUSD/probe", params={"timeframe": "H4"})

    assert response.status_code == 202
    name, options = queue.jobs[0]
    assert name == "probe_history"
    assert options["_queue_name"] == "collect"


def test_a_probe_carries_the_symbol_and_timeframe_it_was_asked_about(
    client: TestClient, queue: _CapturingQueue
) -> None:
    # Without the arguments the agent would probe something, and the screen would wait for an
    # answer about a series nobody asked about.
    client.post("/symbols/XAUUSD/probe", params={"timeframe": "H4"})

    assert queue.args[0] == ("XAUUSD", "H4")
