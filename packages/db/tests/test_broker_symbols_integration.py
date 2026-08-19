"""The snapshot against a real Postgres, because every promise it makes is the database's.

Replacing a table, matching a prefix under a real collation, and an index that either serves
`LIKE 'EUR%'` or quietly does not — none of that is provable against a fake. What *is* pure
policy (refusing an empty catalogue) lives in `test_broker_symbols.py` and runs everywhere.
"""

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tradeforge_db.broker_symbols import (
    BrokerSymbolEntry,
    replace_snapshot,
    search_symbols,
    snapshot_taken_at,
)
from tradeforge_db.models import BrokerSymbol, Instrument
from tradeforge_engine.domain import AssetClass

pytestmark = pytest.mark.integration

FIRST = dt.datetime(2026, 8, 19, 12, 0, tzinfo=dt.UTC)
LATER = dt.datetime(2026, 8, 19, 18, 0, tzinfo=dt.UTC)


def entries(*symbols: str) -> list[BrokerSymbolEntry]:
    return [BrokerSymbolEntry(symbol=symbol) for symbol in symbols]


def test_a_snapshot_lands_whole(session: Session) -> None:
    count = replace_snapshot(
        session,
        [
            BrokerSymbolEntry(
                symbol="EURUSD",
                description="Euro vs US Dollar",
                path=r"Forex\Majors\EURUSD",
                digits=5,
                visible=True,
            )
        ],
        server="MetaQuotes-Demo",
        synced_at=FIRST,
    )

    assert count == 1
    row = session.scalars(select(BrokerSymbol)).one()
    assert (row.symbol, row.digits, row.visible) == ("EURUSD", 5, True)
    assert (row.server, row.synced_at) == ("MetaQuotes-Demo", FIRST)


def test_a_second_sync_replaces_rather_than_merges(session: Session) -> None:
    """⚠️ The behaviour that makes switching brokers work, and the one an upsert would break.

    This terminal really did go from 9550 symbols including AAPL to 84 of forex and CFDs.
    Merging would leave the screen offering AAPL from an account that cannot trade it — and
    the list would grow into the union of every broker ever connected, which nothing would
    ever shrink.
    """
    replace_snapshot(session, entries("AAPL", "MSFT"), server="Old-Broker", synced_at=FIRST)

    replace_snapshot(session, entries("EURUSD"), server="New-Broker", synced_at=LATER)

    remaining = session.scalars(select(BrokerSymbol.symbol)).all()
    assert list(remaining) == ["EURUSD"]
    assert snapshot_taken_at(session) == ("New-Broker", LATER)


def test_the_same_symbol_across_two_syncs_does_not_collide(session: Session) -> None:
    """The unique constraint is on `symbol`, and a replace has to survive it.

    ⚠️ A delete-then-insert in one transaction only works because the delete is flushed first.
    Get that ordering wrong and the second sync fails on a duplicate key — for the symbols that
    did *not* change, which is nearly all of them.
    """
    replace_snapshot(session, entries("EURUSD", "GBPUSD"), server="B", synced_at=FIRST)

    replace_snapshot(session, entries("EURUSD", "USDJPY"), server="B", synced_at=LATER)

    assert sorted(session.scalars(select(BrokerSymbol.symbol)).all()) == ["EURUSD", "USDJPY"]


def test_an_empty_catalogue_leaves_the_old_one_standing(session: Session) -> None:
    """The refusal, seen from the outside: the previous snapshot survives it intact."""
    replace_snapshot(session, entries("EURUSD"), server="B", synced_at=FIRST)

    with pytest.raises(ValueError, match="not logged in"):
        replace_snapshot(session, [], server="B", synced_at=LATER)

    assert session.scalars(select(func.count()).select_from(BrokerSymbol)).one() == 1


