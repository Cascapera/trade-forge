"""`POST /collections/plan` over HTTP, against a real Postgres.

The arithmetic is proved in `test_collection_plan.py`. What only a database can prove is that the
right facts reach it: each pair's own extent, each pair's own probe, and nothing written. Every
window here lies well before today, so the real clock the route reads cannot move an answer.
"""

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.models import Collection, Dataset, Instrument
from tradeforge_db.symbol_history import HistoryProbe, upsert_history
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration


def at(year: int, month: int = 1, day: int = 1, hour: int = 0) -> dt.datetime:
    return dt.datetime(year, month, day, hour, tzinfo=dt.UTC)


def year_start(year: int) -> str:
    return f"{year}-01-01T00:00:00Z"


def year_end(year: int) -> str:
    return f"{year}-12-31T23:59:59.999999Z"


class _CapturingQueue:
    def __init__(self) -> None:
        self.jobs: list[str] = []

    async def enqueue_job(self, function: str, *args: object, **options: object) -> object:
        self.jobs.append(function)
        return None


@pytest.fixture
def queue() -> _CapturingQueue:
    return _CapturingQueue()


@pytest.fixture
def client(session_factory: Callable[[], Session], queue: _CapturingQueue) -> Iterator[TestClient]:
    app: Any = create_app(settings=Settings(), session_factory=session_factory, arq_pool=queue)
    with TestClient(app) as opened:
        yield opened


def on_disk(
    session_factory: Callable[[], Session],
    symbol: str,
    timeframe: str,
    date_from: dt.datetime,
    date_to: dt.datetime,
) -> None:
    """Index one pair as collected. The Parquet path points at nothing: only the index is read."""
    with session_factory() as session:
        instrument = session.scalars(select(Instrument).where(Instrument.symbol == symbol)).first()
        if instrument is None:
            instrument = Instrument(
                symbol=symbol,
                name=symbol,
                asset_class=AssetClass.FOREX,
                currency_base=symbol[:3],
                currency_quote=symbol[3:],
                tick_size=Decimal("0.00001"),
                tick_value=Decimal(1),
                contract_size=Decimal(100000),
                digits=5,
            )
            session.add(instrument)
            session.flush()
        session.add(
            Dataset(
                instrument_id=instrument.id,
                timeframe=timeframe,
                date_from=date_from,
                date_to=date_to,
                candle_count=1000,
                parquet_path=f"{symbol}/{timeframe}",
            )
        )
        session.commit()


def probed(
    session_factory: Callable[[], Session], symbol: str, timeframe: str, oldest: dt.datetime
) -> None:
    with session_factory() as session:
        upsert_history(
            session,
            HistoryProbe(
                symbol=symbol,
                timeframe=timeframe,
                oldest=oldest,
                bar_count=1000,
                terminal_maxbars=100000,
                bar_count_is_a_ceiling=False,
                last_fabricated=None,
                first_measured_cost=None,
            ),
            probed_at=at(2026, 8, 20),
        )
        session.commit()


def a_plan(client: TestClient, **fields: object) -> Any:
    body: dict[str, object] = {
        "symbols": ["EURUSD"],
        "timeframes": ["H1"],
        "date_from": "2019-06-01T00:00:00Z",
        "date_to": "2021-06-30T00:00:00Z",
    }
    body.update(fields)
    return client.post("/collections/plan", json=body)


def test_a_symbol_never_collected_is_planned_whole(client: TestClient) -> None:
    """Not in `instruments` at all — the case the plan exists for, not a 422."""
    response = a_plan(client)
    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "symbol": "EURUSD",
            "timeframe": "H1",
            "covers": None,
            "windows": [{"date_from": year_start(2019), "date_to": year_end(2021)}],
        }
    ]


