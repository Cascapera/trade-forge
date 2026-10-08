"""`/watchlist` over real Postgres: what the live signals follow (signals PR 4)."""

import datetime as dt
import uuid
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.brokers import broker_for_server
from tradeforge_db.models import Backtest, BacktestStatus, Instrument, Strategy
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

START = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)


@pytest.fixture
def client(session_factory: Callable[[], Session]) -> Iterator[TestClient]:
    app: Any = create_app(settings=Settings(), session_factory=session_factory)
    with TestClient(app) as opened:
        yield opened


def a_run(
    session_factory: Callable[[], Session],
    *,
    status: BacktestStatus = BacktestStatus.DONE,
    symbol: str = "WIN",
) -> uuid.UUID:
    """A finished H1 run of one strategy on one XP instrument."""
    with session_factory() as session:
        instrument = session.query(Instrument).filter_by(symbol=symbol).one_or_none()
        if instrument is None:
            instrument = Instrument(
                symbol=symbol,
                name=symbol,
                asset_class=AssetClass.FUTURE,
                currency_quote="BRL",
                tick_size=Decimal(1),
                tick_value=Decimal("0.2"),
                contract_size=Decimal(1),
                digits=0,
                broker_id=broker_for_server(session, "XPMT5-DEMO").id,
                broker_symbol=f"{symbol}$",
            )
            session.add(instrument)
            session.flush()
        strategy = Strategy(
            definition={"schema_version": "1.0", "name": "CHOCH", "entry": {}, "exit": {}},
            version=1,
        )
        session.add(strategy)
        session.flush()
        run = Backtest(
            strategy_id=strategy.id,
            instrument_id=instrument.id,
            timeframe="H1",
            date_from=START,
            date_to=START + dt.timedelta(days=30),
            initial_capital=Decimal(10000),
            cost_model={"type": "spread", "spread_points": "5"},
            status=status,
            engine_version="0.6.0",
        )
        session.add(run)
        session.commit()
        return run.id


def test_a_finished_run_becomes_an_item_that_copies_it(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    run = a_run(session_factory)

    response = client.post("/watchlist", json={"backtest_id": str(run), "note": "melhor H1"})

    assert response.status_code == 201, response.text
    item = response.json()
    assert (item["symbol"], item["broker"], item["timeframe"]) == ("WIN", "xp", "H1")
    assert (item["no_target_r"], item["active"], item["note"]) == ("5.00000000", True, "melhor H1")
    assert item["source_backtest_id"] == str(run)
    assert [one["id"] for one in client.get("/watchlist").json()] == [item["id"]]


def test_the_same_setup_on_the_same_market_is_followed_once(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """Two active items would post every signal twice."""
    run = a_run(session_factory)
    first = client.post("/watchlist", json={"backtest_id": str(run)}).json()

    assert client.post("/watchlist", json={"backtest_id": str(run)}).status_code == 409

    # Turned off, it no longer counts — and a new one may follow the same thing...
    assert client.patch(f"/watchlist/{first['id']}", json={"active": False}).status_code == 200
    assert client.post("/watchlist", json={"backtest_id": str(run)}).status_code == 201
    # ...but the old one cannot come back on beside it.
    assert client.patch(f"/watchlist/{first['id']}", json={"active": True}).status_code == 409


def test_only_a_finished_run_can_be_followed(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    running = a_run(session_factory, status=BacktestStatus.RUNNING)

    assert client.post("/watchlist", json={"backtest_id": str(running)}).status_code == 404
    assert client.post("/watchlist", json={"backtest_id": str(uuid.uuid4())}).status_code == 404


def test_an_item_can_be_changed_and_removed(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    item = client.post("/watchlist", json={"backtest_id": str(a_run(session_factory))}).json()

    changed = client.patch(f"/watchlist/{item['id']}", json={"no_target_r": "3", "note": "x"})
    assert (changed.json()["no_target_r"], changed.json()["note"]) == ("3.00000000", "x")
    assert client.patch(f"/watchlist/{item['id']}", json={"no_target_r": "0"}).status_code == 422

    removed = client.delete(f"/watchlist/{item['id']}")
    again = client.delete(f"/watchlist/{item['id']}")
    assert (removed.status_code, again.status_code) == (204, 404)
    assert client.get("/watchlist").json() == []


def test_an_item_outlives_the_run_it_came_from(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """Copied, not referenced: cleaning the run away leaves the item whole."""
    run = a_run(session_factory)
    client.post("/watchlist", json={"backtest_id": str(run)})
    with session_factory() as session:
        session.delete(session.get(Backtest, run))
        session.commit()

    [item] = client.get("/watchlist").json()
    assert (item["symbol"], item["timeframe"], item["source_backtest_id"]) == ("WIN", "H1", None)
