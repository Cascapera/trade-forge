"""`/symbols/markets` and `/symbols/browse` over real Postgres (02/10).

His ask: hundreds of symbols, chosen a market at a time — crypto, forex, indices, US and Brazilian
shares — a page at a time.
"""

# The symbol suite's `client` and `queue` fixtures are imported by name, which is how pytest finds
# them; each test then takes them as parameters, which ruff reads as redefining the import.
# ruff: noqa: F811

from collections.abc import Callable
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from tradeforge_db.broker_symbols import BrokerSymbolEntry, replace_snapshot
from tradeforge_db.brokers import broker_for_server
from tradeforge_db.models import Instrument
from tradeforge_engine.domain import AssetClass

from .test_symbols_integration import SYNCED_AT, client, queue  # noqa: F401 — fixtures

pytestmark = pytest.mark.integration


def browsable(session_factory: Callable[[], Session]) -> None:
    """A broker list with currencies, a crypto, thirty US shares and a Brazilian share."""
    entries = [
        BrokerSymbolEntry(symbol="EURUSD", description="Euro vs US Dollar", path=r"Forex\Majors"),
        BrokerSymbolEntry(symbol="GBPUSD", description="Pound vs US Dollar", path=r"Forex\Majors"),
        BrokerSymbolEntry(symbol="BTCUSD", description="Bitcoin", path=r"Cryptocurrency\BTCUSD"),
        *(
            BrokerSymbolEntry(
                symbol=f"US{n:03d}.US", description=f"US share {n}", path=r"Stocks\US"
            )
            for n in range(30)
        ),
        BrokerSymbolEntry(symbol="PETR4.SA", description="Petrobras PN", path=r"Stocks\Brazil"),
    ]
    with session_factory() as session:
        replace_snapshot(session, entries, server="ActivTradesCorp-Server", synced_at=SYNCED_AT)
        session.commit()


