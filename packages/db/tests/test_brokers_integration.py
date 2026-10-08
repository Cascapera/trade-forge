"""Several brokers at once (ADR-0032): an instrument knows where it comes from, and keeps it."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tradeforge_db.brokers import (
    BrokerChangedError,
    UnknownBrokerError,
    broker_for_server,
    refuse_another_broker,
)
from tradeforge_db.instruments import CatalogueEntry, upsert_instruments
from tradeforge_db.models import Broker, Instrument
from tradeforge_engine.domain import AssetClass, InstrumentSpec

pytestmark = pytest.mark.integration


def spec(symbol: str, *, offset_hours: int = 3) -> InstrumentSpec:
    return InstrumentSpec(
        symbol=symbol,
        name=symbol,
        asset_class=AssetClass.STOCK,
        currency_quote="USD",
        tick_size=Decimal("0.01"),
        tick_value=Decimal("0.01"),
        contract_size=Decimal(1),
        digits=2,
        server_offset=dt.timedelta(hours=offset_hours),
    )


def test_the_two_brokers_in_use_are_registered_with_their_clocks(session: Session) -> None:
    activtrades = broker_for_server(session, "ActivTradesCorp-Server")
    tradeview = broker_for_server(session, "Tradeview-Demo")

    assert (activtrades.slug, activtrades.server_offset) == ("activtrades", dt.timedelta(hours=2))
    assert (tradeview.slug, tradeview.server_offset) == ("tradeview", dt.timedelta(hours=3))


def test_a_server_nobody_registered_is_refused_not_guessed(session: Session) -> None:
    with pytest.raises(UnknownBrokerError, match="register it"):
        broker_for_server(session, "Somebody-Live")
    with pytest.raises(UnknownBrokerError, match="not logged in"):
        broker_for_server(session, None)


def test_cataloguing_with_a_broker_records_it_and_its_ticker(session: Session) -> None:
    tradeview = broker_for_server(session, "Tradeview-Demo")

    upsert_instruments(session, (CatalogueEntry(spec("AAPL"), Decimal(2)),), broker_id=tradeview.id)

    row = session.scalars(select(Instrument).where(Instrument.symbol == "AAPL")).one()
    assert (row.broker_id, row.broker_symbol) == (tradeview.id, "AAPL")


def test_a_symbol_another_broker_owns_is_refused(session: Session) -> None:
    """`GOLD`: the metal at ActivTrades, Barrick Gold at Tradeview — never one series."""
    activtrades = broker_for_server(session, "ActivTradesCorp-Server")
    tradeview = broker_for_server(session, "Tradeview-Demo")
    upsert_instruments(
        session, (CatalogueEntry(spec("GOLD", offset_hours=2), None),), broker_id=activtrades.id
    )

    with pytest.raises(BrokerChangedError, match="GOLD comes from activtrades, not tradeview"):
        upsert_instruments(session, (CatalogueEntry(spec("GOLD"), None),), broker_id=tradeview.id)

    row = session.scalars(select(Instrument).where(Instrument.symbol == "GOLD")).one()
    assert row.broker_id == activtrades.id


def test_the_same_broker_may_catalogue_its_own_symbol_again(session: Session) -> None:
    tradeview = broker_for_server(session, "Tradeview-Demo")
    upsert_instruments(session, (CatalogueEntry(spec("MSFT"), Decimal(1)),), broker_id=tradeview.id)

    refuse_another_broker(session, "MSFT", tradeview.id)  # no error
    written = upsert_instruments(
        session, (CatalogueEntry(spec("MSFT"), Decimal(3)),), broker_id=tradeview.id
    )

    assert written == 1


def test_a_row_without_a_broker_may_be_claimed(session: Session) -> None:
    """The seeds have no broker behind them; the first collector to catalogue one claims it."""
    upsert_instruments(session, (spec("NVDA"),), overwrite=False)
    tradeview = broker_for_server(session, "Tradeview-Demo")

    upsert_instruments(session, (CatalogueEntry(spec("NVDA"), None),), broker_id=tradeview.id)

    row = session.scalars(select(Instrument).where(Instrument.symbol == "NVDA")).one()
    assert (row.broker_id, row.broker_symbol) == (tradeview.id, "NVDA")


def test_without_a_broker_the_row_keeps_the_one_it_had(session: Session) -> None:
    tradeview = broker_for_server(session, "Tradeview-Demo")
    upsert_instruments(session, (CatalogueEntry(spec("AMD"), None),), broker_id=tradeview.id)

    upsert_instruments(session, (CatalogueEntry(spec("AMD"), Decimal(4)),))

    row = session.scalars(select(Instrument).where(Instrument.symbol == "AMD")).one()
    assert row.broker_id == tradeview.id


def test_a_ticker_without_a_broker_is_refused_by_the_database(session: Session) -> None:
    upsert_instruments(session, (spec("TSLA"),), overwrite=False)
    row = session.scalars(select(Instrument).where(Instrument.symbol == "TSLA")).one()
    row.broker_symbol = "TSLA"

    with pytest.raises(IntegrityError, match="broker_symbol_needs_a_broker"):
        session.flush()


def test_one_brokers_ticker_is_one_instrument(session: Session) -> None:
    tradeview = broker_for_server(session, "Tradeview-Demo")
    upsert_instruments(session, (CatalogueEntry(spec("GOOGL"), None),), broker_id=tradeview.id)
    upsert_instruments(session, (spec("ALPHABET"),), overwrite=False)
    other = session.scalars(select(Instrument).where(Instrument.symbol == "ALPHABET")).one()
    other.broker_id, other.broker_symbol = tradeview.id, "GOOGL"

    with pytest.raises(IntegrityError, match="uq_instruments_broker_id_broker_symbol"):
        session.flush()


def test_a_broker_with_instruments_cannot_be_deleted(session: Session) -> None:
    tradeview = broker_for_server(session, "Tradeview-Demo")
    upsert_instruments(session, (CatalogueEntry(spec("NFLX"), None),), broker_id=tradeview.id)
    session.flush()

    session.delete(session.get(Broker, tradeview.id))
    with pytest.raises(IntegrityError):
        session.flush()
