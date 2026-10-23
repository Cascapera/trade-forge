"""Several brokers at once (ADR-0032): an instrument knows where it comes from, and keeps it."""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.brokers import (
    BrokerChangedError,
    UnknownBrokerError,
    broker_by_slug,
    broker_for_server,
    broker_slug_for_symbol,
    broker_slugs,
    broker_ticker,
    collect_queue,
    keeps_path,
    refuse_another_broker,
)
from tradeforge_db.instruments import CatalogueEntry, upsert_instruments
from tradeforge_db.models import Broker, Instrument
from tradeforge_engine.domain import AssetClass, InstrumentSpec

pytestmark = pytest.mark.integration

NOW = dt.datetime(2026, 10, 8, 12, 0, tzinfo=dt.UTC)


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


def test_each_broker_has_its_own_queue_and_the_legacy_one_stays() -> None:
    assert collect_queue("tradeview") == "collect.tradeview"
    assert collect_queue(None) == "collect"


def test_a_symbol_is_sent_to_its_instruments_broker_first(session: Session) -> None:
    activtrades = broker_for_server(session, "ActivTradesCorp-Server")
    upsert_instruments(
        session, (CatalogueEntry(spec("EURUSD", offset_hours=2), None),), broker_id=activtrades.id
    )
    # Even though a Tradeview catalogue lists a symbol by that name too.
    replace_snapshot(
        session, [BrokerSymbolEntry(symbol="EURUSD")], server="Tradeview-Demo", synced_at=NOW
    )

    assert broker_slug_for_symbol(session, "EURUSD") == "activtrades"


def test_a_symbol_not_catalogued_yet_goes_to_the_broker_whose_terminal_lists_it(
    session: Session,
) -> None:
    replace_snapshot(
        session, [BrokerSymbolEntry(symbol="MU")], server="Tradeview-Demo", synced_at=NOW
    )

    assert broker_slug_for_symbol(session, "MU") == "tradeview"
    assert broker_slug_for_symbol(session, "NOWHERE") is None


def test_a_slug_nobody_registered_names_the_ones_there_are(session: Session) -> None:
    assert broker_by_slug(session, "tradeview").server == "Tradeview-Demo"
    assert broker_slugs(session) == ["activtrades", "tradeview", "xp"]
    with pytest.raises(LookupError, match="registered: activtrades, tradeview, xp"):
        broker_by_slug(session, "clear")


def test_xp_is_registered_on_brasilia_time_keeping_two_folders(session: Session) -> None:
    xp = broker_for_server(session, "XPMT5-DEMO")

    assert (xp.slug, xp.server_offset) == ("xp", dt.timedelta(hours=-3))
    assert xp.catalogue_paths == [r"BOVESPA\A VISTA", r"BMF\SERIES CONTINUAS"]
    assert broker_by_slug(session, "activtrades").terminal_path == (
        r"C:\Program Files\FOREXMetaTrader 5\terminal64.exe"
    )
    assert broker_by_slug(session, "tradeview").catalogue_paths is None


@pytest.mark.parametrize(
    ("path", "kept"),
    [
        (r"BOVESPA\A VISTA\PETR4", True),
        (r"bmf\series continuas\WIN$N", True),
        ("BMF/SERIES CONTINUAS/WDO$N", True),
        (r"BOVESPA\OPCOES\PETRJ300", False),
        (r"BOVESPA\A VISTAX\NOPE", False),  # a folder that merely starts with the same letters
        (None, False),
    ],
)
def test_a_broker_keeps_only_its_folders(path: str | None, kept: bool) -> None:
    assert keeps_path([r"BOVESPA\A VISTA", "BMF\\SERIES CONTINUAS\\"], path) is kept


def test_a_broker_without_folders_keeps_everything() -> None:
    assert keeps_path(None, r"BOVESPA\OPCOES\PETRJ300")
    assert keeps_path(None, None)


def test_an_internal_name_keeps_the_brokers_ticker_beside_it(session: Session) -> None:
    """XP's `WIN$` is `WIN` here: a `$` is no name for a folder (ADR-0032)."""
    xp = broker_for_server(session, "XPMT5-DEMO")
    replace_snapshot(
        session, [BrokerSymbolEntry(symbol="WIN$")], server="XPMT5-DEMO", synced_at=NOW
    )

    # Before it is catalogued, the ticker is what finds the broker.
    assert broker_slug_for_symbol(session, "WIN", "WIN$") == "xp"
    assert broker_ticker(session, "WIN") == "WIN"

    upsert_instruments(
        session, (CatalogueEntry(spec("WIN", offset_hours=-3), None, "WIN$"),), broker_id=xp.id
    )

    row = session.scalars(select(Instrument).where(Instrument.symbol == "WIN")).one()
    assert (row.broker_id, row.broker_symbol) == (xp.id, "WIN$")
    # After, the instrument remembers it: a later collection of `WIN` asks for `WIN$`.
    assert broker_ticker(session, "WIN") == "WIN$"
    assert broker_slug_for_symbol(session, "WIN") == "xp"
