"""The one thing the search response decides, with no database in sight.

`/symbols/search` is a query and a serialisation. The query is proved against a real Postgres
in `test_symbols_integration.py`; what is proved here is the shape of the answer — specifically
that it keeps apart two situations which both produce an empty list and mean opposite things.
"""

import datetime as dt

from tradeforge_api.schemas import SymbolSearchOut
from tradeforge_db.models import BrokerSymbol

SYNCED_AT = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)


def _row(symbol: str, **fields: object) -> BrokerSymbol:
    """A row as the query would hand it back.

    ⚠️ `visible` is spelled out because column defaults — `default` and `server_default` alike
    — fire when a row is **written**, not when the object is constructed. An unflushed
    `BrokerSymbol` therefore has `visible = None`, which is a fact about building rows by hand
    and not about the data: every row the API actually serialises came back out of a NOT NULL
    column. Leaving it off here would fail the serialiser for a reason production never has.
    """
    fields.setdefault("visible", False)
    return BrokerSymbol(symbol=symbol, synced_at=SYNCED_AT, **fields)


def test_no_match_and_no_catalogue_are_told_apart() -> None:
    """⚠️ Both hand back zero symbols, and they are opposite problems.

    "Nothing starts with `zzz`" means type fewer letters. "Nobody has ever synced this broker"
    means the list does not exist and searching it harder will not help. A response that
    flattened the two would have the screen tell a user with no catalogue that their search
    found nothing — true, and the least useful true sentence available.

    It is the same distinction the instruments table already makes between a NULL spread and a
    zero one, one layer up.
    """
    no_match = SymbolSearchOut.build(symbols=[], server="MetaQuotes-Demo", synced_at=SYNCED_AT)
    no_catalogue = SymbolSearchOut.build(symbols=[], server=None, synced_at=None)

    assert no_match.symbols == []
    assert no_catalogue.symbols == []
    # The list is identical; only the provenance separates them.
    assert no_match.snapshot is not None
    assert no_catalogue.snapshot is None


def test_a_snapshot_from_a_server_that_did_not_name_itself_is_still_a_snapshot() -> None:
    """⚠️ The case a truthiness check would get wrong.

    A terminal that is running but not logged in reports no server name, so `server` is `None`
    while `synced_at` is a real instant — the sync genuinely happened. Deciding "is there a
    catalogue?" from the *name* would call that no catalogue at all and send the user to
    re-sync something that is already there.
    """
    built = SymbolSearchOut.build(symbols=[_row("EURUSD")], server=None, synced_at=SYNCED_AT)

    assert built.snapshot is not None
    assert built.snapshot.server is None
    assert built.snapshot.synced_at == SYNCED_AT


def test_a_result_carries_the_symbol_and_where_the_broker_files_it() -> None:
    built = SymbolSearchOut.build(
        symbols=[
            _row(
                "EURUSD",
                description="Euro vs US Dollar",
                path="Forex\\Majors\\EURUSD",
                digits=5,
                visible=True,
            )
        ],
        server="MetaQuotes-Demo",
        synced_at=SYNCED_AT,
    )

    found = built.symbols[0]
    assert found.symbol == "EURUSD"
    assert found.description == "Euro vs US Dollar"
    assert found.path == "Forex\\Majors\\EURUSD"
    assert found.digits == 5
    assert found.visible is True


def test_a_result_does_not_pretend_to_know_how_to_price_the_symbol() -> None:
    """⚠️ A search result is not an `InstrumentOut`, and must not look like one.

    Most symbols in this list have never been collected, so nothing has ever read their tick
    value or decided their asset class. Serialising zeroes for those fields would hand the
    screen numbers that price a position — the exact numbers that, wrong, make a P&L wrong by a
    constant factor and look like a very good strategy.
    """
    built = SymbolSearchOut.build(symbols=[_row("EURUSD")], server=None, synced_at=SYNCED_AT)

    serialised = built.symbols[0].model_dump()
    assert "tick_value" not in serialised
    assert "tick_size" not in serialised
    assert "asset_class" not in serialised