def test_a_covered_pair_is_left_out(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    on_disk(session_factory, "EURUSD", "H1", at(2018, 1, 2), at(2022, 12, 30))
    response = a_plan(client)
    assert response.status_code == 200, response.text
    assert response.json() == []


def test_the_extent_is_read_per_timeframe(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """EURUSD H1 is on disk and EURUSD H4 is not. A plan that looked the extent up by symbol
    alone would call H4 covered."""
    on_disk(session_factory, "EURUSD", "H1", at(2018, 1, 2), at(2022, 12, 30))
    response = a_plan(client, timeframes=["H1", "H4"])
    assert [(one["symbol"], one["timeframe"]) for one in response.json()] == [("EURUSD", "H4")]


def test_the_extent_is_read_per_symbol(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    on_disk(session_factory, "EURUSD", "H1", at(2018, 1, 2), at(2022, 12, 30))
    response = a_plan(client, symbols=["EURUSD", "GBPUSD"])
    assert [(one["symbol"], one["timeframe"]) for one in response.json()] == [("GBPUSD", "H1")]


def test_a_partly_covered_pair_says_what_it_holds_and_reaches_it(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    # On disk 2021-03-01 .. 2021-09-30; asked 2019-06 .. 2021-06 → 2019 .. 2021, one window.
    on_disk(session_factory, "EURUSD", "H1", at(2021, 3), at(2021, 9, 30))
    (planned,) = a_plan(client).json()
    assert planned["covers"] == "2021-03-01 to 2021-09-30"
    assert planned["windows"] == [{"date_from": year_start(2019), "date_to": year_end(2021)}]


def test_the_probe_is_read_per_timeframe(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """H1 and H4 both start on disk at the broker's H1 oldest bar, 2020-05-10; only H1 was
    probed. H1 is complete — nothing older exists. H4 is planned from 2019, because nobody
    asked the broker how far back H4 goes. A plan that read the probe by symbol alone would
    leave H4 out."""
    oldest = at(2020, 5, 10, 13)
    for timeframe in ("H1", "H4"):
        on_disk(session_factory, "BTCUSD", timeframe, oldest, at(2022, 12, 30))
    probed(session_factory, "BTCUSD", "H1", oldest)

    response = a_plan(client, symbols=["BTCUSD"], timeframes=["H1", "H4"])
    assert response.json() == [
        {
            "symbol": "BTCUSD",
            "timeframe": "H4",
            "covers": "2020-05-10 to 2022-12-30",
            "windows": [{"date_from": year_start(2019), "date_to": year_end(2020)}],
        }
    ]


def test_the_probe_is_read_per_symbol(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    oldest = at(2020, 5, 10, 13)
    for symbol in ("BTCUSD", "ETHUSD"):
        on_disk(session_factory, symbol, "H1", oldest, at(2022, 12, 30))
    probed(session_factory, "BTCUSD", "H1", oldest)

    response = a_plan(client, symbols=["BTCUSD", "ETHUSD"])
    assert [one["symbol"] for one in response.json()] == ["ETHUSD"]


def test_a_plan_writes_and_queues_nothing(
    client: TestClient, session_factory: Callable[[], Session], queue: _CapturingQueue
) -> None:
    assert a_plan(client).status_code == 200
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(Collection)) == 0
    assert queue.jobs == []


def test_the_windows_are_accepted_by_the_collection_endpoint_as_they_are(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """The plan is only useful if its answer can be sent on unchanged."""
    with session_factory() as session:
        replace_snapshot(
            session,
            [BrokerSymbolEntry(symbol="EURUSD", path="Forex\\Majors\\EURUSD")],
            server="Tradeview-Demo",
            synced_at=at(2026, 8, 20),
        )
        session.commit()
    (planned,) = a_plan(client).json()
    rows = [{"timeframe": planned["timeframe"], **window} for window in planned["windows"]]

    response = client.post(
        "/collections", json={"items": [{"symbol": planned["symbol"]}], "rows": rows}
    )
    assert response.status_code == 202, response.text
    (row,) = response.json()
    assert (row["date_from"], row["date_to"], row["years_total"]) == (
        "2019-01-01T00:00:00Z",
        "2021-12-31T23:59:59.999999Z",
        3,
    )


@pytest.mark.parametrize(
    ("fields", "complaint"),
    [
        ({"date_from": "2019-06-01T00:00:00"}, "timezone"),
        ({"date_from": "2022-01-01T00:00:00Z"}, "before"),
        ({"symbols": ["EURUSD", "EURUSD"]}, "repeated"),
        ({"timeframes": ["H2"]}, "H2"),
        ({"symbols": []}, "at least 1"),
    ],
)
def test_a_malformed_request_is_refused(
    client: TestClient, fields: dict[str, object], complaint: str
) -> None:
    response = a_plan(client, **fields)
    assert response.status_code == 422
    assert complaint in response.text