def test_the_tabs_count_each_market_and_name_every_one(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    browsable(session_factory)

    body = client.get("/symbols/markets").json()

    counts = {one["key"]: one["count"] for one in body["markets"]}
    assert (counts["forex"], counts["crypto"], counts["stocks_us"], counts["stocks_br"]) == (
        2,
        1,
        30,
        1,
    )
    assert counts["indices"] == 0  # every tab is named, so the tabs never move
    assert body["snapshot"]["server"] == "ActivTradesCorp-Server"


def test_a_market_comes_a_page_at_a_time(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    browsable(session_factory)

    first = client.get("/symbols/browse", params={"market": "stocks_us", "limit": 25}).json()
    second = client.get(
        "/symbols/browse", params={"market": "stocks_us", "limit": 25, "offset": 25}
    ).json()

    assert (first["total"], len(first["items"]), len(second["items"])) == (30, 25, 5)
    assert first["items"][0]["symbol"] == "US000.US"
    assert second["items"][-1]["symbol"] == "US029.US"
    assert {one["market"] for one in first["items"]} == {"stocks_us"}
    assert first["items"][0]["catalogued"] is False


def test_the_search_reads_the_symbol_and_the_description(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    browsable(session_factory)

    by_name = client.get("/symbols/browse", params={"q": "petro"}).json()
    by_symbol = client.get("/symbols/browse", params={"market": "forex", "q": "gbp"}).json()

    assert [one["symbol"] for one in by_name["items"]] == ["PETR4.SA"]
    assert [one["symbol"] for one in by_symbol["items"]] == ["GBPUSD"]


def test_text_the_database_cannot_store_is_refused(client: TestClient) -> None:
    assert client.get("/symbols/browse", params={"q": "\x00"}).status_code == 422


def test_the_collected_ones_are_counted_and_can_be_kept_alone(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    browsable(session_factory)
    with session_factory() as session:
        session.add(
            Instrument(
                symbol="EURUSD",
                name="EURUSD for the browser tests",
                asset_class=AssetClass.FOREX,
                currency_base="EUR",
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
                default_spread_points=Decimal("8"),
            )
        )
        session.commit()

    tabs = client.get("/symbols/markets").json()
    page = client.get("/symbols/browse", params={"market": "forex", "collected": True}).json()

    forex = next(one for one in tabs["markets"] if one["key"] == "forex")
    assert (forex["count"], forex["collected"]) == (2, 1)
    assert [(one["symbol"], float(one["spread_points"])) for one in page["items"]] == [
        ("EURUSD", 8)
    ]


def test_a_whole_market_comes_in_one_request(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    browsable(session_factory)

    whole = client.get("/symbols/browse", params={"market": "stocks_us", "limit": 500}).json()

    assert len(whole["items"]) == 30
    assert client.get("/symbols/browse", params={"limit": 501}).status_code == 422


def test_each_symbol_says_which_brokers_terminal_lists_it(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """ADR-0032: two terminals' lists side by side, each line naming its broker."""
    browsable(session_factory)
    with session_factory() as session:
        replace_snapshot(
            session,
            [BrokerSymbolEntry(symbol="NVDA", description="NVIDIA", path=r"Stocks\US")],
            server="Tradeview-Demo",
            synced_at=SYNCED_AT,
        )
        replace_snapshot(
            session, [BrokerSymbolEntry(symbol="ZZZ")], server="Nobody-Demo", synced_at=SYNCED_AT
        )
        session.commit()

    page = client.get("/symbols/browse", params={"limit": 500}).json()["items"]
    browsed = {one["symbol"]: one["broker"] for one in page}

    def searched(prefix: str) -> str | None:
        found = client.get("/symbols/search", params={"q": prefix}).json()["symbols"]
        return str(found[0]["broker"]) if found[0]["broker"] else None

    assert (browsed["EURUSD"], browsed["NVDA"], browsed["ZZZ"]) == (
        "activtrades",
        "tradeview",
        None,
    )
    assert (searched("btc"), searched("nvd"), searched("zz")) == ("activtrades", "tradeview", None)


def test_a_market_collected_under_its_own_name_is_listed_by_it(
    client: TestClient, session_factory: Callable[[], Session]
) -> None:
    """09/10: WIN$, DOL$ and WDO$ were collected as WIN, DOL and WDO (ADR-0032), and the
    futures tab, matching by name, showed none of them collected — and hid them."""
    browsable(session_factory)
    with session_factory() as session:
        replace_snapshot(
            session,
            [
                BrokerSymbolEntry(
                    symbol="DOL$",
                    description="DOLAR COMERCIAL FUTURO - Ajuste Proporcional",
                    path=r"BMF\SERIES CONTINUAS\DOL$",
                ),
                BrokerSymbolEntry(symbol="EURUSD", description="Euro", path=r"Forex\Majors"),
            ],
            server="XPMT5-DEMO",
            synced_at=SYNCED_AT,
        )
        xp = broker_for_server(session, "XPMT5-DEMO")
        activtrades = broker_for_server(session, "ActivTradesCorp-Server")
        for symbol, ticker, broker in (("DOL", "DOL$", xp), ("EURUSD", "EURUSD", activtrades)):
            session.add(
                Instrument(
                    symbol=symbol,
                    name=symbol,
                    asset_class=AssetClass.FUTURE,
                    currency_quote="BRL",
                    tick_size=Decimal("0.001"),
                    tick_value=Decimal("0.05"),
                    contract_size=Decimal(1),
                    digits=3,
                    broker_id=broker.id,
                    broker_symbol=ticker,
                )
            )
        session.commit()

    page = client.get("/symbols/browse", params={"collected": True, "limit": 500}).json()
    found = client.get("/symbols/search", params={"q": "DOL$"}).json()["symbols"]
    by_name = client.get(
        "/symbols/browse", params={"q": "dol", "market": "futures", "collected": True}
    ).json()

    assert sorted((one["symbol"], one["ticker"], one["broker"]) for one in page["items"]) == [
        ("DOL", "DOL$", "xp"),
        ("EURUSD", "EURUSD", "activtrades"),
    ], "XP's EURUSD is not the one collected from ActivTrades"
    assert (found[0]["symbol"], found[0]["name"], found[0]["catalogued"]) == ("DOL$", "DOL", True)
    assert [one["symbol"] for one in by_name["items"]] == ["DOL"]
