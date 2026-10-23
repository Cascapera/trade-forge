"""`/instruments` over real Postgres: each row says where it is collected from (ADR-0032)."""

import datetime as dt
from collections.abc import Callable, Iterator
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_api.config import Settings
from tradeforge_api.main import create_app
from tradeforge_db.brokers import broker_for_server
from tradeforge_db.instruments import CatalogueEntry, upsert_instruments
from tradeforge_engine.domain import AssetClass, InstrumentSpec

pytestmark = pytest.mark.integration


@pytest.fixture
def client(session_factory: Callable[[], Session]) -> Iterator[TestClient]:
    app: Any = create_app(settings=Settings(), session_factory=session_factory)
    with TestClient(app) as opened:
        yield opened


def _spec(symbol: str) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        name=symbol,
        asset_class=AssetClass.STOCK,
        currency_quote="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        contract_size=Decimal(1),
        digits=2,
        server_offset=dt.timedelta(hours=3),
    )


def test_an_instrument_names_its_broker_and_a_seed_names_none(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    with session_factory() as session:
        tradeview = broker_for_server(session, "Tradeview-Demo")
        upsert_instruments(session, (CatalogueEntry(_spec("AAPL"), None),), broker_id=tradeview.id)
        upsert_instruments(session, (_spec("SEED"),), overwrite=False)
        session.commit()

    body = {one["symbol"]: one for one in client.get("/instruments").json()}

    assert (body["AAPL"]["broker"], body["AAPL"]["broker_symbol"]) == ("tradeview", "AAPL")
    assert (body["SEED"]["broker"], body["SEED"]["broker_symbol"]) == (None, None)