class TestSearching:
    @pytest.fixture(autouse=True)
    def catalogue(self, session: Session) -> None:
        replace_snapshot(
            session,
            entries("EURUSD", "EURGBP", "EURJPY", "GBPUSD", "USDJPY", "XAUUSD"),
            server="MetaQuotes-Demo",
            synced_at=FIRST,
        )

    def test_a_prefix_matches_from_the_first_letter(self, session: Session) -> None:
        assert [row.symbol for row in search_symbols(session, "e")] == [
            "EURGBP",
            "EURJPY",
            "EURUSD",
        ]

    def test_typing_more_narrows_it(self, session: Session) -> None:
        assert [row.symbol for row in search_symbols(session, "eurj")] == ["EURJPY"]

    def test_the_match_is_case_insensitive(self, session: Session) -> None:
        # Tickers are shown uppercase and typed lowercase. Anything else makes the box feel broken.
        assert [row.symbol for row in search_symbols(session, "EuR")] == [
            "EURGBP",
            "EURJPY",
            "EURUSD",
        ]

    def test_it_is_a_prefix_and_not_a_substring(self, session: Session) -> None:
        """⚠️ The accepted trade-off, asserted so nobody "fixes" it by accident.

        `usd` finds USDJPY and not EURUSD. A substring match would bury AAPL under every symbol
        with those letters in the middle, and a ticker is recalled from its front.
        """
        assert [row.symbol for row in search_symbols(session, "usd")] == ["USDJPY"]

    def test_an_empty_prefix_opens_the_list_rather_than_dumping_it(self, session: Session) -> None:
        found = search_symbols(session, "", limit=2)

        assert [row.symbol for row in found] == ["EURGBP", "EURJPY"]

    def test_nothing_matching_is_an_empty_list_and_not_an_error(self, session: Session) -> None:
        assert search_symbols(session, "zzz") == []

    def test_a_wildcard_typed_by_a_user_is_searched_for_literally(self, session: Session) -> None:
        """⚠️ Brokers ship names like `EURUSD.pro` and `US30_m`, so `%` and `_` reach this query.

        Without escaping, typing `_` would match every symbol with any second character — a
        search box that returns everything the moment somebody hits underscore.
        """
        assert search_symbols(session, "_") == []
        assert search_symbols(session, "%") == []


class TestWhatCanActuallyBeRun:
    """The outer join, which is what keeps the screen from offering 83 choices that fail."""

    @pytest.fixture(autouse=True)
    def catalogue(self, session: Session) -> None:
        # ⚠️ **One of each, in one query.** A scenario where nothing is catalogued cannot
        # tell the join from a hard-coded `False`, and one where everything is cannot tell it
        # from a hard-coded `True`. The separating case needs both sides present at once.
        replace_snapshot(
            session,
            entries("EURUSD", "EURJPY"),
            server="Tradeview-Demo",
            synced_at=FIRST,
        )
        session.add(
            Instrument(
                symbol="EURUSD",
                name="Euro vs US Dollar",
                asset_class=AssetClass.FOREX,
                currency_quote="USD",
                tick_size=Decimal("0.00001"),
                tick_value=Decimal("1"),
                contract_size=Decimal("100000"),
                digits=5,
            )
        )
        session.flush()

    def test_a_symbol_with_an_instrument_row_is_marked_runnable(self, session: Session) -> None:
        found = {match.symbol: match.catalogued for match in search_symbols(session, "eur")}

        assert found == {"EURUSD": True, "EURJPY": False}

    def test_an_uncatalogued_symbol_is_still_offered(self, session: Session) -> None:
        """⚠️ The whole point of the feature: the list is not narrowed to what exists.

        An inner join here would be the easy mistake and would look right — the search would
        return real symbols and nothing would error. It would just quietly hand back the same
        short list the screen already had, which is what this table was added to escape.
        """
        assert [match.symbol for match in search_symbols(session, "eurj")] == ["EURJPY"]


def test_an_unsynced_database_has_no_snapshot(session: Session) -> None:
    """`None`, so the screen can say "never synced" instead of showing an empty broker."""
    assert snapshot_taken_at(session) is None
